"""Synthetic Corellium API with shared state across bounded client copies."""

from dataclasses import replace

from mobile_playbook.platforms.corellium.client import CorelliumError, CorelliumInstance, CorelliumProject
from mobile_playbook.platforms.corellium.selection import AutomationBaseline, ResolvedDevice


def device(instance_id="stock", role="non_rooted", state="off"):
    rooted = role == "rooted"
    instance = CorelliumInstance(instance_id, instance_id, state, None, project="project", type="android",
                                 os="16.0.0", osbuild="r2 userdebug" if rooted else "r2 user", model="Generic Android")
    baseline = AutomationBaseline("android", rooted, instance.os, instance.osbuild, snapshot_id="baseline-" + instance_id)
    return ResolvedDevice(role, instance, baseline.snapshot_id, baseline)


class FakeAPI:
    api_url = "https://synthetic.example/api"
    timeout = 30

    def __init__(self, *devices):
        self.data = {
            "instances": {item.instance.id: item.instance for item in devices},
            "snapshots": {item.instance.id: [{"id": item.baseline_id, "name": "automation-ready-v1",
                                              "instance": item.instance.id, "status": {"created": True, "task": "none"},
                                              "type": item.instance.type, "model": "Generic Android"}] for item in devices},
            "calls": [], "failures": {}, "stuck": set(), "assert_mutation": None, "restore_busy": {},
            "restore_delay": {}, "restore_reads": {},
        }

    def call(self, action, instance_id):
        self.data["calls"].append((action, instance_id))
        if self.data["assert_mutation"] and action in {"restore", "start", "stop", "unpause"}:
            self.data["assert_mutation"]()
        failures = self.data["failures"].get((action, instance_id), [])
        if failures:
            raise failures.pop(0)

    def list_projects(self):
        return [CorelliumProject("project", "automation")]

    def list_instances(self):
        self.call("list", "all")
        return list(self.data["instances"].values())

    def list_snapshots(self, instance_id):
        self.call("snapshots", instance_id)
        return self.data["snapshots"][instance_id]

    def get_snapshot(self, instance_id, snapshot_id):
        self.call("snapshot", instance_id)
        snapshot = next(item for item in self.data["snapshots"][instance_id] if item["id"] == snapshot_id)
        tasks = self.data["restore_busy"].get(instance_id)
        if tasks:
            task = tasks.pop(0)
            return {**snapshot, "status": {"created": True, "task": task}}
        return snapshot

    def get_instance(self, instance_id):
        self.call("get", instance_id)
        instance = self.data["instances"][instance_id]
        reads = self.data["restore_reads"].get(instance_id)
        if reads and reads.pop(0) == "busy":
            return replace(instance, task_state="restoring")
        return instance

    def start_instance(self, instance_id):
        self.call("start", instance_id)
        if instance_id not in self.data["stuck"]:
            self.data["instances"][instance_id] = replace(self.data["instances"][instance_id], state="on")

    def unpause_instance(self, instance_id):
        self.call("unpause", instance_id)
        self.data["instances"][instance_id] = replace(self.data["instances"][instance_id], state="on")

    def stop_instance(self, instance_id):
        self.call("stop", instance_id)
        if instance_id not in self.data["stuck"]:
            self.data["instances"][instance_id] = replace(self.data["instances"][instance_id], state="off")

    def restore_snapshot(self, instance_id, snapshot_id):
        self.call("restore", instance_id)
        if self.data["instances"][instance_id].state != "off":
            raise CorelliumError("Restore requires off")
        assert snapshot_id == "baseline-" + instance_id
        self.data["restore_reads"][instance_id] = ["idle"] * self.data["restore_delay"].get(instance_id, 0) + ["busy"]

import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mobile_playbook.common import env_file
from mobile_playbook.common.storage_paths import corellium_state_dir
from mobile_playbook.platforms.corellium.claims import ClaimBusy, ClaimStore
from mobile_playbook.platforms.corellium.client import CorelliumError
from mobile_playbook.platforms.corellium.lifecycle import CorelliumLifecycle, LifecyclePolicy
from tests.platforms.corellium.helpers import FakeAPI, device


def test_claim_order_deterministic_and_failed_second_lock_leaves_no_claims(tmp_path, monkeypatch):
    store = ClaimStore(FakeAPI.api_url, tmp_path)
    first, second = device("a"), device("z", "rooted")
    occupied = store.claim_all("occupied", (second,))
    attempted = []
    original = store.lock
    def record(instance_id):
        attempted.append(instance_id)
        return original(instance_id)
    monkeypatch.setattr(store, "lock", record)
    try:
        with pytest.raises(ClaimBusy):
            store.claim_all("contender", (second, first))
        assert attempted == ["a", "z"]
        assert [row["run_id"] for row in store.pending()] == ["occupied"]
        assert store.history("contender") == []
        independent = store.claim_all("independent", (first,))
        independent.detach()
    finally:
        occupied.detach()


def test_pending_second_claim_rolls_back_first_row_and_all_history(tmp_path):
    store = ClaimStore(FakeAPI.api_url, tmp_path)
    first, second = device("a"), device("z", "rooted")
    occupied = store.claim_all("abandoned", (second,))
    occupied.detach()
    with pytest.raises(ClaimBusy, match="pending ownership"):
        store.claim_all("contender", (first, second))
    assert len(store.pending()) == 1
    assert store.history("contender") == []
    independent = store.claim_all("independent", (first,))
    independent.detach()


def test_claim_metadata_and_guards(tmp_path):
    store = ClaimStore(FakeAPI.api_url, tmp_path)
    claims = store.claim_all("run-1", (device(),))
    try:
        row = claims.record("stock")
        assert (row["run_id"], row["role"], row["instance_id"], row["baseline_id"]) == (
            "run-1", "non_rooted", "stock", "baseline-stock")
        assert row["pid"] == os.getpid() and row["hostname"] and row["owner"]
        with pytest.raises(CorelliumError, match="confirmed off"):
            store.release("stock", claims.owner)
        with pytest.raises(CorelliumError, match="Ownership lost"):
            store.transition("stock", "wrong-token", "off")
    finally:
        claims.detach()
    with pytest.raises(CorelliumError, match="No active"):
        claims.record("stock")
    with pytest.raises(ClaimBusy):
        store.claim_all("run-2", (device(),))


def test_duplicate_instances_and_roles_rejected_without_claims(tmp_path):
    store = ClaimStore(FakeAPI.api_url, tmp_path)
    with pytest.raises(CorelliumError, match="same instance"):
        store.claim_all("run", (device(), device("stock", "rooted")))
    with pytest.raises(CorelliumError, match="one role"):
        store.claim_all("run", (device(), device("other")))
    assert store.pending() == []


def test_database_corruption_fails_closed(tmp_path):
    store = ClaimStore(FakeAPI.api_url, tmp_path)
    store.path.write_bytes(b"broken database")
    with pytest.raises(sqlite3.DatabaseError):
        ClaimStore(FakeAPI.api_url, tmp_path)


# A separate interpreter owns both instances until it exits, independent of pytest's in-process objects.
OWNER_SCRIPT = """
import sys
from pathlib import Path
from mobile_playbook.platforms.corellium.claims import ClaimStore
from tests.platforms.corellium.helpers import FakeAPI, device
store = ClaimStore(FakeAPI.api_url, Path(sys.argv[1]))
claims = store.claim_all('child-run', (device('a'), device('z', 'rooted')))
print('claimed', flush=True)
sys.stdin.readline()
"""


def test_competing_processes_and_terminated_owner_recovery(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", OWNER_SCRIPT, str(tmp_path)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    devices = (device("a"), device("z", "rooted"))
    api = FakeAPI(*devices)
    store = ClaimStore(api.api_url, tmp_path)
    lifecycle = CorelliumLifecycle(api, store, LifecyclePolicy(1, 1, 0.001))
    try:
        assert select.select([process.stdout], [], [], 10)[0], "Child process did not acquire claims"
        assert process.stdout.readline().strip() == "claimed"
        with pytest.raises(ClaimBusy):
            store.claim_all("parent-run", tuple(reversed(devices)))
        assert {row["status"] for row in lifecycle.recover()} == {"active_owner"}
        assert api.data["calls"] == []
        process.terminate()
        process.wait(timeout=10)
        assert len(ClaimStore(api.api_url, tmp_path).pending()) == 2
        assert lifecycle.recover() == [{"instance_id": "a", "status": "off"}, {"instance_id": "z", "status": "off"}]
        claims = store.claim_all("after-recovery", devices)
        claims.detach()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_new_recovery_process_can_read_durable_records_after_abrupt_exit(tmp_path):
    script = OWNER_SCRIPT.replace("sys.stdin.readline()", "import os; os._exit(0)")
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True, text=True, timeout=10)
    api = FakeAPI(device("a", state="on"), device("z", "rooted", "on"))
    store = ClaimStore(api.api_url, tmp_path)
    lifecycle = CorelliumLifecycle(api, store, LifecyclePolicy(1, 1, 0.001))
    assert len(store.pending()) == 2
    assert all(item["status"] == "off" for item in lifecycle.recover())
    assert all(instance.state == "off" for instance in api.data["instances"].values())


def test_host_storage_and_api_namespaces_are_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("CORELLIUM_STATE_DIR", str(tmp_path))
    assert corellium_state_dir() == tmp_path
    store = ClaimStore("https://synthetic.example/api/v1")
    assert store.path == ClaimStore(FakeAPI.api_url).path
    assert store.path != ClaimStore("https://other.example").path
    with pytest.raises(CorelliumError, match="namespace"):
        CorelliumLifecycle(FakeAPI(), ClaimStore("https://other.example", tmp_path))
    monkeypatch.setenv("CORELLIUM_STATE_DIR", "relative")
    with pytest.raises(ValueError, match="absolute"):
        corellium_state_dir()


def test_tests_never_default_to_the_real_host_store():
    assert not corellium_state_dir().is_relative_to(Path.home() / ".local" / "state")


def test_state_dir_in_env_file_is_rejected_even_when_exported(tmp_path, monkeypatch):
    env_file.ENV_FILE.write_text(f"CORELLIUM_STATE_DIR={tmp_path}\n")
    monkeypatch.setenv("CORELLIUM_STATE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="process environment, not in .env"):
        corellium_state_dir()


def test_forked_child_does_not_keep_the_parent_claim_locked(tmp_path):
    api = FakeAPI(device())
    store = ClaimStore(api.api_url, tmp_path)
    claims = store.claim_all("forked", (device(),))
    read_end, write_end = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_end)
        os.write(write_end, b"ready")
        time.sleep(10)
        os._exit(0)
    try:
        os.close(write_end)
        assert os.read(read_end, 5) == b"ready"
        claims.detach()
        lifecycle = CorelliumLifecycle(api, store, LifecyclePolicy(1, 1, 0.001))
        assert lifecycle.recover() == [{"instance_id": "stock", "status": "off"}]
    finally:
        os.close(read_end)
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)


def test_shared_modules_import_without_android_or_appium_dependencies():
    script = """
import importlib.abc
import sys
class BlockAndroid(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('mobile_playbook.platforms.android', 'appium', 'selenium')):
            raise AssertionError('Platform dependency imported: ' + fullname)
sys.meta_path.insert(0, BlockAndroid())
import importlib, pkgutil
import mobile_playbook.platforms.corellium as package
names = [info.name for info in pkgutil.iter_modules(package.__path__)]
assert {'client', 'selection', 'claims', 'lifecycle', 'profile'} <= set(names), names
for name in names:
    importlib.import_module(f'{package.__name__}.{name}')
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=10)

from __future__ import annotations

import threading

import pytest

from mobile_playbook.workers import launch_agent

SERVICE = f"gui/501/{launch_agent.WORKER_AGENT_LABEL}"


@pytest.fixture
def launchctl(monkeypatch, tmp_path):
    """A fake launchctl over an installed plist; `loaded` and `fail` steer its answers."""
    plist = tmp_path / f"{launch_agent.WORKER_AGENT_LABEL}.plist"
    plist.write_text("<plist/>")
    state = {"loaded": True, "fail": set(), "calls": []}

    def fake(*args):
        state["calls"].append(args)
        if args[0] in state["fail"]:
            return 5, f"{args[0]} refused"
        if args[0] == "print":
            return (0, "state = running") if state["loaded"] else (113, "Could not find service")
        if args[0] == "bootstrap":
            state["loaded"] = True
        if args[0] == "bootout":
            state["loaded"] = False
        return 0, ""

    monkeypatch.setattr(launch_agent, "_launchctl", fake)
    monkeypatch.setattr(launch_agent, "agent_plist", lambda: plist)
    monkeypatch.setattr(launch_agent, "_domain", lambda: "gui/501")
    monkeypatch.setattr(launch_agent, "worker_agent_installed", lambda: True)
    state["plist"] = plist
    return state


def test_a_loaded_worker_is_restarted_so_it_runs_the_current_code(launchctl):
    assert launch_agent.start_worker_agent() == "started"

    assert launchctl["calls"] == [("print", SERVICE), ("kickstart", "-k", SERVICE)]


def test_an_unloaded_agent_is_loaded_before_it_is_started(launchctl):
    launchctl["loaded"] = False

    assert launch_agent.start_worker_agent() == "started"

    assert launchctl["calls"] == [
        ("print", SERVICE),
        ("bootstrap", "gui/501", str(launchctl["plist"])),
        ("kickstart", "-k", SERVICE),
    ]


def test_a_failed_load_does_not_try_to_start(launchctl):
    launchctl["loaded"] = False
    launchctl["fail"].add("bootstrap")

    assert launch_agent.start_worker_agent() == "failed"

    assert [call[0] for call in launchctl["calls"]] == ["print", "bootstrap"]


def test_stopping_unloads_the_agent_so_its_schedule_cannot_restart_the_worker(launchctl):
    assert launch_agent.stop_worker_agent() == "stopped"

    assert launchctl["calls"] == [("print", SERVICE), ("bootout", SERVICE)]
    assert launchctl["loaded"] is False


def test_stopping_an_unloaded_agent_does_nothing(launchctl):
    launchctl["loaded"] = False

    assert launch_agent.stop_worker_agent() == "not_running"

    assert launchctl["calls"] == [("print", SERVICE)]


def test_without_the_agent_installed_launchctl_is_never_run(launchctl, monkeypatch):
    monkeypatch.setattr(launch_agent, "worker_agent_installed", lambda: False)

    assert launch_agent.start_worker_agent() == "not_installed"
    assert launch_agent.stop_worker_agent() == "not_installed"
    assert launchctl["calls"] == []


def test_the_worker_starts_only_once_the_api_is_listening(monkeypatch):
    probes = iter([False, False, True])
    started = []
    monkeypatch.setattr(launch_agent, "LISTEN_POLL_SECONDS", 0)
    monkeypatch.setattr(launch_agent, "tcp_reachable", lambda url, timeout: next(probes))
    monkeypatch.setattr(launch_agent, "start_worker_agent", lambda: started.append(True))

    launch_agent.start_worker_agent_when_listening("http://127.0.0.1:8080", threading.Event()).join(timeout=5)

    assert started == [True]


def test_a_server_that_stops_before_listening_never_starts_the_worker(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(launch_agent, "tcp_reachable", lambda url, timeout: pytest.fail("probed after cancel"))
    monkeypatch.setattr(launch_agent, "start_worker_agent", lambda: pytest.fail("started after cancel"))

    launch_agent.start_worker_agent_when_listening("http://127.0.0.1:8080", cancelled).join(timeout=5)


def test_a_server_that_never_listens_gives_up(monkeypatch):
    monkeypatch.setattr(launch_agent, "LISTEN_WAIT_SECONDS", 0)
    monkeypatch.setattr(launch_agent, "tcp_reachable", lambda url, timeout: False)
    monkeypatch.setattr(launch_agent, "start_worker_agent", lambda: pytest.fail("started without a server"))

    thread = launch_agent.start_worker_agent_when_listening("http://127.0.0.1:8080", threading.Event())
    thread.join(timeout=5)

    assert not thread.is_alive()


def test_the_api_launcher_import_does_not_load_worker_credentials():
    import subprocess
    import sys

    probe = (
        "import sys, mobile_playbook.api.__main__; "
        "print(any(name.endswith(('supabase_store', 'assessment_worker')) for name in sys.modules))"
    )
    output = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert output.strip() == "False"

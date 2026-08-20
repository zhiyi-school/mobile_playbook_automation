from __future__ import annotations

from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.reporting.report_writer import ReportWriter


class FakeRunner:
    platform = "fake"

    def __init__(self):
        self.connect_calls = 0
        self.health_check_calls = []
        self.run_test_calls = []

    def requires_device(self, config, selected_tests, selected_apps):
        return True

    def connect_device(self, config, run_dir=None):
        self.connect_calls += 1
        return f"client-{self.connect_calls}"

    def close_device(self, device_client):
        pass

    def ensure_device_healthy(self, config, device_client, run_dir=None):
        self.health_check_calls.append(device_client)
        return device_client

    def iter_enabled_tests(self, config, selected_tests, selected_apps):
        yield "app_one", "risk_one"
        yield "app_one", "risk_two"

    def run_test(self, app, test_id, config, device_client, report_writer):
        self.run_test_calls.append((app, test_id, device_client))


def test_run_platform_checks_device_health_before_every_test(tmp_path):
    runner = FakeRunner()
    outcome = run_platform(
        config=object(),
        platform_runner=runner,
        options=RunOptions(out_dir=tmp_path),
        report_writer_factory=lambda out_dir, run_timestamp: ReportWriter(out_dir, run_timestamp),
    )

    assert runner.connect_calls == 1
    assert len(runner.health_check_calls) == 2  # once before each of the two tests
    assert [call[1] for call in runner.run_test_calls] == ["risk_one", "risk_two"]
    assert outcome.run_dir.exists()


def test_run_platform_uses_reconnected_client_for_later_tests(tmp_path):
    class ReconnectingRunner(FakeRunner):
        def ensure_device_healthy(self, config, device_client, run_dir=None):
            self.health_check_calls.append(device_client)
            if len(self.health_check_calls) == 2:
                return "reconnected-client"  # simulate recovery kicking in before the 2nd test
            return device_client

    runner = ReconnectingRunner()
    run_platform(
        config=object(),
        platform_runner=runner,
        options=RunOptions(out_dir=tmp_path),
        report_writer_factory=lambda out_dir, run_timestamp: ReportWriter(out_dir, run_timestamp),
    )

    clients_used = [call[2] for call in runner.run_test_calls]
    assert clients_used == ["client-1", "reconnected-client"]

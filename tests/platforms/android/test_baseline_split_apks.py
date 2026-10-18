from mobile_playbook.orchestration.scan_runner import RunOptions, run_platform
from mobile_playbook.platforms.android.acquisition import intake_apk
from mobile_playbook.platforms.android.config import load_config
from mobile_playbook.reporting.report_writer import ReportWriter
from tests.platforms.android.baseline_fakes import PACKAGE, PLAIN, BaselineBench


def test_baseline_preparation_installs_every_original_split(tmp_path, monkeypatch, write_apk_set, apk_metadata):
    bench = BaselineBench(tmp_path, monkeypatch, risks=(PLAIN,), roles=("non_rooted",))
    paths = write_apk_set(bench.apk.parent / PACKAGE, package=PACKAGE, version_code="322")
    monkeypatch.setattr(intake_apk, "inspect_apk_metadata", lambda path: apk_metadata[path.name])
    bench.apk.unlink()
    bench.adb.apks["base.apk"] = (PACKAGE, "1.2.3", "322")
    outcome = run_platform(load_config(bench.config_path), bench.runner(),
                           RunOptions(out_dir=tmp_path / "reports"),
                           lambda out, ts: ReportWriter(out, ts))
    assert outcome is not None
    pushes = [args[1] for serial, args in bench.adb.commands if args[0] == "push"]
    assert pushes == [str(path) for path in paths]
    installs = [args for serial, args in bench.adb.commands if args[:3] == ["shell", "pm", "install"]]
    assert len(installs) == 1
    assert installs[0][-2:] == ["/data/local/tmp/mobile_playbook_install/0.apk", "/data/local/tmp/mobile_playbook_install/1.apk"]

from __future__ import annotations

import json
from types import SimpleNamespace

from mobile_playbook.platforms.android.models import AndroidRiskRunResult
from mobile_playbook.platforms.android.private_data.findings import DEFAULT_MARKDOWN_TITLE, record_scan
from mobile_playbook.platforms.android.private_data.scan import scan_file
from tests.platforms.android.private_data_fakes import DATA, ENCRYPTED_PREFS, PACKAGE, TOKEN

ACCESS_FLAG = {"id": "EXAMPLE_ACCESS", "severity": "HIGH", "title": "Read for the test", "evidence": [], "recommendation": "None."}
APP = SimpleNamespace(id="example", name="Example", package_name=PACKAGE)


def make_result() -> AndroidRiskRunResult:
    return AndroidRiskRunResult(
        run_timestamp="2026-10-19_00-00-00",
        timestamp_start="2026-10-19T00:00:00+08:00",
        timestamp_end=None,
        app_id=APP.id,
        app_name=APP.name,
        package_name=PACKAGE,
        risk_id="android-feature-00-risk-00",
        test_case_id="private_data_access",
        test_case_type="example",
    )


def test_the_callers_access_flag_and_markdown_title_are_used(tmp_path):
    files = [scan_file(f"/{DATA}/shared_prefs/session.xml", f'<map><string name="auth_token">{TOKEN}</string></map>'.encode(), False)]
    result = make_result()

    record_scan(files, ACCESS_FLAG, APP, tmp_path, result, markdown_title="Example Title")

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_EXPOSED", "At Risk")
    critical = json.loads((tmp_path / "critical_findings.json").read_text())
    assert [flag["id"] for flag in critical["flags"]] == ["EXAMPLE_ACCESS", "SENSITIVE_INFORMATION_EXPOSURE"]
    markdown = (tmp_path / "critical_findings.md").read_text()
    assert "Example Title" in markdown
    assert DEFAULT_MARKDOWN_TITLE not in markdown


def test_the_markdown_title_defaults_to_the_private_data_title(tmp_path):
    result = make_result()

    record_scan([scan_file(f"/{DATA}/shared_prefs/secure_prefs.xml", ENCRYPTED_PREFS, False)], ACCESS_FLAG, APP, tmp_path, result)

    assert (result.final_status, result.verdict) == ("PRIVATE_DATA_PROTECTED", "Reduced Risk")
    assert DEFAULT_MARKDOWN_TITLE in (tmp_path / "critical_findings.md").read_text()

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from mobile_playbook.api.routes import reports as api_reports
from mobile_playbook.api.services import reports as reports_service
from mobile_playbook.reporting.evidence import (
    decode_ref,
    encode_ref,
    normalize_evidence,
    report_dir_evidence,
)


def _report_dir(root, *names):
    directory = root / "ios" / "example_app" / "example-feature-01-risk-01" / "example_case"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text("example")
    return directory


class TestReportDirEvidence:
    def test_names_the_artifacts_a_reader_recognises(self, tmp_path):
        directory = _report_dir(tmp_path, "report.json", "logs.txt", "critical_findings.md")

        items = report_dir_evidence(directory)

        assert [item["label"] for item in items] == [
            "Detailed result report",
            "Critical findings",
            "Run log",
        ]

    def test_puts_the_detailed_report_before_the_log(self, tmp_path):
        directory = _report_dir(tmp_path, "logs.txt", "report.json")
        assert report_dir_evidence(directory)[0]["label"] == "Detailed result report"

    def test_offers_an_unnamed_artifact_labelled_from_its_suffix(self, tmp_path):
        directory = _report_dir(tmp_path, "target_screen-navigation-step-1.png")

        item = report_dir_evidence(directory)[0]

        assert item["kind"] == "screenshot"
        assert item["label"] == "Screenshot (target_screen-navigation-step-1.png)"
        assert item["source"].name == "target_screen-navigation-step-1.png"

    def test_lists_a_recording_as_one(self, tmp_path):
        directory = _report_dir(tmp_path, "example.mp4")
        assert report_dir_evidence(directory)[0]["kind"] == "screen_recording"

    def test_says_nothing_for_a_directory_that_was_never_written(self, tmp_path):
        assert report_dir_evidence(tmp_path / "missing") == []

    def test_ignores_subdirectories(self, tmp_path):
        directory = _report_dir(tmp_path, "report.json")
        (directory / "nested").mkdir()

        assert [item["label"] for item in report_dir_evidence(directory)] == [
            "Detailed result report"
        ]


class TestNormalizeEvidence:
    def test_keeps_a_declared_artifact_that_exists(self, tmp_path):
        artifact = tmp_path / "work" / "acquired.ipa"
        artifact.parent.mkdir()
        artifact.write_text("example")

        items = normalize_evidence(
            [{"kind": "ipa", "path": str(artifact), "label": "Acquired IPA"}],
            None,
            {"work": tmp_path / "work"},
        )

        assert items[0]["kind"] == "ipa"
        assert items[0]["label"] == "Acquired IPA"
        assert items[0]["path"] == "work/acquired.ipa"
        assert decode_ref(items[0]["ref"]) == ("work", "acquired.ipa")
        assert items[0]["size_bytes"] == len("example")

    def test_drops_an_artifact_outside_every_allowed_root(self, tmp_path):
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("example")
        (tmp_path / "work").mkdir()

        assert normalize_evidence(
            [{"kind": "log", "path": str(outside)}], None, {"work": tmp_path / "work"}
        ) == []

    def test_drops_a_declared_artifact_that_is_gone(self, tmp_path):
        assert normalize_evidence([{"kind": "ipa", "path": str(tmp_path / "gone.ipa")}]) == []

    def test_drops_an_entry_with_no_path_at_all(self):
        assert normalize_evidence([{"kind": "ipa", "path": "  "}, {"kind": "log"}]) == []

    def test_reports_one_entry_per_file_however_it_was_named(self, tmp_path):
        directory = _report_dir(tmp_path, "report.json")
        declared = {"kind": "report", "path": str(directory / "report.json"), "label": "Mine"}

        items = normalize_evidence([declared], directory)

        assert len(items) == 1
        assert items[0]["label"] == "Mine"

    def test_falls_back_to_the_file_name_when_nothing_named_it(self, tmp_path):
        artifact = tmp_path / "logs.txt"
        artifact.write_text("example")

        assert normalize_evidence([{"kind": "log", "path": str(artifact)}])[0]["label"] == "logs.txt"

    def test_round_trips_a_reference_through_its_encoding(self):
        ref = encode_ref("reports", "run/ios/app/report.json")
        assert "run/ios" not in ref
        assert decode_ref(ref) == ("reports", "run/ios/app/report.json")

    def test_refuses_a_reference_it_did_not_write(self):
        for bad in ["", "!!!!", "bm90LWEtcmVm", "cmVwb3J0cw"]:
            assert decode_ref(bad) is None or decode_ref(bad)[1]

    def test_adds_the_report_directory_to_what_the_run_declared(self, tmp_path):
        directory = _report_dir(tmp_path, "report.json")
        acquired = tmp_path / "acquired.ipa"
        acquired.write_text("example")

        items = normalize_evidence([{"kind": "ipa", "path": str(acquired), "label": "IPA"}], directory)

        assert [item["label"] for item in items] == ["IPA", "Detailed result report"]

    def test_needs_no_report_directory_at_all(self, tmp_path):
        artifact = tmp_path / "logs.txt"
        artifact.write_text("example")
        assert len(normalize_evidence([{"kind": "log", "path": str(artifact)}], None)) == 1


class TestServedRowsCarryTheirArtifacts:
    @pytest.fixture(autouse=True)
    def reports_root(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        root = tmp_path / "reports"
        monkeypatch.setattr(reports_service, "REPORTS_ROOT", root)
        return root

    def _run(self, root, evidence=None):
        run_dir = root / "2026-01-02_00-00-00"
        run_dir.mkdir(parents=True)
        _report_dir(run_dir, "report.json", "logs.txt")
        (run_dir / "dashboard_results.json").write_text(
            json.dumps(
                [
                    {
                        "run_timestamp": "2026-01-02_00-00-00",
                        "app_id": "example_app",
                        "test_id": "example-feature-01-risk-01",
                        "verdict": "At Risk",
                        "report_path": "ios/example_app/example-feature-01-risk-01/example_case",
                        "evidence": evidence or [],
                    }
                ]
            )
        )
        return run_dir

    def test_a_run_recorded_before_this_still_gains_its_artifacts(self, reports_root):
        self._run(reports_root)

        rows = reports_service.read_dashboard_results("2026-01-02_00-00-00")

        assert [item["label"] for item in rows[0]["evidence"]] == [
            "Detailed result report",
            "Run log",
        ]

    def test_the_history_endpoint_carries_them_too(self, reports_root):
        self._run(reports_root)

        history = reports_service.app_risk_history("example_app", "example-feature-01-risk-01", 5)

        assert len(history[0]["evidence"]) == 2

    def test_evidence_stays_with_its_own_application_and_risk(self, reports_root):
        self._run(reports_root)

        assert reports_service.app_risk_history("other_app", "example-feature-01-risk-01", 5) == []
        assert reports_service.app_risk_history("example_app", "example-feature-02-risk-01", 5) == []

    def test_a_report_path_pointing_outside_the_run_is_ignored(self, reports_root):
        run_dir = self._run(reports_root)
        (run_dir / "dashboard_results.json").write_text(
            json.dumps(
                [
                    {
                        "run_timestamp": "2026-01-02_00-00-00",
                        "app_id": "example_app",
                        "test_id": "example-feature-01-risk-01",
                        "report_path": "../../etc",
                        "evidence": [],
                    }
                ]
            )
        )

        rows = reports_service.read_dashboard_results("2026-01-02_00-00-00")

        assert rows[0]["evidence"] == []


class TestEvidenceStaysInsideItsRoots:
    @pytest.fixture(autouse=True)
    def roots(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        (reports / "2026-01-02_00-00-00").mkdir(parents=True)
        (tmp_path / "work").mkdir()
        monkeypatch.setattr(reports_service, "REPORTS_ROOT", reports)
        monkeypatch.setattr(reports_service, "WORK_ROOT", tmp_path / "work")
        # Started from anywhere: resolution must not depend on the working directory.
        monkeypatch.chdir(tmp_path.parent)
        return tmp_path

    def test_serves_a_file_inside_the_reports_root(self, roots):
        artifact = roots / "reports" / "2026-01-02_00-00-00" / "report.json"
        artifact.write_text("example")

        resolved = reports_service.safe_evidence_path(
            "2026-01-02_00-00-00", encode_ref("reports", "2026-01-02_00-00-00/report.json")
        )

        assert resolved == artifact.resolve()

    def test_serves_a_work_artifact_belonging_to_the_run(self, roots):
        artifact = roots / "work" / "ios" / "acquired" / "2026-01-02_00-00-00" / "original.ipa"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"PK\x03\x04example")

        resolved = reports_service.safe_evidence_path(
            "2026-01-02_00-00-00",
            encode_ref("work", "ios/acquired/2026-01-02_00-00-00/original.ipa"),
        )

        assert resolved == artifact.resolve()

    def test_refuses_a_handle_it_did_not_write(self, roots):
        for bad in ["", "not-base64!!", "Zm9v"]:
            with pytest.raises(HTTPException) as excinfo:
                reports_service.safe_evidence_path("2026-01-02_00-00-00", bad)
            assert excinfo.value.status_code == 400

    def test_refuses_an_unknown_root(self, roots):
        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path("2026-01-02_00-00-00", encode_ref("etc", "passwd"))
        assert excinfo.value.status_code == 400

    def test_refuses_a_traversal_out_of_the_allowed_roots(self, roots):
        outside = roots / "secret.txt"
        outside.write_text("example")

        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", "../secret.txt")
            )
        assert excinfo.value.status_code == 400

    def test_refuses_an_absolute_path_inside_the_handle(self, roots):
        outside = roots / "secret.txt"
        outside.write_text("example")

        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", str(outside))
            )
        assert excinfo.value.status_code == 400

    def test_refuses_a_symlink_pointing_out_of_the_root(self, roots):
        outside = roots / "secret.txt"
        outside.write_text("example")
        link = roots / "reports" / "2026-01-02_00-00-00" / "escape.json"
        link.symlink_to(outside)

        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", "2026-01-02_00-00-00/escape.json")
            )
        assert excinfo.value.status_code == 404

    def test_refuses_another_run_s_artifact(self, roots):
        other = roots / "reports" / "2026-01-09_00-00-00"
        other.mkdir()
        (other / "report.json").write_text("example")

        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", "2026-01-09_00-00-00/report.json")
            )
        assert excinfo.value.status_code == 404
        assert "for this run" in excinfo.value.detail

    def test_refuses_a_work_artifact_from_another_run(self, roots):
        artifact = roots / "work" / "ios" / "acquired" / "2026-01-09_00-00-00" / "original.ipa"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("example")

        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00",
                encode_ref("work", "ios/acquired/2026-01-09_00-00-00/original.ipa"),
            )
        assert excinfo.value.status_code == 404

    def test_refuses_a_directory(self, roots):
        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", "2026-01-02_00-00-00")
            )
        assert excinfo.value.status_code == 404

    def test_refuses_a_file_that_is_not_there(self, roots):
        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", "2026-01-02_00-00-00/gone.json")
            )
        assert excinfo.value.status_code == 404

    def test_never_names_a_host_path_in_the_error(self, roots):
        for ref in [encode_ref("reports", "2026-01-02_00-00-00/gone.json"), "bad"]:
            with pytest.raises(HTTPException) as excinfo:
                reports_service.safe_evidence_path("2026-01-02_00-00-00", ref)
            assert str(roots) not in str(excinfo.value.detail)


class TestServedHeaders:
    def test_names_the_file_rather_than_the_endpoint(self):
        assert reports_service.safe_download_name("original.ipa") == "original.ipa"

    def test_strips_separators_and_quotes_out_of_a_filename(self):
        assert reports_service.safe_download_name('../../etc/pa"ss wd') == "pa_ss_wd"
        assert reports_service.safe_download_name("a/b/c.json") == "c.json"

    def test_never_returns_an_empty_filename(self):
        assert reports_service.safe_download_name("...") == "evidence"
        assert reports_service.safe_download_name("") == "evidence"

    def test_types_the_artifacts_a_browser_guesses_badly(self):
        assert reports_service.media_type_for("original.ipa") == "application/octet-stream"
        assert reports_service.media_type_for("app.apk") == "application/vnd.android.package-archive"
        assert reports_service.media_type_for("critical_findings.md") == "text/markdown; charset=utf-8"
        assert reports_service.media_type_for("report.json") == "application/json"
        assert reports_service.media_type_for("screen.mp4") == "video/mp4"
        assert reports_service.media_type_for("shot.png") == "image/png"

    def test_falls_back_for_an_unknown_suffix(self):
        assert reports_service.media_type_for("thing.unknownext") == "application/octet-stream"


class TestReferencesReplacePaths:
    def test_an_artifact_is_offered_by_handle_rather_than_by_host_path(self, tmp_path):
        directory = _report_dir(tmp_path / "reports", "report.json")

        item = normalize_evidence(None, directory, {"reports": tmp_path / "reports"})[0]

        assert not item["path"].startswith("/")
        assert str(tmp_path) not in item["ref"]
        assert decode_ref(item["ref"])[0] == "reports"


class TestDownloadedBytesMatchTheSource:
    """Every artifact kind, compared by size and digest through the real route."""

    @pytest.fixture(autouse=True)
    def roots(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        (reports / "2026-01-02_00-00-00").mkdir(parents=True)
        (tmp_path / "work").mkdir()
        monkeypatch.setattr(reports_service, "REPORTS_ROOT", reports)
        monkeypatch.setattr(reports_service, "WORK_ROOT", tmp_path / "work")
        monkeypatch.chdir(tmp_path.parent)
        return tmp_path

    SAMPLES = {
        "report.json": b'{"verdict": "At Risk"}',
        "critical_findings.md": b"# Critical findings\n\n- Example\n",
        "target_screen.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)),
        "original.ipa": b"PK\x03\x04" + bytes(range(256)) * 4,
        "screen.mp4": b"\x00\x00\x00 ftypisom" + bytes(range(128)),
        "logs.txt": b"example log line\n",
    }

    def _serve(self, name, roots):
        source = roots / "reports" / "2026-01-02_00-00-00" / name
        source.write_bytes(self.SAMPLES[name])
        response = api_reports.evidence_file(
            "2026-01-02_00-00-00", encode_ref("reports", f"2026-01-02_00-00-00/{name}")
        )
        return source, response

    def test_serves_every_kind_byte_for_byte(self, roots):
        for name, expected in self.SAMPLES.items():
            source, response = self._serve(name, roots)
            served = Path(response.path).read_bytes()

            assert served == expected, name
            assert served == source.read_bytes(), name
            assert (
                hashlib.sha256(served).hexdigest() == hashlib.sha256(expected).hexdigest()
            ), name

    def test_reports_the_source_length(self, roots):
        for name, expected in self.SAMPLES.items():
            _, response = self._serve(name, roots)
            headers = {k.decode(): v.decode() for k, v in response.raw_headers}
            assert headers["content-length"] == str(len(expected)), name

    def test_names_the_artifact_as_an_attachment(self, roots):
        for name in self.SAMPLES:
            _, response = self._serve(name, roots)
            headers = {k.decode(): v.decode() for k, v in response.raw_headers}
            assert headers["content-disposition"].startswith("attachment"), name
            assert name in headers["content-disposition"], name

    def test_types_each_artifact_for_the_browser(self, roots):
        expected = {
            "report.json": "application/json",
            "critical_findings.md": "text/markdown; charset=utf-8",
            "target_screen.png": "image/png",
            "original.ipa": "application/octet-stream",
            "screen.mp4": "video/mp4",
            "logs.txt": "text/plain; charset=utf-8",
        }
        for name, media_type in expected.items():
            _, response = self._serve(name, roots)
            assert response.media_type == media_type, name

    def test_a_refused_request_raises_instead_of_serving_a_body(self, roots):
        # An error must never reach the browser as the artifact it asked for.
        self._serve("original.ipa", roots)
        with pytest.raises(HTTPException) as excinfo:
            api_reports.evidence_file(
                "2026-01-02_00-00-00", encode_ref("reports", "2026-01-02_00-00-00/gone.ipa")
            )
        assert excinfo.value.status_code == 404


class TestRecordedPathsAreInstallationRelative:
    def test_a_declared_relative_path_resolves_against_the_installation(self, tmp_path, monkeypatch):
        artifact = tmp_path / "work" / "ios" / "original.ipa"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"PK\x03\x04")
        # Started from somewhere else entirely.
        monkeypatch.chdir(tmp_path.parent)

        items = normalize_evidence(
            [{"kind": "ipa", "path": "work/ios/original.ipa", "label": "Acquired IPA"}],
            None,
            {"work": tmp_path / "work"},
            tmp_path,
        )

        assert [item["path"] for item in items] == ["work/ios/original.ipa"]
        assert decode_ref(items[0]["ref"]) == ("work", "ios/original.ipa")

    def test_it_is_dropped_when_the_installation_does_not_hold_it(self, tmp_path, monkeypatch):
        (tmp_path / "work").mkdir()
        monkeypatch.chdir(tmp_path.parent)

        assert (
            normalize_evidence(
                [{"kind": "ipa", "path": "work/ios/original.ipa"}],
                None,
                {"work": tmp_path / "work"},
                tmp_path,
            )
            == []
        )


class TestMalformedReferencesAreRefusedNotRaised:
    @pytest.fixture(autouse=True)
    def roots(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        (reports / "2026-01-02_00-00-00").mkdir(parents=True)
        monkeypatch.setattr(reports_service, "REPORTS_ROOT", reports)
        monkeypatch.setattr(reports_service, "WORK_ROOT", tmp_path / "work")
        return tmp_path

    def test_a_reference_carrying_a_null_byte_is_refused(self, roots):
        # It reaches the filesystem call as a ValueError unless it is rejected first.
        assert decode_ref(encode_ref("reports", "run/\x00etc")) is None

        with pytest.raises(HTTPException) as excinfo:
            reports_service.safe_evidence_path(
                "2026-01-02_00-00-00", encode_ref("reports", "run/\x00etc")
            )
        assert excinfo.value.status_code == 400

    def test_a_tampered_reference_is_refused_rather_than_crashing(self, roots):
        good = encode_ref("reports", "2026-01-02_00-00-00/report.json")
        for tampered in [good[:-4] + "AAAA", good[:-1], good + "ZZZZ", good[::-1]]:
            with pytest.raises(HTTPException) as excinfo:
                reports_service.safe_evidence_path("2026-01-02_00-00-00", tampered)
            assert excinfo.value.status_code in {400, 404}

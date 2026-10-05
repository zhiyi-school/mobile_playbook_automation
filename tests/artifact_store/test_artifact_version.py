from __future__ import annotations

from types import SimpleNamespace

from mobile_playbook.artifact_store.resolver import artifact_version_for_app_config
from tests.conftest import make_ipa


def _app(artifact):
    return SimpleNamespace(id="care", name="Care", bundle_id="sg.example.care", package_name="", artifact=artifact)


def test_reads_the_version_of_the_ipa_a_local_ipa_app_uses(tmp_path):
    ipa = make_ipa(tmp_path / "Care_4.3.4.ipa", bundle_id="sg.example.care", version="4.3.4")

    assert artifact_version_for_app_config("ios", _app({"source": "local_ipa", "ipa": str(ipa)})) == "4.3.4"


def test_reads_the_pinned_intake_build_not_the_highest(tmp_path):
    make_ipa(tmp_path / "Care_4.3.4.ipa", bundle_id="sg.example.care", version="4.3.4")
    make_ipa(tmp_path / "Care_3.9.9.ipa", bundle_id="sg.example.care", version="3.9.9")
    app = _app({"source": "intake_ipa", "intake_dir": str(tmp_path), "build_version": "3.9.9"})

    assert artifact_version_for_app_config("ios", app) == "3.9.9"


def test_a_build_with_no_version_or_no_file_records_none(tmp_path):
    unversioned = make_ipa(tmp_path / "Care.ipa", bundle_id="sg.example.care")

    assert artifact_version_for_app_config("ios", _app({"source": "local_ipa", "ipa": str(unversioned)})) is None
    assert artifact_version_for_app_config("ios", _app({"source": "local_ipa", "ipa": str(tmp_path / "gone.ipa")})) is None


def test_android_records_no_build_version():
    assert artifact_version_for_app_config("android", _app({})) is None

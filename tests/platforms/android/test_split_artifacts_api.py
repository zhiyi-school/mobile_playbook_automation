import asyncio

from fastapi import FastAPI

from mobile_playbook.api.routes.artifacts import router
from mobile_playbook.api.services import artifacts
from tests.asgi_helpers import call


def test_http_artifacts_lists_one_identity_for_a_complete_split_set(tmp_path, monkeypatch, write_apk_set):
    paths = write_apk_set(tmp_path / "com.example.app")
    monkeypatch.setattr(artifacts, "intake_dirs", lambda: {"android": tmp_path})
    app = FastAPI()
    app.include_router(router)
    status, body = asyncio.run(call(app, "GET", "/artifacts/android"))
    assert status == 200
    assert body == [{"file": "base.apk", "path": str(paths[0]), "bundle_id": "com.example.app",
                     "display_name": "Example", "version": "1.2.3", "modified_at": max(path.stat().st_mtime for path in paths)}]


def test_dashboard_icon_uses_density_split_resources(tmp_path, monkeypatch, write_apk_set):
    from mobile_playbook.artifact_store import icon_extraction
    from tests.icon_helpers import make_apk, make_png
    base = make_apk(tmp_path / "source-base.apk", {})
    split = make_apk(tmp_path / "source-split.apk", {"res/mipmap-xhdpi-v4/ic_launcher.png": make_png(96, 96)})
    paths = write_apk_set(tmp_path / "intake/com.example.app", files={"base.apk": base.read_bytes(), "split_config.xhdpi.apk": split.read_bytes()})
    monkeypatch.setattr(icon_extraction, "_android_declared_icons", lambda path: ["res/mipmap-xhdpi-v4/ic_launcher.png"])
    result = icon_extraction.extract_icon("android", paths[0], force=True)
    assert result.available
    assert (result.width, result.height) == (96, 96)


def test_dashboard_resolves_current_split_intake_before_an_old_pulled_build(tmp_path, monkeypatch, write_apk_set):
    from mobile_playbook.artifact_store import resolver
    paths = write_apk_set(tmp_path / "intake/com.example.app")
    stale = tmp_path / "work/com_example_app/original/base.apk"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old build")
    monkeypatch.setattr(resolver, "ANDROID_WORKFLOW_APK_DIR", tmp_path / "work")
    app = {"id": "example", "package_name": "com.example.app", "artifact": {"intake_dir": str(tmp_path / "intake")}}
    assert resolver._resolve_android(app) == paths[0]

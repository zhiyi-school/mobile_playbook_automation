from __future__ import annotations

from mobile_playbook.platforms.static_analysis.mobsf_client import mobsf_scan


def test_mobsf_scan_can_auto_start_mobsf_with_generated_api_key(monkeypatch, tmp_path):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    reachability = iter([False, True])
    popen_calls = []
    api_keys = []

    class FakeProcess:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    fake_process = FakeProcess()

    def fake_popen(command, stdout, stderr, env):
        popen_calls.append({"command": command, "env": env})
        return fake_process

    def fake_post(base_url, endpoint, api_key, data=None, files=None, timeout=120):
        api_keys.append(api_key)
        if endpoint == "/api/v1/upload":
            return {"hash": "generated-key-hash", "scan_type": "ipa", "file_name": "app.ipa"}
        if endpoint == "/api/v1/report_json":
            return {"app_name": "Generated Key Demo", "bundle_id": "com.example.app"}
        return {"ok": True}

    monkeypatch.delenv("MOBSF_API_KEY", raising=False)
    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_is_reachable", lambda base_url, timeout: next(reachability))
    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.subprocess.Popen", fake_popen)
    monkeypatch.setattr("mobile_playbook.platforms.static_analysis.mobsf_client.mobsf_post", fake_post)

    result = mobsf_scan(
        ipa,
        {
            "mobsf_url": "http://127.0.0.1:8000",
            "auto_start": {
                "enabled": True,
                "command": ["mobsf", "runserver", "127.0.0.1:8000"],
                "generate_api_key": True,
                "stop_after_scan": True,
                "wait_seconds": 1,
            },
        },
    )

    assert result["auto_started"] is True
    assert result["generated_api_key"] is True
    assert result["hash"] == "generated-key-hash"
    assert popen_calls[0]["command"] == ["mobsf", "runserver", "127.0.0.1:8000"]
    assert popen_calls[0]["env"]["MOBSF_API_KEY"]
    assert set(api_keys) == {popen_calls[0]["env"]["MOBSF_API_KEY"]}
    assert fake_process.terminated is True

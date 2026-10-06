from __future__ import annotations

import pytest

from mobile_playbook.dashboard_sync import supabase_store
from mobile_playbook.dashboard_sync.contracts import SupabaseRestError
from mobile_playbook.dashboard_sync.supabase_store import SupabaseRestStore


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.body


def respond_with(monkeypatch, body: bytes) -> list[str]:
    urls: list[str] = []

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        return FakeResponse(body)

    monkeypatch.setattr(supabase_store.request, "urlopen", fake_urlopen)
    return urls


@pytest.mark.parametrize(("body", "recovered"), [(b"0", 0), (b"3", 3)])
def test_lease_recovery_reads_the_bare_integer_postgrest_returns(monkeypatch, body, recovered):
    urls = respond_with(monkeypatch, body)
    store = SupabaseRestStore("https://example.supabase.co", "synthetic-key")

    assert store.recover_expired_assessment_run_leases() == recovered
    assert urls == ["https://example.supabase.co/rest/v1/rpc/recover_expired_assessment_run_leases"]


def test_a_non_integer_scalar_response_is_still_rejected(monkeypatch):
    respond_with(monkeypatch, b'"unexpected"')
    store = SupabaseRestStore("https://example.supabase.co", "synthetic-key")

    with pytest.raises(SupabaseRestError, match="unexpected JSON"):
        store.recover_expired_assessment_run_leases()


def test_an_application_upsert_conflicts_on_external_id_and_platform(monkeypatch):
    urls = respond_with(monkeypatch, b'[{"id": "app_1"}]')
    store = SupabaseRestStore("https://example.supabase.co", "synthetic-key")

    store.upsert_application({"external_id": "example_app", "platform": "android", "name": "Example"})

    assert urls == ["https://example.supabase.co/rest/v1/applications?on_conflict=external_id%2Cplatform&select=%2A"]

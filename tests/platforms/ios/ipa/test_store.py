from __future__ import annotations

import hashlib
import os
import stat

from mobile_playbook.platforms.ios.acquisition.local_ipa import LocalIpaProvider
from mobile_playbook.platforms.ios.ipa import store
from mobile_playbook.platforms.ios.ipa.store import STORE_DIR_NAME, place_ipa
from tests.conftest import make_ipa


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_repeated_placements_of_one_ipa_share_a_single_stored_file(tmp_path):
    source = make_ipa(tmp_path / "intake" / "App.ipa")
    store_dir = tmp_path / "acquired" / STORE_DIR_NAME
    first = tmp_path / "acquired" / "run-one" / "original.ipa"
    second = tmp_path / "acquired" / "run-two" / "original.ipa"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)

    assert place_ipa(source, first, store_dir) == _sha(source)
    assert place_ipa(source, second, store_dir) == _sha(source)

    stored = list(store_dir.glob("*.ipa"))
    assert [path.name for path in stored] == [f"{_sha(source)}.ipa"]
    assert os.path.samefile(first, second) and os.path.samefile(first, stored[0])
    assert stored[0].stat().st_nlink == 3
    assert not stat.S_IMODE(stored[0].stat().st_mode) & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_different_ipas_get_their_own_stored_files(tmp_path):
    store_dir = tmp_path / STORE_DIR_NAME
    one = make_ipa(tmp_path / "one.ipa", bundle_id="com.example.one")
    two = make_ipa(tmp_path / "two.ipa", bundle_id="com.example.two")

    place_ipa(one, tmp_path / "a.ipa", store_dir)
    place_ipa(two, tmp_path / "b.ipa", store_dir)

    assert sorted(path.name for path in store_dir.glob("*.ipa")) == sorted([f"{_sha(one)}.ipa", f"{_sha(two)}.ipa"])


def test_rewriting_the_source_in_place_does_not_change_placed_copies(tmp_path):
    source = make_ipa(tmp_path / "App.ipa")
    placed = tmp_path / "run" / "original.ipa"
    placed.parent.mkdir()
    digest = place_ipa(source, placed, tmp_path / STORE_DIR_NAME)

    with source.open("r+b") as handle:
        handle.write(b"XXXX")

    assert _sha(placed) == digest != _sha(source)


def test_a_failed_link_falls_back_to_an_independent_copy(tmp_path, monkeypatch):
    source = make_ipa(tmp_path / "App.ipa")
    placed = tmp_path / "run" / "original.ipa"
    placed.parent.mkdir()

    def refuse(*args, **kwargs):
        raise OSError("hard links unsupported")

    monkeypatch.setattr(store.os, "link", refuse)

    assert place_ipa(source, placed, tmp_path / STORE_DIR_NAME) == _sha(source)
    assert placed.stat().st_nlink == 1


def test_a_source_that_changes_while_being_stored_is_copied_instead(tmp_path, monkeypatch):
    source = make_ipa(tmp_path / "App.ipa")
    placed = tmp_path / "run" / "original.ipa"
    placed.parent.mkdir()
    store_dir = tmp_path / STORE_DIR_NAME
    real_sha = store.sha256_file
    calls = []

    def changing_sha(path):
        calls.append(path)
        return "0" * 64 if len(calls) == 2 else real_sha(path)

    monkeypatch.setattr(store, "sha256_file", changing_sha)

    assert place_ipa(source, placed, store_dir) == _sha(source)
    assert placed.stat().st_nlink == 1
    assert list(store_dir.iterdir()) == []


def test_local_ipa_acquisition_links_each_run_to_the_stored_ipa(global_config, tmp_path):
    app = global_config.apps[0]
    out_dir = tmp_path / "acquired"
    provider = LocalIpaProvider()

    first = provider.acquire(app, global_config, None, "2026-01-01_00-00-00", out_dir)
    second = provider.acquire(app, global_config, None, "2026-01-02_00-00-00", out_dir)

    assert first.status == second.status == "ACQUIRED"
    assert first.ipa_path == out_dir / "2026-01-01_00-00-00" / app.id / "original.ipa"
    assert first.input_sha256 == second.input_sha256 == _sha(first.ipa_path)
    assert os.path.samefile(first.ipa_path, second.ipa_path)
    assert len(list((out_dir / STORE_DIR_NAME).glob("*.ipa"))) == 1

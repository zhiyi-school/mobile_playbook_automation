from __future__ import annotations

from mobile_playbook.platforms.android.acquisition import app_gallery, play_store, store


def test_store_exports_all_come_from_the_selected_store():
    selected = {app_gallery.__name__: app_gallery, play_store.__name__: play_store}[store.install_missing_app.__module__]
    package = app_gallery.APP_GALLERY_PACKAGE if selected is app_gallery else play_store.PLAY_STORE_PACKAGE

    assert store.STORE_PACKAGE == package
    assert store.STORE_NAME == selected.STORE_NAME
    assert store.WORK_DIR_NAME == selected.WORK_DIR_NAME
    assert store.failure is selected.failure

"""
Chooses the app store that installs missing Android apps and that tamper checks watch for; switch stores by changing the import below.
"""

from mobile_playbook.platforms.android.acquisition.app_gallery import (
    APP_GALLERY_PACKAGE as STORE_PACKAGE,
    STORE_NAME,
    WORK_DIR_NAME,
    failure,
    install_from_app_gallery as install_missing_app,
)

# from mobile_playbook.platforms.android.acquisition.play_store import (
#     PLAY_STORE_PACKAGE as STORE_PACKAGE,
#     STORE_NAME,
#     WORK_DIR_NAME,
#     failure,
#     install_from_play_store as install_missing_app,
# )

__all__ = ["STORE_NAME", "STORE_PACKAGE", "WORK_DIR_NAME", "failure", "install_missing_app"]

"""
Chooses the app store that installs missing Android apps; switch stores by changing the import below.
"""

from mobile_playbook.platforms.android.acquisition.app_gallery import (
    WORK_DIR_NAME,
    failure,
    install_from_app_gallery as install_missing_app,
)

# from mobile_playbook.platforms.android.acquisition.play_store import (
#     WORK_DIR_NAME,
#     failure,
#     install_from_play_store as install_missing_app,
# )

__all__ = ["WORK_DIR_NAME", "failure", "install_missing_app"]

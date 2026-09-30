"""
Runs the dashboard sync worker with `python -m mobile_playbook.dashboard_sync`.
"""

from mobile_playbook.dashboard_sync.worker import main


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations


def check_app_one(driver, report_dir):
    return {"status": "PASS", "name": "check_app_one"}


def run_app_specific_check(name: str | None, driver, report_dir):
    if not name:
        return None
    func = globals().get(name)
    if func is None or not callable(func):
        return {"status": "FAIL", "errors": [f"App-specific check not found: {name}"]}
    return func(driver, report_dir)

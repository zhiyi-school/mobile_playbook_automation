from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

def check_app_one(driver, report_dir):
    return {"status": "PASS", "name": "check_app_one"}


def run_app_specific_check(name: str | None, driver, report_dir):
    if not name:
        logger.debug("ios behavior: no app-specific check configured")
        return None
    func = globals().get(name)
    if func is None or not callable(func):
        logger.debug("ios behavior: app-specific check %s not found", name)
        return {"status": "FAIL", "errors": [f"App-specific check not found: {name}"]}
    logger.debug("ios behavior: running app-specific check %s (report_dir=%s)", name, report_dir)
    result = func(driver, report_dir)
    logger.debug("ios behavior: app-specific check %s -> %s", name, result)
    return result

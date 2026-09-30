"""
Builds the risk catalogue from risk classes, YAML metadata and the playbook, plus the PAC file.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import HTTPException

from mobile_playbook.api import config_editing
from mobile_playbook.api.services import playbook_assets
from mobile_playbook.api.schemas import Platform
from mobile_playbook.api.services import playbook as playbook_service
from mobile_playbook.common import network
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks

PAC_DIRECT_HOST_PATTERNS = ["*.apple.com", "*.icloud.com", "ocsp.apple.com", "*.push.apple.com"]
TRAFFIC_INTERCEPTION_RISK_ID = {"ios": "ios-feature-02-risk-01"}
logger = logging.getLogger(__name__)


# Build the platform's risk list with YAML metadata, playbook overviews, demonstrations and controls.
def list_platform_risks(platform: Platform) -> list[dict]:
    risks = list_android_risks() if platform == "android" else list_ios_risks()
    controls_by_risk, controls_error = playbook_service.risk_control_summaries(platform)
    logger.debug(
        "api: building %d %s risk(s); controls for %d risk(s), controls_error=%r.",
        len(risks),
        platform,
        len(controls_by_risk),
        controls_error,
    )
    for risk in risks:
        risk.update(config_editing.get_risk_metadata(platform, risk["risk_id"]))
        _apply_playbook_overview(risk, playbook_service.risk_overview(platform, risk["risk_id"]))
        demonstration = playbook_service.risk_demonstration(platform, risk["risk_id"])
        if demonstration is None:
            logger.debug("api: risk %s has no playbook demonstration; using the YAML entry.", risk["risk_id"])
            demonstration = config_editing.get_risk_demonstration(platform, risk["risk_id"])
        risk["demonstration"] = playbook_assets.decorate_demonstration(platform, demonstration)
        risk["controls"] = controls_by_risk.get(risk["risk_id"], [])
        risk["controls_available"] = controls_error is None
        risk["controls_error"] = controls_error
    return risks


# Overlay the playbook's title, description and tactic on a risk, keeping YAML values as fallback.
def _apply_playbook_overview(risk: dict, overview: dict | None) -> None:
    risk.setdefault("tactic_id", None)
    if overview is None:
        logger.debug("api: no playbook overview for risk %s; keeping YAML metadata.", risk.get("risk_id"))
        return
    for source_field, target_field in (("title", "name"), ("description", "description")):
        text = str(overview.get(source_field) or "").strip()
        if text:
            risk[target_field] = text
    if overview.get("tactic"):
        risk["tactic"] = overview["tactic"]
        risk["tactic_id"] = overview.get("tactic_id")


# Render a PAC file that proxies through the configured Burp listener, sending Apple hosts direct.
def traffic_interception_pac(platform: Platform, proxy_host: str | None = None) -> str:
    risk_id = TRAFFIC_INTERCEPTION_RISK_ID.get(platform)
    if risk_id is None:
        logger.debug("api: no traffic-interception risk for %s; responding 404.", platform)
        raise HTTPException(status_code=404, detail=f"No traffic-interception proxy config for platform {platform}")
    settings = config_editing.get_risk_settings(platform, risk_id)
    proxy_url = str((settings.get("burp") or {}).get("proxy_url") or "")
    if not proxy_url:
        logger.debug("api: %s.burp.proxy_url is empty; responding 400.", risk_id)
        raise HTTPException(status_code=400, detail=f"{risk_id}.burp.proxy_url is not configured")

    parsed = urlparse(proxy_url)
    host = proxy_host or parsed.hostname or "127.0.0.1"
    if not proxy_host and host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        host = network.detect_lan_ip()
        logger.debug("api: loopback proxy host replaced with detected LAN address %s.", host)
    port = parsed.port or 8080
    logger.debug("api: PAC for %s proxies through %s:%s.", platform, host, port)

    direct_conditions = " ||\n      ".join(f'shExpMatch(host, "{pattern}")' for pattern in PAC_DIRECT_HOST_PATTERNS)
    return (
        "function FindProxyForURL(url, host) {\n"
        f"  if ({direct_conditions}) {{\n"
        "    return \"DIRECT\";\n"
        "  }\n"
        f'  return "PROXY {host}:{port}";\n'
        "}\n"
    )

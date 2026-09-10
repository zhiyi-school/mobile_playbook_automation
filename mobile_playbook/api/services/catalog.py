from __future__ import annotations

import socket
from urllib.parse import urlparse

from fastapi import HTTPException

from mobile_playbook.api import config_editor, playbook_assets
from mobile_playbook.api.models import Platform
from mobile_playbook.api.services import playbook as playbook_service
from mobile_playbook.platforms.android.risks import list_risks as list_android_risks
from mobile_playbook.platforms.ios.risks import list_risks as list_ios_risks

PAC_DIRECT_HOST_PATTERNS = ["*.apple.com", "*.icloud.com", "ocsp.apple.com", "*.push.apple.com"]
TRAFFIC_INTERCEPTION_RISK_ID = {"ios": "ios-feature-02-risk-01"}


def list_platform_risks(platform: Platform) -> list[dict]:
    risks = list_android_risks() if platform == "android" else list_ios_risks()
    controls_by_risk, controls_error = playbook_service.risk_control_summaries(platform)
    for risk in risks:
        risk.update(config_editor.get_risk_metadata(platform, risk["risk_id"]))
        _apply_playbook_overview(risk, playbook_service.risk_overview(platform, risk["risk_id"]))
        demonstration = playbook_service.risk_demonstration(platform, risk["risk_id"])
        if demonstration is None:
            demonstration = config_editor.get_risk_demonstration(platform, risk["risk_id"])
        risk["demonstration"] = playbook_assets.decorate_demonstration(platform, demonstration)
        risk["controls"] = controls_by_risk.get(risk["risk_id"], [])
        risk["controls_available"] = controls_error is None
        risk["controls_error"] = controls_error
    return risks


def _apply_playbook_overview(risk: dict, overview: dict | None) -> None:
    """The playbook document wins where it says something; the YAML entry remains the fallback."""
    risk.setdefault("tactic_id", None)
    if overview is None:
        return
    for source_field, target_field in (("title", "name"), ("description", "description")):
        text = str(overview.get(source_field) or "").strip()
        if text:
            risk[target_field] = text
    if overview.get("tactic"):
        risk["tactic"] = overview["tactic"]
        risk["tactic_id"] = overview.get("tactic_id")


def detect_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def traffic_interception_pac(platform: Platform, proxy_host: str | None = None) -> str:
    risk_id = TRAFFIC_INTERCEPTION_RISK_ID.get(platform)
    if risk_id is None:
        raise HTTPException(status_code=404, detail=f"No traffic-interception proxy config for platform {platform}")
    settings = config_editor.get_risk_settings(platform, risk_id)
    proxy_url = str((settings.get("burp") or {}).get("proxy_url") or "")
    if not proxy_url:
        raise HTTPException(status_code=400, detail=f"{risk_id}.burp.proxy_url is not configured")

    parsed = urlparse(proxy_url)
    host = proxy_host or parsed.hostname or "127.0.0.1"
    if not proxy_host and host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        host = detect_lan_ip()
    port = parsed.port or 8080

    direct_conditions = " ||\n      ".join(f'shExpMatch(host, "{pattern}")' for pattern in PAC_DIRECT_HOST_PATTERNS)
    return (
        "function FindProxyForURL(url, host) {\n"
        f"  if ({direct_conditions}) {{\n"
        "    return \"DIRECT\";\n"
        "  }\n"
        f'  return "PROXY {host}:{port}";\n'
        "}\n"
    )

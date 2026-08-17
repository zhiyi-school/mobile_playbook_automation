from __future__ import annotations

from mobile_playbook.platforms.android.risks.base import AndroidRisk


def _registry() -> dict[str, type[AndroidRisk]]:
    from mobile_playbook.platforms.android.risks.repackaging import AndroidRepackagingRisk
    from mobile_playbook.platforms.android.risks.screen_capture import AndroidScreenCaptureRisk

    return {
        AndroidScreenCaptureRisk.risk_id: AndroidScreenCaptureRisk,
        AndroidRepackagingRisk.risk_id: AndroidRepackagingRisk,
    }


def get_risk(risk_id: str) -> AndroidRisk | None:
    risk_type = _registry().get(risk_id)
    return risk_type() if risk_type else None


def known_risks() -> set[str]:
    return set(_registry())


def list_risks() -> list[dict]:
    risks = []
    for risk_id, risk_type in sorted(_registry().items()):
        risk = risk_type()
        risks.append(
            {
                "risk_id": risk_id,
                "name": risk.name,
                "requires_device": risk.requires_device,
                "requires": list(risk.requires),
            }
        )
    return risks

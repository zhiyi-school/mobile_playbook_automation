from __future__ import annotations

RISK_IDS = {"ios-feature1-risk1", "ios-feature5-risk1"}


def known_risks() -> set[str]:
    return set(RISK_IDS)


def get_risk(risk_id: str):
    if risk_id == "ios-feature1-risk1":
        from mobile_playbook.platforms.ios.risks.feature1_risk1 import Feature1Risk1

        return Feature1Risk1()
    if risk_id == "ios-feature5-risk1":
        from mobile_playbook.platforms.ios.risks.feature5_risk1 import Feature5Risk1

        return Feature5Risk1()
    return None


def list_risks() -> list[dict[str, object]]:
    from mobile_playbook.platforms.ios.risks.feature1_risk1 import Feature1Risk1
    from mobile_playbook.platforms.ios.risks.feature5_risk1 import Feature5Risk1

    risks = [Feature1Risk1(), Feature5Risk1()]
    return [
        {
            "risk_id": risk.risk_id,
            "feature_id": risk.feature_id,
            "name": risk.name,
            "requires_ipa_artifact": risk.requires_ipa_artifact,
        }
        for risk in risks
    ]

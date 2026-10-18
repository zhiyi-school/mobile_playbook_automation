"""
Parses an aapt or aapt2 AndroidManifest.xml tree dump into the package, SDK levels, permissions, application flags
and components the APK analysis reports.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

COMPONENT_TAGS = ("activity", "activity-alias", "service", "receiver", "provider")
PLAY_SERVICES_VERSION_METADATA = "com.google.android.gms.version"

# Runtime ("dangerous") permissions from the Android platform manifest; the user must grant each one explicitly.
DANGEROUS_PERMISSIONS = frozenset(
    f"android.permission.{name}"
    for name in (
        "ACCEPT_HANDOVER", "ACCESS_BACKGROUND_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_FINE_LOCATION",
        "ACCESS_MEDIA_LOCATION", "ACTIVITY_RECOGNITION", "ADD_VOICEMAIL", "ANSWER_PHONE_CALLS",
        "BLUETOOTH_ADVERTISE", "BLUETOOTH_CONNECT", "BLUETOOTH_SCAN", "BODY_SENSORS", "BODY_SENSORS_BACKGROUND",
        "CALL_PHONE", "CAMERA", "GET_ACCOUNTS", "NEARBY_WIFI_DEVICES", "POST_NOTIFICATIONS", "PROCESS_OUTGOING_CALLS",
        "READ_CALENDAR", "READ_CALL_LOG", "READ_CONTACTS", "READ_EXTERNAL_STORAGE", "READ_MEDIA_AUDIO",
        "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_VISUAL_USER_SELECTED", "READ_PHONE_NUMBERS",
        "READ_PHONE_STATE", "READ_SMS", "RECEIVE_MMS", "RECEIVE_SMS", "RECEIVE_WAP_PUSH", "RECORD_AUDIO",
        "SEND_SMS", "USE_SIP", "UWB_RANGING", "WRITE_CALENDAR", "WRITE_CALL_LOG", "WRITE_CONTACTS",
        "WRITE_EXTERNAL_STORAGE",
    )
)

_ELEMENT_RE = re.compile(r"^(\s*)E: (\S+)")
_ATTRIBUTE_RE = re.compile(r"^\s*A: ([^=]+)=(.*)$")
_QUOTED_RE = re.compile(r'^"(.*?)"(?: \(Raw: .*\))?$')
_TYPED_RE = re.compile(r"^\(type 0x([0-9a-fA-F]+)\)0x([0-9a-fA-F]+)$")


@dataclass
class _Element:
    tag: str
    indent: int
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list[_Element] = field(default_factory=list)


# Parse a manifest tree dump into the manifest summary the analysis reports.
def parse_manifest_tree(text: str) -> dict[str, Any]:
    root = _parse_elements(text)
    manifest = next((element for element in root.children if element.tag == "manifest"), None)
    if manifest is None:
        raise ValueError("The manifest dump has no <manifest> element")
    uses_sdk = _first_child(manifest, "uses-sdk")
    target_sdk = _as_int(uses_sdk.attributes.get("targetSdkVersion")) if uses_sdk else None
    application = _first_child(manifest, "application")
    app_attributes = application.attributes if application else {}
    permissions = sorted(
        {
            str(child.attributes["name"])
            for child in manifest.children
            if child.tag in {"uses-permission", "uses-permission-sdk-23"} and child.attributes.get("name")
        }
    )
    components = [_component(element, target_sdk) for element in (application.children if application else []) if element.tag in COMPONENT_TAGS]
    summary = {
        "package_name": manifest.attributes.get("package"),
        "version_code": _as_text(manifest.attributes.get("versionCode")),
        "version_name": _as_text(manifest.attributes.get("versionName")),
        "min_sdk": _as_int(uses_sdk.attributes.get("minSdkVersion")) if uses_sdk else None,
        "target_sdk": target_sdk,
        "permissions": permissions,
        "dangerous_permissions": [name for name in permissions if name in DANGEROUS_PERMISSIONS],
        "application": {
            "debuggable": _as_bool(app_attributes.get("debuggable")),
            "allow_backup": _as_bool(app_attributes.get("allowBackup")),
            "uses_cleartext_traffic": _as_bool(app_attributes.get("usesCleartextTraffic")),
            "network_security_config": "networkSecurityConfig" in app_attributes,
        },
        "components": components,
        "exported_counts": exported_counts(components),
    }
    logger.debug(
        "android manifest: package=%s target_sdk=%s permissions=%s dangerous=%s components=%s exported=%s application=%s",
        summary["package_name"], target_sdk, len(permissions), len(summary["dangerous_permissions"]),
        len(components), summary["exported_counts"], summary["application"],
    )
    return summary


# Report whether the manifest declares the Google Play services client version meta-data, which every Play services
# client library adds to the application.
def declares_play_services(text: str) -> bool:
    root = _parse_elements(text)
    manifest = next((element for element in root.children if element.tag == "manifest"), None)
    if manifest is None:
        raise ValueError("The manifest dump has no <manifest> element")
    application = _first_child(manifest, "application")
    return any(
        child.tag == "meta-data" and child.attributes.get("name") == PLAY_SERVICES_VERSION_METADATA
        for child in (application.children if application else [])
    )


# Count, per component type, the exported components other apps can reach without holding a permission; launcher
# activities are left out because every app must export one.
def exported_counts(components: list[dict[str, Any]]) -> dict[str, int]:
    counts = {tag: 0 for tag in COMPONENT_TAGS}
    for component in unprotected_exported_components(components):
        counts[component["type"]] += 1
    return counts


# Return the exported components that need no permission, excluding launcher activities.
def unprotected_exported_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in components if item["exported"] and not item["permission"] and not item["launcher"]]


# Build an element tree from the dump's indentation, under a synthetic root.
def _parse_elements(text: str) -> _Element:
    root = _Element(tag="", indent=-1)
    stack = [root]
    for line in text.splitlines():
        element_match = _ELEMENT_RE.match(line)
        if element_match:
            indent = len(element_match.group(1))
            while stack[-1].indent >= indent:
                stack.pop()
            element = _Element(tag=element_match.group(2), indent=indent)
            stack[-1].children.append(element)
            stack.append(element)
            continue
        attribute_match = _ATTRIBUTE_RE.match(line)
        if attribute_match and len(stack) > 1:
            stack[-1].attributes[_attribute_name(attribute_match.group(1))] = _attribute_value(attribute_match.group(2))
    return root


# Return the bare attribute name from "android:exported(0x01010010)" or "http://.../android:exported(0x01010010)".
def _attribute_name(key: str) -> str:
    key = re.sub(r"\(0x[0-9a-fA-F]+\)$", "", key.strip())
    return key.rsplit(":", 1)[-1]


# Decode an attribute value: quoted strings, aapt typed values (0x12 boolean, 0x10/0x11 integer), aapt2 literals,
# or the raw text such as a resource reference.
def _attribute_value(raw: str) -> Any:
    raw = raw.strip()
    quoted = _QUOTED_RE.match(raw)
    if quoted:
        return quoted.group(1)
    typed = _TYPED_RE.match(raw)
    if typed:
        value_type, value = int(typed.group(1), 16), int(typed.group(2), 16)
        return value != 0 if value_type == 0x12 else value
    if raw in {"true", "false"}:
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


# Describe one component: its type, name, whether other apps can reach it, its guarding permission and intent filters.
def _component(element: _Element, target_sdk: int | None) -> dict[str, Any]:
    filters = [child for child in element.children if child.tag == "intent-filter"]
    actions = sorted(_filter_names(filters, "action"))
    categories = sorted(_filter_names(filters, "category"))
    explicit = element.attributes.get("exported")
    if isinstance(explicit, bool):
        exported = explicit
    elif element.tag == "provider":
        # Providers were exported by default until API 17.
        exported = target_sdk is not None and target_sdk < 17
    else:
        # Without android:exported, a component with an intent filter is exported (apps targeting API 31+ must set it).
        exported = bool(filters)
    return {
        "type": element.tag,
        "name": element.attributes.get("name"),
        "exported": exported,
        "exported_explicit": explicit if isinstance(explicit, bool) else None,
        "enabled": _as_bool(element.attributes.get("enabled")) is not False,
        "permission": element.attributes.get("permission"),
        "intent_actions": actions,
        "intent_categories": categories,
        "launcher": "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories,
    }


# Return the android:name values of the intent filters' children with this tag, such as "action" or "category".
def _filter_names(filters: list[_Element], tag: str) -> set[str]:
    return {
        str(child.attributes["name"])
        for intent_filter in filters
        for child in intent_filter.children
        if child.tag == tag and child.attributes.get("name")
    }


# Return the first direct child element with this tag, or None.
def _first_child(element: _Element, tag: str) -> _Element | None:
    return next((child for child in element.children if child.tag == tag), None)


# Return a boolean attribute value, or None when it is unset or not a boolean.
def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


# Return an integer attribute value, or None when it is unset or not numeric.
def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


# Return an attribute value as text, or None when unset.
def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)

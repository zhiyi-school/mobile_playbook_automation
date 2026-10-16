"""
Minimal Corellium REST client that lists projects and virtual devices, picks one for a test, and powers it on and
off, independent of its platform.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from mobile_playbook.common.logging_setup import safe_url

logger = logging.getLogger(__name__)

API_URL_ENV = "CORELLIUM_API_URL"
API_TOKEN_ENV = "CORELLIUM_API_TOKEN"
DEFAULT_BOOT_TIMEOUT_SECONDS = 600.0
POLL_SECONDS = 5.0
# Corellium lists this patch on jailbroken iOS instances; stock iOS instances list "corelliumd" instead.
JAILBROKEN_PATCH = "jailbroken"
# Android build variants that allow root. Corellium lists the jailbroken patch on every Android instance, so for
# Android the build variant in `osbuild` (e.g. "r2 user") tells a rooted instance from a stock one.
ROOTED_ANDROID_BUILDS = frozenset({"userdebug", "eng"})
# States an instance passes through on its own; waiting on them ends in "on" or a failure state.
TRANSITIONAL_STATES = frozenset({"creating", "booting", "rebooting", "restoring"})
# How strongly each usable state is preferred when choosing an instance: one already on avoids starting another.
STATE_PREFERENCE = {"on": 0, **{state: 1 for state in TRANSITIONAL_STATES}, "paused": 2, "off": 3}


class CorelliumError(RuntimeError):
    """The Corellium API could not be used, or no instance reached a usable state."""


@dataclass(frozen=True)
class CorelliumProject:
    """A Corellium project's id and name."""

    id: str
    name: str


@dataclass(frozen=True)
class CorelliumInstance:
    """The fields of a Corellium instance the framework uses."""

    id: str
    name: str
    state: str
    service_ip: str | None
    project: str = ""
    type: str = ""
    os: str = ""
    patches: tuple[str, ...] = field(default_factory=tuple)
    osbuild: str = ""
    adb_port: int | None = None

    # Report whether the instance is rooted (Android, from its build variant) or jailbroken (iOS, from its patches).
    @property
    def rooted(self) -> bool:
        if self.type == "android":
            variant = self.osbuild.split()[-1] if self.osbuild.split() else ""
            return variant in ROOTED_ANDROID_BUILDS
        return JAILBROKEN_PATCH in self.patches

    # Build an instance from the API's JSON representation.
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CorelliumInstance:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            state=str(data.get("state") or "unknown"),
            service_ip=data.get("serviceIp") or None,
            project=str(data.get("project") or ""),
            type=str(data.get("type") or ""),
            os=str(data.get("os") or ""),
            patches=tuple(str(patch) for patch in data.get("patches") or ()),
            osbuild=str(data.get("osbuild") or ""),
            # Reported while the instance is on; unset otherwise.
            adb_port=int(data["port-adb"]) if str(data.get("port-adb") or "").isdigit() else None,
        )

    # Describe the instance for error messages, e.g. "pixel (android 16.0.0, rooted, off)".
    def describe(self) -> str:
        rooted = "rooted" if self.rooted else "not rooted"
        return f"{self.name or self.id} ({self.type} {self.os}, {rooted}, {self.state})"


class CorelliumClient:
    """Calls the Corellium REST API with a bearer token."""

    # Store the API base URL, normalised to end in /api, and the API token.
    def __init__(self, api_url: str, token: str, timeout: float = 30.0):
        self.api_url = api_base_url(api_url)
        self.token = token
        self.timeout = timeout

    # Build a client from CORELLIUM_API_URL and CORELLIUM_API_TOKEN in the process environment.
    @classmethod
    def from_env(cls) -> CorelliumClient:
        api_url = os.environ.get(API_URL_ENV, "").strip()
        token = os.environ.get(API_TOKEN_ENV, "").strip()
        missing = [name for name, value in ((API_URL_ENV, api_url), (API_TOKEN_ENV, token)) if not value]
        if missing:
            raise CorelliumError(f"{' and '.join(missing)} must be set to use a Corellium device.")
        return cls(api_url, token)

    # Return every project the token can see.
    def list_projects(self) -> list[CorelliumProject]:
        return [
            CorelliumProject(id=str(item.get("id") or ""), name=str(item.get("name") or ""))
            for item in self._request("GET", "/v1/projects") or []
        ]

    # Return every instance the token can see, across all projects.
    def list_instances(self) -> list[CorelliumInstance]:
        return [CorelliumInstance.from_json(item) for item in self._request("GET", "/v1/instances") or []]

    # Return the instance with the given id.
    def get_instance(self, instance_id: str) -> CorelliumInstance:
        return CorelliumInstance.from_json(self._request("GET", f"/v1/instances/{instance_id}") or {})

    # Ask Corellium to power the instance on.
    def start_instance(self, instance_id: str) -> None:
        self._request("POST", f"/v1/instances/{instance_id}/start", {})

    # Ask Corellium to resume a paused instance.
    def unpause_instance(self, instance_id: str) -> None:
        self._request("POST", f"/v1/instances/{instance_id}/unpause", {})

    # Ask Corellium to power the instance off.
    def stop_instance(self, instance_id: str) -> None:
        self._request("POST", f"/v1/instances/{instance_id}/stop", {})

    # Send one API request and return its decoded JSON body, or None when it has none.
    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        logger.debug("corellium: %s %s", method, safe_url(url))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace").strip()
            logger.debug("corellium: %s %s -> HTTP %s", method, path, exc.code)
            raise CorelliumError(f"Corellium {method} {path} failed with HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            logger.debug("corellium: %s %s unreachable: %s", method, path, exc, exc_info=True)
            raise CorelliumError(f"Corellium API not reachable at {safe_url(self.api_url)}: {exc}") from exc
        if not payload.strip():
            return None
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise CorelliumError(f"Corellium {method} {path} did not return JSON") from exc


# Return the API root ending in /api, accepting the site URL, the /api root or the /api/v1 root.
def api_base_url(api_url: str) -> str:
    base = api_url.strip().rstrip("/")
    if base.endswith("/api/v1"):
        return base[: -len("/v1")]
    return base if base.endswith("/api") else f"{base}/api"


# Return the instance the settings select for a platform ("android" or "ios") from the given projects and instances.
def select_instance(
    projects: list[CorelliumProject],
    instances: list[CorelliumInstance],
    settings: dict[str, Any],
    platform: str,
) -> CorelliumInstance:
    wanted = str(settings.get("project") or "").strip()
    matches = [project for project in projects if wanted in (project.id, project.name)]
    if not matches:
        known = ", ".join(sorted(project.name for project in projects)) or "none"
        raise CorelliumError(f"No Corellium project is named {wanted!r}; projects this token can see: {known}.")
    if len(matches) > 1:
        raise CorelliumError(f"Several Corellium projects are named {wanted!r}; set device.corellium.project to the project id.")
    project = matches[0]
    in_project = [instance for instance in instances if instance.project == project.id]

    rooted = bool(settings.get("rooted"))
    os_version = str(settings.get("os_version") or "").strip()
    pinned = str(settings.get("instance") or "").strip()
    candidates = [
        instance
        for instance in in_project
        if instance.type == platform
        and instance.rooted == rooted
        and instance.state in STATE_PREFERENCE
        and (not os_version or instance.os == os_version or instance.os.startswith(f"{os_version}."))
        and (not pinned or pinned in (instance.id, instance.name))
    ]
    if not candidates:
        wanted_device = f"{'rooted' if rooted else 'non-rooted'} {platform}{f' {os_version}' if os_version else ''} instance"
        if pinned:
            wanted_device += f" named {pinned!r}"
        found = "; ".join(instance.describe() for instance in sorted(in_project, key=lambda item: item.name)) or "no instances"
        raise CorelliumError(f"Corellium project {project.name!r} has no usable {wanted_device}. It has: {found}.")
    chosen = min(candidates, key=lambda instance: (STATE_PREFERENCE[instance.state], instance.name, instance.id))
    logger.debug("corellium: chose %s from %s candidates in project %s", chosen.describe(), len(candidates), project.name)
    return chosen


# Return the selected instance, listing the token's projects and instances to choose it.
def find_instance(client: CorelliumClient, settings: dict[str, Any], platform: str) -> CorelliumInstance:
    return select_instance(client.list_projects(), client.list_instances(), settings, platform)


# Power the instance on when it is off or paused, then wait until it is on and return it.
def ensure_instance_on(
    client: CorelliumClient,
    instance_id: str,
    *,
    timeout: float = DEFAULT_BOOT_TIMEOUT_SECONDS,
    poll: float = POLL_SECONDS,
) -> CorelliumInstance:
    deadline = time.monotonic() + timeout
    instance = client.get_instance(instance_id)
    logger.debug("corellium: instance %s is %s", instance_id, instance.state)
    if instance.state == "off":
        logger.info("Powering on Corellium instance %s.", instance.name or instance_id)
        client.start_instance(instance_id)
    elif instance.state == "paused":
        logger.info("Resuming paused Corellium instance %s.", instance.name or instance_id)
        client.unpause_instance(instance_id)
    elif instance.state not in TRANSITIONAL_STATES and instance.state != "on":
        raise CorelliumError(f"Corellium instance {instance_id} is {instance.state} and cannot be used.")
    while instance.state != "on":
        if time.monotonic() >= deadline:
            raise CorelliumError(f"Corellium instance {instance_id} was still {instance.state} after {timeout:.0f}s.")
        time.sleep(poll)
        instance = client.get_instance(instance_id)
        logger.debug("corellium: instance %s is %s", instance_id, instance.state)
        if instance.state == "error":
            raise CorelliumError(f"Corellium instance {instance_id} entered the error state while starting.")
    return instance


# Power the instance off unless it already is, logging rather than raising when Corellium refuses.
def stop_instance(client: CorelliumClient, instance: CorelliumInstance) -> None:
    try:
        if client.get_instance(instance.id).state == "off":
            return
        logger.info("Powering off Corellium instance %s.", instance.name or instance.id)
        client.stop_instance(instance.id)
    except CorelliumError as exc:
        logger.warning("Could not power off Corellium instance %s: %s", instance.name or instance.id, exc)


# Return the problems in Corellium device settings that every platform shares, prefixed with their config path.
def settings_errors(settings: Any, label: str = "device.corellium") -> list[str]:
    if not isinstance(settings, dict):
        return [f"{label} must be a mapping"]
    errors = []
    if not str(settings.get("project") or "").strip():
        errors.append(f"{label}.project is required")
    if not isinstance(settings.get("rooted"), bool):
        errors.append(f"{label}.rooted must be true or false")
    for key in ("auto_start", "auto_stop"):
        if key in settings and not isinstance(settings[key], bool):
            errors.append(f"{label}.{key} must be true or false")
    value = settings.get("boot_timeout_seconds")
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
        errors.append(f"{label}.boot_timeout_seconds must be a positive number")
    return errors

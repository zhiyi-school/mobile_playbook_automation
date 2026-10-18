"""
Shared Corellium REST client and primitives for instance selection, power, snapshots, screenshots and touch input,
independent of device connection packages.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mobile_playbook.common.env_file import env_value
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

    # Preserve retry classification without requiring consumers to parse error text.
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


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
    task_state: str = "none"
    model: str = ""

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
            task_state=str(data.get("taskState") or "none"),
            model=str(data.get("model") or ""),
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

    # Read the Corellium settings directly from the repository .env, with optional process overrides and no exports.
    @classmethod
    def from_env(cls, env_path: Path | None = None) -> CorelliumClient:
        api_url = (env_value(API_URL_ENV, env_path) or "").strip()
        token = (env_value(API_TOKEN_ENV, env_path) or "").strip()
        missing = [name for name, value in ((API_URL_ENV, api_url), (API_TOKEN_ENV, token)) if not value]
        if missing:
            raise CorelliumError(f"{' and '.join(missing)} must be set in the repository .env file to use a Corellium device.")
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
        return CorelliumInstance.from_json(self._request("GET", f"/v1/instances/{_segment(instance_id)}") or {})

    # Ask Corellium to power the instance on.
    def start_instance(self, instance_id: str) -> None:
        self._request("POST", f"/v1/instances/{_segment(instance_id)}/start", {})

    # Ask Corellium to resume a paused instance.
    def unpause_instance(self, instance_id: str) -> None:
        self._request("POST", f"/v1/instances/{_segment(instance_id)}/unpause", {})

    # Ask Corellium to power the instance off.
    def stop_instance(self, instance_id: str) -> None:
        self._request("POST", f"/v1/instances/{_segment(instance_id)}/stop", {})

    # List only snapshots belonging to this exact instance.
    def list_snapshots(self, instance_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/instances/{_segment(instance_id)}/snapshots") or []

    # Read a snapshot through its instance-scoped endpoint.
    def get_snapshot(self, instance_id: str, snapshot_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/instances/{_segment(instance_id)}/snapshots/{_segment(snapshot_id)}") or {}

    # Submit one restore request; callers persist intent first and never blindly replay it.
    def restore_snapshot(self, instance_id: str, snapshot_id: str) -> None:
        self._request("POST", f"/v1/instances/{_segment(instance_id)}/snapshots/{_segment(snapshot_id)}/restore")

    # Retrieve original-size image bytes using the same authentication and errors as JSON calls.
    def screenshot(self, instance_id: str, *, format: str = "png", timeout: float = 30, poll: float = 1) -> bytes:
        if format not in {"png", "jpeg"}:
            raise ValueError("Screenshot format must be png or jpeg")
        path = f"/v1/instances/{_segment(instance_id)}/screenshot.{format}"
        return retry_read(self, lambda api: api._request("GET", path, raw=True), polling_deadline(timeout, poll), poll)

    # Send separate press and release events because some deployments ignore batched taps.
    def touch(self, instance_id: str, x: int, y: int) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (x, y)):
            raise ValueError("Touch coordinates must be nonnegative pixel integers")
        path = f"/v1/instances/{_segment(instance_id)}/input"
        release = [{"buttons": [], "wait": 100}]
        try:
            self._request("POST", path, [{"buttons": ["finger"], "position": [[x, y]], "wait": 0}])
        except BaseException:
            # Attempt the release, but report the press failure rather than a secondary release failure.
            try:
                self._request("POST", path, release)
            except Exception as exc:
                logger.warning("Could not release touch on Corellium instance %s: %s", instance_id, exc)
            raise
        self._request("POST", path, release)

    # Send one API request and return its decoded JSON body, or None when it has none.
    def _request(self, method: str, path: str, body: Any = None, *, raw: bool = False) -> Any:
        url = f"{self.api_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "image/png, image/jpeg" if raw else "application/json"}
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
            raise CorelliumError(f"Corellium {method} {path} failed with HTTP {exc.code}: {detail or exc.reason}",
                                 transient=exc.code in {408, 429, 500, 502, 503, 504}) from exc
        except (urllib.error.URLError, OSError) as exc:
            logger.debug("corellium: %s %s unreachable: %s", method, path, exc, exc_info=True)
            raise CorelliumError(f"Corellium API not reachable at {safe_url(self.api_url)}: {exc}", transient=True) from exc
        if raw:
            return payload
        if not payload.strip():
            return None
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise CorelliumError(f"Corellium {method} {path} did not return JSON") from exc


# Encode opaque identifiers so they cannot change an API path.
def _segment(value: str) -> str:
    if not value:
        raise ValueError("Corellium identity cannot be empty")
    return urllib.parse.quote(value, safe="")


# Reject unbounded or nonsensical polling settings before contacting the API.
def polling_deadline(timeout: float, poll: float) -> float:
    if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
           for value in (timeout, poll)) or timeout <= 0 or poll <= 0:
        raise ValueError("timeout and poll must be positive and finite")
    return time.monotonic() + timeout


# Bound each HTTP call by the remaining operation budget without mutating a shared client.
def bounded_call(client: CorelliumClient, operation, deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CorelliumError("Corellium operation deadline expired")
    bounded = copy.copy(client)
    bounded.timeout = min(getattr(client, "timeout", remaining), remaining)
    return operation(bounded)


# Retry read-only requests on transient transport/server failures within one deadline.
def retry_read(client: CorelliumClient, operation, deadline: float, poll: float):
    last_error = None
    while True:
        if last_error is not None and time.monotonic() >= deadline:
            raise last_error
        try:
            return bounded_call(client, operation, deadline)
        except CorelliumError as exc:
            if not exc.transient or time.monotonic() >= deadline:
                raise
            last_error = exc
            time.sleep(min(poll, max(0, deadline - time.monotonic())))


# Retry a status read that keeps the client's full request timeout even when the wait's deadline is near or past, so a
# wait that outlasts its budget reports the observed state instead of a read cut short; it overruns by at most one timeout.
def retry_status_read(client: CorelliumClient, operation, deadline: float, poll: float):
    return retry_read(client, operation, max(deadline, time.monotonic() + client.timeout), poll)


# Wait for a stable instance state and idle task, retrying temporary API outages.
def wait_instance_state(client: CorelliumClient, instance_id: str, states: set[str], *,
                        timeout: float = DEFAULT_BOOT_TIMEOUT_SECONDS, poll: float = POLL_SECONDS,
                        deadline: float | None = None) -> CorelliumInstance:
    deadline = deadline if deadline is not None else polling_deadline(timeout, poll)
    while True:
        instance = retry_status_read(client, lambda api: api.get_instance(instance_id), deadline, poll)
        if instance.id != instance_id:
            raise CorelliumError(f"Corellium returned a different instance identity for {instance_id}")
        if instance.state in states and instance.task_state in {"none", ""}:
            return instance
        if instance.state in {"error", "deleted", "deleting"}:
            raise CorelliumError(f"Corellium instance {instance_id} entered {instance.state}")
        if time.monotonic() >= deadline:
            raise CorelliumError(f"Corellium instance {instance_id} did not reach {sorted(states)} "
                                 f"(last {instance.state}, task {instance.task_state})")
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


# Reconcile power requests with observed state, retrying ambiguous transient failures safely.
def ensure_instance_state(client: CorelliumClient, instance_id: str, target: str, *,
                          timeout: float = DEFAULT_BOOT_TIMEOUT_SECONDS, poll: float = POLL_SECONDS) -> CorelliumInstance:
    if target not in {"on", "off"}:
        raise ValueError("Power target must be on or off")
    deadline = polling_deadline(timeout, poll)
    while True:
        instance = retry_status_read(client, lambda api: api.get_instance(instance_id), deadline, poll)
        if instance.id != instance_id:
            raise CorelliumError(f"Corellium returned a different instance identity for {instance_id}")
        if instance.state == target and instance.task_state in {"none", ""}:
            return instance
        if instance.state in {"deleted", "deleting"} or (instance.state == "error" and target == "on"):
            raise CorelliumError(f"Corellium instance {instance_id} entered {instance.state}")
        if time.monotonic() >= deadline:
            raise CorelliumError(f"Corellium instance {instance_id} did not reach {[target]} "
                                 f"(last {instance.state}, task {instance.task_state})")
        if instance.task_state in {"none", ""}:
            operation = None
            if target == "off" and instance.state not in TRANSITIONAL_STATES and instance.state != "off":
                operation = "stop_instance"
            elif target == "on" and instance.state == "off":
                operation = "start_instance"
            elif target == "on" and instance.state == "paused":
                operation = "unpause_instance"
            if operation:
                try:
                    bounded_call(client, lambda api: getattr(api, operation)(instance_id), deadline)
                except CorelliumError as exc:
                    if not exc.transient:
                        raise
        time.sleep(min(poll, max(0, deadline - time.monotonic())))


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

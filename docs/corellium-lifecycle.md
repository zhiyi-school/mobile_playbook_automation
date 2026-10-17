# Shared Corellium lifecycle

The shared package in `mobile_playbook/platforms/corellium/` provides VM
selection, baseline restoration, durable ownership and confirmed shutdown.
It imports no Android connection/configuration classes or Appium drivers.
The existing authenticated REST client also retrieves PNG/JPEG screenshots and
sends touch input. Platform adapters own debugging approval and device readiness;
the Android adapter is described in [Android role connections](#android-role-connections).

CLI, API and worker runs use the single-device integration described in
[Android configuration](android/configuration.md#corellium-virtual-device);
they do not call this baseline lifecycle. Physical-device behavior does not
depend on it.

## Resolve before claiming

`selection.resolve_roles()` accepts only required roles (`non_rooted`, `rooted`),
each with a `RoleSelection(project, instance="", os_version="")`. It makes
read-only API calls. An empty role mapping makes no API calls.

Supply an explicit automation inventory keyed by **exact instance ID**. Each
`AutomationBaseline` declares the approved platform, rooted state, exact OS
version/build, and exactly one of `snapshot_id` or `snapshot_name`. Creating this
manifest is an operator assertion that this specific baseline was prepared and
validated for that image. The snapshot API does not always expose OS/build
metadata, so availability alone cannot establish compatibility.

Selection requires the correct project, platform, rooted state, optional OS
selector and inventory registration. Automatic selection considers idle running,
paused and off instances, in that order. Candidates with invalid baselines are
skipped. Transient API outages fail after the retry budget rather than causing a
device switch. Explicit pins never fall back. Ambiguous project/instance names
require IDs, including names that collide with another instance's ID.

Snapshots must belong to the selected instance, be fully created and have no
active task. Available platform/model/OS/build/rooted metadata must match.
Names resolve uniquely within that instance; ID configuration never falls back
to a name. Immutable `ResolvedDevice` objects retain both IDs; preparation never
selects again. Non-rooted-only plans do not need rooted VM availability.

```python
from mobile_playbook.platforms.corellium.client import CorelliumClient
from mobile_playbook.platforms.corellium.claims import ClaimStore
from mobile_playbook.platforms.corellium.lifecycle import CorelliumLifecycle
from mobile_playbook.platforms.corellium.selection import (
    AutomationBaseline, RoleSelection, resolve_roles,
)

client = CorelliumClient.from_env()
devices = resolve_roles(
    client,
    {"non_rooted": RoleSelection(project="automation", instance="stock-vm-id")},
    {"stock-vm-id": AutomationBaseline(
        platform="android", rooted=False, os_version="16.0.0",
        osbuild="r2 user", snapshot_id="stock-prepared-snapshot-id",
    )},
)
store = ClaimStore(client.api_url)
lifecycle = CorelliumLifecycle(client, store)
with lifecycle.session("unique-run-id", devices) as claims:
    # Connect a platform adapter to these exact claimed IDs here.
    # Retrieve evidence and close platform sessions before leaving this block.
    running = claims.prepared["non_rooted"]
    adb_port = running.adb_port
```

`claims.devices` keeps the identities resolved before preparation, which are
often powered-off records without connection details. `claims.prepared` holds
each role's instance record read after it reached `on`, including its ADB port
and service IP.

For direct per-role cleanup outcomes, call `store.claim_all()`, then
`lifecycle.prepare(claims)` inside a `try`, and `lifecycle.cleanup(claims)` in
its `finally`. Preparation must be inside that block because it can fail before
a platform device client exists. The `session()` context handles this ordering.
The example describes the shared API, not a new assessment entry point.

## Exclusive durable ownership

`claim_all()` takes nonblocking kernel locks in sorted instance-ID order, then
records the whole claim set in one synchronous SQLite transaction. A failed
second lock or pending owner rolls back every new claim and releases its locks
before preparation. Records include run, role, instance/baseline IDs, manifest,
owner UUID, hostname, machine ID, PID and preparation/cleanup phase.

The default store is host-wide at `~/.local/state/mobile-playbook/corellium/`,
outside prunable work/report data. Its location is owned by
`common.storage_paths.corellium_state_dir()`. The optional `CORELLIUM_STATE_DIR`
override must be an absolute path exported in the **process environment**. A
value in the repository `.env` is rejected: the CLI and workers load the whole
file while the API reads only allowlisted keys, so a `.env` value would give
them different stores. Export the same value, or none, for every controlling
process (shells and launchd `EnvironmentVariables` alike). All controlling
processes must use the same OS account/shared store and canonical API URL,
including across checkouts. `/api` and `/api/v1` normalize to the same namespace. Coordination
requires a local POSIX filesystem supporting `flock` and SQLite; it is not a
distributed lock between automation hosts.

Locks stay held for the owner's lifetime. Process exit drops kernel locks but
keeps database records. Forked children close their inherited copies of the
lock handles, so a child outliving its parent never keeps a claim locked. Lock presence proves an active owner regardless of row
age; there is no lease-expiry override. Losing a `ClaimSet` leaves cleanup pending.
`detach()` drops local handles without deleting durable ownership; never call it
while device operations remain in progress. Do not delete the database or lock
files to bypass ownership. Corrupt/unreadable state fails closed.

`pending()` reads unresolved ownership. `history(run_id)` retains ordered events
and exact role/instance/baseline/manifest identities after successful release.
A previously claimed run ID cannot acquire a replacement plan; a new assessment
attempt uses a fresh run ID.

## Preparation, cleanup and recovery

Preparation revalidates the exact claimed instance/project and baseline, confirms
the VM off, persists `restore_requested`, submits one restore, waits for the
restore to be reported busy and then idle, then starts/resumes the same VM. Repeated
preparation on an already prepared claim does not restore/start again. Partially
attempted preparation requires cleanup. Runs never create/delete snapshots.

The restore endpoint returns `204 Accepted` without an operation ID, and an idle
VM looks the same before a restore starts as after it ends. Completion therefore
requires seeing the restore in progress first: a busy instance `taskState`, the
`restoring` instance state or a busy snapshot `status.task`. Only an idle reading
after that counts as complete, matching Corellium's own SDK, which waits for
`taskState` to leave `none` and then return to it. A restore never reported busy
before the restore budget expires fails preparation without starting the VM.
Snapshot task failures and instance error states also fail preparation.

Snapshot compatibility compares the snapshot `model` with the instance `model`
field; `flavorName` is a display label and is not used.

Reads retry transient transport failures and HTTP 408/429/500/502/503/504.
Power retries reconcile observed state. Monotonic deadlines bound operations;
individual HTTP timeouts are capped by the remaining budget. Permanent errors
fail promptly. `LifecyclePolicy` configures independent restore/power budgets and
a poll interval; budgets and intervals must be positive and finite, so polling
never loops without a pause. Screenshot reads have bounded retries. Touch sends
separate press/release requests and attempts release even after a press failure,
reporting the press failure if the release fails too.

Restore POSTs and touch presses are not blindly replayed after ambiguous errors.
Corellium provides no documented restore idempotency key. Persisting intent first
ensures at most one submission per claimed run, even after a lost reply/crash;
successful preparation restores exactly once. An uncertain submission fails the
run and requires cleanup instead of risking another restore.

Cleanup attempts every VM independently, confirms `off` with an idle task,
persists confirmation, then releases that instance. Outages/timeouts retain
`cleanup_pending` and block reuse. Cleanup covers setup/test failures,
`KeyboardInterrupt` and `SystemExit`; forced termination relies on recovery.
Per-role cleanup outcomes remain separate from security-test verdicts.

`recover()` locks each pending instance using the claim lock, rereads ownership,
checks its UUID, and confirms shutdown before release. Active owners are skipped
without API calls. Stale scans cannot stop newly claimed devices. Recovery is
independent and idempotent: invoking it after connectivity returns retries pending
cleanup. It never restores, starts, installs or selects.

Claims record the owning machine's ID (the macOS `IOPlatformUUID`, else
`/etc/machine-id`, else the hostname), which survives hostname changes. Recovery
reports a claim made on another machine as `foreign_host` and leaves it untouched,
because a lock acquired on this host says nothing about an owner elsewhere. Run
recovery on the machine that made the claim.

Maintain original, active prepared and previous prepared snapshots explicitly.
Update manifests only after qualifying replacements; another VM needs its own
snapshot ID even when baseline names match.

## Android role connections

The Android adapter connects claimed, prepared instances by role and routes each
risk to its role. It lives in `mobile_playbook/platforms/android/`; the shared
package never imports it.

**Risk routing.** `device_roles.risk_role()` reads each risk's existing `requires`
and `requires_device` metadata. A risk that lists `root` in `requires` runs on the
`rooted` device; any other device risk runs on the `non_rooted` device, and a risk
without a device has no role. A `requires` value that is not a list of names,
repeats a name, or asks for root without a device, and a non-boolean
`requires_device`, are rejected. `AndroidPlatformRunner.required_device_roles()`
derives the roles a run needs from its selected apps and risks before anything is
claimed, so a non-rooted-only run never needs the rooted VM and a static-only run
needs neither. Risks keep their planned order; the runner routes each one to its
role's device. A missing role fails that risk with a clear error and is never
replaced by the other role's device. A single physical or single-instance
Corellium device still runs every risk, as before.

**Connecting.** `AndroidPlatformRunner.connect_role_devices()` starts Appium when
needed and calls `device/corellium_roles.connect_claimed()` with the claim's
`devices` and `prepared` instances. Each prepared instance must be the claimed ID,
be on and have the role's rooted state. Its adb address is the services address
with the reported adb port (or a per-role `adb_address`/`adb_port` override), and
two roles never share an address. `device/adb_connection.connect_ready()` then:

- reads `adb devices` and tells `device`, `unauthorized`, `offline` and absent
  apart;
- connects an absent device, disconnects and reconnects an offline one, and fails
  after a bounded number of attempts;
- treats `unauthorized` as a debugging prompt only after a short grace period,
  because adb briefly reports it during the key handshake;
- waits for `sys.boot_completed`, then requires `ro.build.type` to match the role:
  `userdebug` and `eng` builds are rooted, `user` builds are not.

If a later role fails, every role's adb address is disconnected and the error
propagates; the caller's lifecycle cleanup still shuts the VMs down. The result is
an `AndroidDevicePool` holding each role's exact instance ID, serial and client.
Releasing the pool quits sessions and disconnects adb; powering off stays with
the lifecycle.

**Debugging approval.** The preferred baseline already authorizes this host's
stable adb key (`$ANDROID_USER_HOME/adbkey.pub`, else `~/.android/adbkey.pub`), so
no prompt appears. When adb stays unauthorized, `device/debugging_prompt.py`
takes a PNG screenshot through the shared client, reads its dimensions from the
PNG header, and recognises words with Tesseract (`tesseract … --psm 11 tsv`).
The screen counts as the supported prompt only when it shows exactly one "Allow
USB debugging" title, the "RSA key fingerprint" message and exactly one Allow
button in a button row below the title. The tap goes to the centre of that
button in the screenshot's own pixel coordinates, in portrait or landscape,
through the shared touch input. adb must then list the device as ready and booted
within the approval budget.

An unfamiliar screen (polled until the prompt budget expires), a recogniser
failure, a screenshot or touch failure, or an approval timeout fails the
connection. The screenshot and a JSON description (instance, serial, reason,
dimensions, orientation, recognised lines, tap point and host key fingerprint) go
to `debug-authorization/` in the run folder. Recognition matches the stock
English prompt text; an image is supported only after its prompt has been
approved this way on the actual image.

**Appium recovery.** When Appium becomes unreachable, the runner quits the role
sessions, restarts Appium and reconnects every role's adb to its own serial,
rechecking boot and root state and approving the prompt again if needed. Before
each risk, a role whose adb is no longer `device` is reconnected the same way.
Recovery returns the same pool: it never selects, restores, reinstalls, starts,
stops or releases a device. The single-instance integration likewise reconnects
adb to the instance it already uses instead of choosing one again.

## Tests

```bash
python -m pytest -q tests/platforms/corellium
python -m pytest -q tests/platforms/android/test_device_roles.py \
  tests/platforms/android/test_runner_roles.py tests/platforms/android/device/test_adb_connection.py \
  tests/platforms/android/device/test_debugging_prompt.py tests/platforms/android/device/test_pool.py
```

Tests use fake APIs, synthetic snapshots, temporary stores and separate Python
processes. The shared pytest setup points `CORELLIUM_STATE_DIR` at each session's
temporary directory, so no test touches the host store. Coverage includes
selection/compatibility, off VMs, deterministic multi-device claims, failed second
claims, process exclusion/termination, forked children, persisted intent, restore
progress and completion, startup/resume, outages, shutdown timeouts, independent
cleanup, stale and foreign-host recovery and active-owner protection. A fresh
interpreter blocks Android/Appium imports while loading every shared module.

The Android tests use a synthetic adb server, synthetic PNG headers and recognised
words, and fake Corellium clients that fail on any lifecycle call. They cover role
derivation and malformed declarations, routing in plan order through the shared
runner, missing roles without substitution, adb states and bounded reconnection,
boot and root-state checks, recognised and unfamiliar prompts in both
orientations, approval timeouts with diagnostics, and Appium recovery on the same
instances without preparation. These checks do not qualify actual Android images
or debugging dialogs.

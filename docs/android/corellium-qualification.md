# Corellium Image Qualification

Qualification checks the baseline role lifecycle against real Corellium Android
images. The synthetic test suite proves the lifecycle's logic. It cannot show
that an actual image restores, boots, accepts this host's adb key, shows the
expected debugging prompt or installs the intended build within its budgets.
Use this procedure for each new instance, image or replacement baseline before
a run relies on it. The lifecycle itself is described in
[shared Corellium lifecycle](../corellium-lifecycle.md#runs-installs-and-readiness).

Every step except the read-only checks restores a snapshot, which discards
whatever the VM held since that snapshot was taken. Run qualification only
on the automation VMs listed in `device.corellium.baselines`, and only when
no one is using them by hand.

## Before starting

- `configs/android.yaml` uses the
  [baseline role form](configuration.md#baseline-role-devices), with each role
  pinned by exact instance ID and each baseline given by exact `snapshot_id`.
  The two VMs need separate snapshot IDs.
- Each baseline was prepared from the
  [baseline checklist](configuration.md#baseline-role-devices) on its own VM.
- `CORELLIUM_API_URL` and `CORELLIUM_API_TOKEN` are in the repository `.env`,
  and this host is on the project's VPN. The VPN carries traffic only while at
  least one VM in the project is on, so with both role VMs off, adb cannot reach
  a VM that has just started until the VPN comes back. Keep another VM in the
  project on during qualification, or run single-role scenarios while the other
  role's VM stays on.
- `adb`, Appium and the tools the selected risks need are installed. `tesseract`
  is needed only when an image shows the debugging prompt.
- `ANDROID_HOME` (or `ANDROID_SDK_ROOT`) is set in the environment that starts
  each run. A run that restarts Appium passes on its own environment, and
  UiAutomator2 sessions fail without the SDK path.
- Each app under test has its APK in `artifacts/intake/android/apks/`.
- Both role VMs are off in Corellium, and no claim is pending:

  ```bash
  python -c "from mobile_playbook.platforms.corellium.claims import ClaimStore; from mobile_playbook.platforms.corellium.client import CorelliumClient; print(ClaimStore(CorelliumClient.from_env().api_url).pending())"
  ```

  This prints `[]` when nothing is pending. Clear a pending claim through recovery
  (see [Recovery](#recovery)), never by deleting the claim store.

## Read-only readiness

```bash
python -c "from mobile_playbook.platforms.android.config import load_config; from mobile_playbook.platforms.android.baseline_devices import role_readiness; print(role_readiness(load_config('configs/android.yaml'), ('non_rooted', 'rooted')))"
```

Each role should read `startable` while its VM is off. Confirm in Corellium that
neither VM changed state and that the pending-claims check still prints `[]`.
`GET /config/android/apps/<APP_ID>/provisioning` reports the same result per app
through the API.

## Scenarios

Start each scenario with both VMs off and no pending claims. Each run writes
`events.jsonl` in `artifacts/reports/<RUN_TIMESTAMP>/`. The events named below
come from that file.

| Scenario | Command | Pass when |
| --- | --- | --- |
| Non-rooted-only run | `python -m mobile_playbook run --platform android --config configs/android.yaml --risks <NON_ROOT_RISK_ID>` | Only `non_rooted` appears in `device_claimed`, `device_prepared` and `device_cleanup`. The rooted VM shows no activity in Corellium. |
| Rooted-only run | `... run ... --risks <ROOT_RISK_ID>` | Only `rooted` appears in those events. The non-rooted VM shows no activity. |
| Mixed run | `... run ... --risks <NON_ROOT_RISK_ID>,<ROOT_RISK_ID>` | Each role has one `device_prepared` event. Each risk's `report.json` names its role under `metadata.device`. Both roles end `off`. |
| Standalone install | `python -m mobile_playbook install-app --config configs/android.yaml --apps <APP_ID>`, then the same command with `--role rooted` | Each install reports `installed` and then confirms that VM off. The next run restores the VM and installs the build again. |
| Appium recovery | With `device.appium_auto_start` enabled, stop the Appium process during a run's device risk | The run logs one `appium_recovery` event and continues on the same instance. `device_prepared` and `app_installed` are not repeated. |
| Cancellation | Press Ctrl-C during a run's device risk | Every claimed VM is confirmed off and the pending-claims check prints `[]`. |

A run uses the rooted VM only when a selected risk lists `root` in its `requires`.
`python -m mobile_playbook list-risks --platform android` prints each risk's
`requires`. If none does, rooted-only and mixed
runs cannot be qualified. Qualify the rooted VM's restore, adb connection,
installation and shutdown with `install-app --role rooted` instead.

For every run and install, also check:

- `app_installed` names the role, instance and `artifact_sha256`. The digest
  matches `shasum -a 256` of the intake APK, and `version` and `build_number`
  match the APK.
- Evidence files referenced by each `report.json` exist under the run folder
  on this host.
- `device_cleanup` reads `off` for every claimed role, and Corellium shows each
  VM off.
- The screen captured by a device risk shows the app itself. An app that needs
  Google Play services stops at an error dialog on a baseline without them, and
  that risk's verdict then describes the dialog rather than the app.

## Debugging approval

A baseline that authorizes this host's adb key never shows the prompt. Record
that result and leave prompt approval unqualified for that image. When a prompt
does appear, the run approves it or saves a screenshot and a JSON description
under `debug-authorization/` in the run folder (see
[Android role connections](../corellium-lifecycle.md#android-role-connections)).
Prompt approval is qualified for an image only after a run has approved the
prompt on that image and adb reached `device` with Android booted. An unfamiliar
screen must fail the run with diagnostics, and both VMs must still be confirmed off.

## Recovery

Stop a run process abruptly with `kill -9 <PID>` while a device risk is running.
The pending-claims check then lists that run's claims. Recover them with:

```bash
python -c "from mobile_playbook.platforms.corellium.lifecycle import recover_abandoned; print(recover_abandoned())"
```

Each claim should report `off`, and the pending-claims check should print `[]`.
The API server and the assessment worker run the same recovery on their own
(see [operations](../operations.md#corellium-vms-awaiting-cleanup)).

## Recording a qualification

For each VM, record its instance ID and role, its `os_version` and `osbuild`,
the snapshot ID and the qualification date. Also record whether a prompt
appeared and was approved, the scenarios that passed and the observed restore
and boot times. Note any limitation, such as a scenario that could not run. Update
`device.corellium.baselines` only after a replacement baseline has qualified.
Keep the previous prepared baseline until the new one has passed real runs.

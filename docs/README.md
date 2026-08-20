# Documentation

Documentation is split by platform, since iOS and Android use different tooling, device connections, and risks.

- [iOS](ios/README.md): device/Appium/XCUITest setup, IPA configuration, iOS risks, the manual LocalKeyboard server, and iOS reports/troubleshooting.
- [Android](android/README.md): ADB/Appium setup, Android configuration, Android risks, and Android reports/troubleshooting.
- [Architecture And Technology](architecture.md): how a run is executed end-to-end, and what libraries/tools each part of the framework depends on, across both platforms.
- [HTTP API](api.md): running `python -m mobile_playbook.api` to trigger runs and read reports over HTTP, for a dashboard or any other external caller.

Both platforms share the same CLI (`python -m mobile_playbook ...`), the same run orchestration, and the same top-level report layout under `reports/<run_timestamp>/`. See the platform-specific "Reports And Troubleshooting" pages for the parts that differ.

## Running Both Platforms Together

`run` always targets one platform (`--platform ios` or `--platform android`) and one config file. To run both in a single command, use `run-all`, which takes both config paths and runs the two platforms concurrently in one process:

```bash
python -m mobile_playbook run-all --ios-config configs/ios.yaml --android-config configs/android.yaml --apps parking,lifesg --out reports
```

`run-all` is built entirely on top of the existing `run` flow for each platform — it does not change how either platform runs or reports on its own. Each platform still writes its own `reports/<run_timestamp>/ios/...` or `.../android/...` tree, so reports stay separate. See the [top-level README](../README.md#common-commands) for the full caveat on same-second run-timestamp collisions.

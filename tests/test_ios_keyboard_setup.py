from __future__ import annotations

from pathlib import Path

import pytest

from mobile_playbook.platforms.ios import keyboard_setup

DISPLAY_NAME = "LocalKeyboard"


class FakeSwitch:
    def __init__(self, values):
        self.values = list(values)
        self.reads = 0

    def get_attribute(self, name: str) -> str:
        assert name == "value"
        value = self.values[min(self.reads, len(self.values) - 1)]
        self.reads += 1
        return value


class FakeDevice:
    """Records every navigation call; `present` names the labels the screen currently offers."""

    def __init__(self, present=(), switch_values=("1",), missing_labels=(), sheet=()):
        self.present = set(present)
        # Labels the Add New Keyboard sheet offers; they appear only once it is open.
        self.sheet = set(sheet)
        self.switch = FakeSwitch(switch_values)
        self.missing_labels = set(missing_labels)
        self.activated: list[str] = []
        # 4 == foreground.
        self.app_state = 4
        self.taps: list[list[str]] = []
        self.row_taps: list[list[str]] = []
        self.switch_taps: list[list[str]] = []
        self.back_taps = 0
        self.diagnostics: list[tuple[Path, str]] = []

    def activate_app(self, bundle_id: str) -> None:
        self.activated.append(bundle_id)

    def query_app_state(self, bundle_id: str) -> int:
        return self.app_state

    def has_label(self, labels, timeout) -> bool:
        return any(label in self.present for label in labels)

    def tap_navigation_back(self, timeout) -> None:
        self.back_taps += 1

    def tap_label(self, labels, timeout) -> None:
        if any(label in self.missing_labels for label in labels):
            raise RuntimeError(f"no element for {labels}")
        self.taps.append(list(labels))

    def tap_row_label(self, labels, timeout) -> None:
        self.row_taps.append(list(labels))
        self.tap_label(labels, timeout)
        if labels == keyboard_setup.ADD_NEW_KEYBOARD_LABELS:
            self.present |= self.sheet

    def find_switch_by_label(self, labels, timeout) -> FakeSwitch:
        return self.switch

    def tap_switch_by_label(self, labels, timeout) -> None:
        self.switch_taps.append(list(labels))

    def save_diagnostics(self, report_dir: Path, prefix: str) -> dict:
        self.diagnostics.append((report_dir, prefix))
        return {"screenshot": f"{prefix}.png", "page_source": f"{prefix}.xml"}


def _config(**overrides) -> dict:
    config = {"mode": "appium_ui", "keyboard_display_name": DISPLAY_NAME, "navigation_timeout_seconds": 1}
    config.update(overrides)
    return config


def _settings_root(*extra) -> FakeDevice:
    return FakeDevice(present={"Settings", "General", *extra})


@pytest.mark.parametrize("mode", [None, "", "disabled", "manual", "appium-ui"])
def test_any_mode_other_than_appium_ui_does_nothing(mode, tmp_path):
    device = _settings_root()

    result = keyboard_setup.prepare_custom_keyboard(device, _config(mode=mode), tmp_path)

    assert result["status"] == "SKIPPED"
    assert device.activated == []
    assert device.taps == []


def test_a_padded_mode_is_still_recognised(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    assert keyboard_setup.prepare_custom_keyboard(device, _config(mode=" appium_ui "), tmp_path)["status"] == "READY"


def test_an_absent_keyboard_is_added_and_given_full_access(tmp_path):
    device = _settings_root()
    device.sheet = {DISPLAY_NAME}
    device.switch = FakeSwitch(["0", "1"])

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert result["status"] == "READY"
    assert result["keyboard_action"] == "added"
    assert result["full_access"] is True
    assert device.taps == [
        ["General"],
        ["Keyboard", "Keyboards"],
        ["Keyboards"],
        keyboard_setup.ADD_NEW_KEYBOARD_LABELS,
        [DISPLAY_NAME],
        [DISPLAY_NAME],
        ["Allow"],
    ]
    assert device.switch_taps == [keyboard_setup.FULL_ACCESS_LABELS]

    # Only the alert's Allow button is tapped without the row restriction.
    assert device.row_taps == device.taps[:-1]


def test_a_keyboard_already_in_the_list_is_not_added_again(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert result["status"] == "READY"
    assert result["keyboard_action"] == "already_added"
    assert keyboard_setup.ADD_NEW_KEYBOARD_LABELS not in device.taps


def test_a_keyboard_that_could_not_be_added_is_not_recorded_as_added(tmp_path):
    device = _settings_root()

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert "keyboard_action" not in exc.value.state
    assert exc.value.state["status"] == "KEYBOARD_SETUP_FAILED"


def test_full_access_already_on_is_left_alone(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert result["full_access"] is True
    assert device.switch_taps == []


def test_a_switch_that_stays_off_fails_after_three_attempts(tmp_path):
    device = _settings_root(DISPLAY_NAME)
    device.switch = FakeSwitch(["0"])

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert exc.value.status == "KEYBOARD_SETUP_FAILED"
    assert len(device.switch_taps) == 3
    assert exc.value.state["status"] == "KEYBOARD_SETUP_FAILED"
    assert DISPLAY_NAME in exc.value.state["error"]
    assert exc.value.state["diagnostics"] == {
        "screenshot": "keyboard_setup_failed.png",
        "page_source": "keyboard_setup_failed.xml",
    }
    assert device.diagnostics == [(tmp_path, "keyboard_setup_failed")]


def test_a_missing_display_name_fails_before_any_navigation(tmp_path):
    device = _settings_root()

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(keyboard_display_name="  "), tmp_path)

    assert "keyboard_display_names is required" in exc.value.state["error"]
    assert device.activated == []


def test_full_access_can_be_waived(tmp_path):
    device = _settings_root(DISPLAY_NAME)
    device.switch = FakeSwitch(["0"])

    result = keyboard_setup.prepare_custom_keyboard(device, _config(require_full_access=False), tmp_path)

    assert result["status"] == "READY"
    assert "full_access" not in result
    assert device.switch_taps == []


def test_a_deep_settings_page_is_walked_back_to_the_root(tmp_path):
    class Nested(FakeDevice):
        def has_label(self, labels, timeout):
            if self.back_taps < 3 and labels == ["Settings"]:
                return False
            return super().has_label(labels, timeout)

    device = Nested(present={"Settings", "General", DISPLAY_NAME})

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert device.back_taps == 3
    assert result["status"] == "READY"


def test_a_settings_page_that_never_comes_back_is_reported(tmp_path):
    device = FakeDevice(present={"General"})

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert "Settings root page" in exc.value.state["error"]
    assert device.back_taps == 8


def test_a_missing_allow_prompt_does_not_stop_the_switch_from_being_confirmed(tmp_path):
    device = _settings_root(DISPLAY_NAME)
    device.switch = FakeSwitch(["0", "1"])
    device.missing_labels = {"Allow"}

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert result["full_access"] is True
    assert device.switch_taps == [keyboard_setup.FULL_ACCESS_LABELS]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", " On "])
def test_every_truthy_switch_value_counts_as_on(value):
    assert keyboard_setup._on(value) is True


@pytest.mark.parametrize("value", ["0", "false", "off", "", None, "yes"])
def test_every_other_switch_value_counts_as_off(value):
    assert keyboard_setup._on(value) is False


ALTERNATE_NAME = "Local Keyboard"
BOTH_NAMES = [ALTERNATE_NAME, DISPLAY_NAME]


def test_a_name_already_in_the_keyboards_list_is_never_added_again(tmp_path):
    device = _settings_root(DISPLAY_NAME)
    device.sheet = {ALTERNATE_NAME}

    result = keyboard_setup.prepare_custom_keyboard(
        device, _config(keyboard_display_names=BOTH_NAMES), tmp_path
    )

    assert result["keyboard_action"] == "already_added"
    assert result["keyboard_display_name"] == DISPLAY_NAME
    assert keyboard_setup.ADD_NEW_KEYBOARD_LABELS not in device.row_taps
    assert keyboard_setup.CANCEL_LABELS not in device.taps


def test_a_name_offered_only_in_the_sheet_is_added_under_that_name(tmp_path):
    device = _settings_root()
    device.sheet = {ALTERNATE_NAME}

    result = keyboard_setup.prepare_custom_keyboard(
        device, _config(keyboard_display_names=BOTH_NAMES), tmp_path
    )

    assert result["keyboard_action"] == "added"
    assert result["keyboard_display_name"] == ALTERNATE_NAME
    assert keyboard_setup.ADD_NEW_KEYBOARD_LABELS in device.row_taps
    assert keyboard_setup.CANCEL_LABELS not in device.taps


def test_the_first_listed_name_wins_when_several_are_offered(tmp_path):
    device = _settings_root()
    device.sheet = set(BOTH_NAMES)

    result = keyboard_setup.prepare_custom_keyboard(
        device, _config(keyboard_display_names=BOTH_NAMES), tmp_path
    )

    assert result["keyboard_display_name"] == ALTERNATE_NAME


def test_a_name_in_neither_place_closes_the_sheet_and_names_the_candidates(tmp_path):
    device = _settings_root()

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(keyboard_display_names=BOTH_NAMES), tmp_path)

    error = exc.value.state["error"]
    assert ALTERNATE_NAME in error and DISPLAY_NAME in error
    assert "not registered on this device" in error
    assert keyboard_setup.CANCEL_LABELS in device.taps
    assert "keyboard_action" not in exc.value.state
    assert exc.value.state["keyboard_display_names"] == BOTH_NAMES


def test_a_cancel_that_is_not_there_does_not_mask_the_real_failure(tmp_path):
    device = _settings_root()
    device.missing_labels = {"Cancel"}

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(keyboard_display_names=BOTH_NAMES), tmp_path)

    assert "not registered on this device" in exc.value.state["error"]


def test_the_singular_key_is_still_accepted(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    result = keyboard_setup.prepare_custom_keyboard(
        device, {"mode": "appium_ui", "keyboard_display_name": DISPLAY_NAME, "navigation_timeout_seconds": 1}, tmp_path
    )

    assert result["keyboard_action"] == "already_added"
    assert result["keyboard_display_name"] == DISPLAY_NAME


def test_a_single_name_given_as_a_plain_string_is_accepted(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    result = keyboard_setup.prepare_custom_keyboard(
        device, _config(keyboard_display_names=DISPLAY_NAME), tmp_path
    )

    assert result["keyboard_display_name"] == DISPLAY_NAME


def test_blank_and_padded_names_are_cleaned_up(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    result = keyboard_setup.prepare_custom_keyboard(
        device, _config(keyboard_display_names=["   ", f"  {DISPLAY_NAME}  ", ""]), tmp_path
    )

    assert result["keyboard_display_names"] == [DISPLAY_NAME]


def test_a_probe_timeout_is_used_for_the_lookups_not_the_navigation_timeout(tmp_path):
    seen: list[float] = []

    class Timing(FakeDevice):
        def has_label(self, labels, timeout):
            seen.append(timeout)
            return super().has_label(labels, timeout)

    device = Timing(present={"Settings", "General", DISPLAY_NAME})

    keyboard_setup.prepare_custom_keyboard(
        device, _config(keyboard_display_names=[DISPLAY_NAME], probe_timeout_seconds=0.5), tmp_path
    )

    # The Settings-root checks use the navigation timeout; the name lookup uses the probe.
    assert 0.5 in seen
    assert 1 in seen


EXTENSION_BUNDLE_ID = "com.example.LocalKeyboard.4228qcqtj9.KeyboardExtension"
COMPOSITE_LABEL = "Local Keyboard — LocalKeyboard"


def test_the_extension_bundle_id_is_tried_before_any_display_name():
    names = keyboard_setup._display_names(
        {
            "keyboard_extension_bundle_id": EXTENSION_BUNDLE_ID,
            "keyboard_display_names": [COMPOSITE_LABEL, ALTERNATE_NAME, DISPLAY_NAME],
        }
    )

    assert names[0] == EXTENSION_BUNDLE_ID
    assert names == [EXTENSION_BUNDLE_ID, COMPOSITE_LABEL, ALTERNATE_NAME, DISPLAY_NAME]


def test_a_bundle_id_repeated_in_the_display_names_is_not_tried_twice():
    names = keyboard_setup._display_names(
        {
            "keyboard_extension_bundle_id": EXTENSION_BUNDLE_ID,
            "keyboard_display_names": [EXTENSION_BUNDLE_ID, DISPLAY_NAME, DISPLAY_NAME],
        }
    )

    assert names == [EXTENSION_BUNDLE_ID, DISPLAY_NAME]


@pytest.mark.parametrize("bundle_id", [None, "", "   "])
def test_no_bundle_id_leaves_the_display_names_as_they_were(bundle_id):
    config = {"keyboard_extension_bundle_id": bundle_id, "keyboard_display_names": [DISPLAY_NAME]}

    assert keyboard_setup._display_names(config) == [DISPLAY_NAME]


def test_the_bundle_id_is_used_even_when_no_display_name_is_configured():
    assert keyboard_setup._display_names({"keyboard_extension_bundle_id": EXTENSION_BUNDLE_ID}) == [
        EXTENSION_BUNDLE_ID
    ]


class RowDevice(FakeDevice):
    """Matches a row on label, name or value, the way the client's iOS predicate does."""

    def __init__(self, rows: list[dict[str, str]], **kwargs):
        super().__init__(present=("Settings", "General"), **kwargs)
        self.rows = rows

    def has_label(self, labels, timeout) -> bool:
        if any(label in self.present for label in labels):
            return True
        return any(
            row.get(key) == label for row in self.rows for key in ("label", "name", "value") for label in labels
        )


def test_a_row_whose_name_is_the_bundle_id_matches_despite_its_em_dash_label(tmp_path):
    row = {"label": COMPOSITE_LABEL, "name": EXTENSION_BUNDLE_ID}
    device = RowDevice([row])

    result = keyboard_setup.prepare_custom_keyboard(
        device,
        _config(
            keyboard_extension_bundle_id=EXTENSION_BUNDLE_ID,
            keyboard_display_names=[DISPLAY_NAME],
        ),
        tmp_path,
    )

    assert result["keyboard_action"] == "already_added"
    assert result["keyboard_display_name"] == EXTENSION_BUNDLE_ID
    assert keyboard_setup.ADD_NEW_KEYBOARD_LABELS not in device.row_taps
    assert [EXTENSION_BUNDLE_ID] in device.row_taps


def test_a_composite_label_alone_is_missed_without_the_bundle_id(tmp_path):
    device = RowDevice([{"label": COMPOSITE_LABEL, "name": EXTENSION_BUNDLE_ID}])

    with pytest.raises(keyboard_setup.KeyboardSetupError):
        keyboard_setup.prepare_custom_keyboard(
            device, _config(keyboard_display_names=[DISPLAY_NAME]), tmp_path
        )


def test_the_stage_reached_is_recorded_on_success(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    assert keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)["stage"] == "complete"


def test_the_stage_that_failed_is_carried_into_the_error_state(tmp_path):
    device = _settings_root(DISPLAY_NAME)
    device.switch = FakeSwitch(["0"])

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert exc.value.state["stage"] == "enable_full_access"


def test_a_keyboard_that_is_registered_nowhere_reports_the_add_stage(tmp_path):
    device = _settings_root()

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert exc.value.state["stage"] == "add_keyboard"


def test_a_navigation_failure_reports_the_stage_it_was_in(tmp_path):
    device = FakeDevice(present={"General"})

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert exc.value.state["stage"] == "open_keyboards_list"


def test_settings_being_in_the_foreground_is_recorded(tmp_path):
    device = _settings_root(DISPLAY_NAME)

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert result["settings_foreground"] is True


def test_a_background_settings_app_is_reopened_before_giving_up(tmp_path):
    class Recovering(FakeDevice):
        def query_app_state(self, bundle_id: str) -> int:
            # Backgrounded on the first look, foreground once reopened.
            self.app_state = 4
            return 1 if len(self.activated) <= 2 else 4

    device = Recovering(present={"Settings", "General", DISPLAY_NAME})

    result = keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert result["settings_foreground"] is True
    assert result["status"] == "READY"
    assert device.activated.count(keyboard_setup.SETTINGS_BUNDLE_ID) >= 2


def test_a_settings_app_that_never_comes_forward_fails_with_the_reason(tmp_path):
    device = _settings_root(DISPLAY_NAME)
    device.app_state = 1

    with pytest.raises(keyboard_setup.KeyboardSetupError) as exc:
        keyboard_setup.prepare_custom_keyboard(device, _config(), tmp_path)

    assert "not in the foreground" in exc.value.state["error"]
    assert exc.value.state["settings_foreground"] is False
    assert exc.value.state["stage"] == "open_keyboards_list"

import base64
import hashlib
import json
import subprocess

import pytest

from mobile_playbook.platforms.android.device import debugging_prompt
from mobile_playbook.platforms.android.device.adb import AdbClient
from mobile_playbook.platforms.android.device.adb_connection import AdbConnectPolicy, connect_ready
from mobile_playbook.platforms.android.device.debugging_prompt import (
    DebuggingAuthorizationError,
    DebuggingAuthorizer,
    RecognitionError,
    TesseractWords,
    UnrecognizedPrompt,
    Word,
    host_adb_key_fingerprint,
    locate_allow,
    parse_tsv,
    png_size,
)
from mobile_playbook.platforms.corellium.client import CorelliumError
from tests.platforms.android.prompt_fakes import line, png, prompt_words

SERIAL = "10.11.1.1:5001"
INSTANCE = "stock-vm"


def test_png_size_reads_the_header_and_rejects_other_images():
    assert png_size(png(1080, 2400)) == (1080, 2400)
    with pytest.raises(UnrecognizedPrompt, match="not a PNG"):
        png_size(b"\xff\xd8\xff\xe0 jpeg")


def test_tesseract_tsv_rows_become_words_with_boxes_and_lines():
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t1080\t2400\t-1\t\n"
        "5\t1\t2\t1\t1\t1\t100\t900\t120\t40\t96.1\tAllow\n"
        "5\t1\t2\t1\t1\t2\t240\t900\t80\t40\t95.0\tUSB\n"
        "5\t1\t3\t1\t1\t1\t5\t5\t5\t5\t-1\t \n"
    )

    assert parse_tsv(tsv) == [
        Word("Allow", 100, 900, 120, 40, (1, 2, 1, 1)),
        Word("USB", 240, 900, 80, 40, (1, 2, 1, 1)),
    ]


def test_allow_is_located_in_the_portrait_prompt_from_its_actual_box():
    match = locate_allow(prompt_words(), 1080, 2400)

    assert (match.x, match.y, match.width, match.height, match.orientation) == (850, 1250, 1080, 2400, "portrait")


def test_allow_is_located_in_a_landscape_screenshot_using_its_dimensions():
    words = [
        *line(1, 200, "Allow", "USB", "debugging?", left=700),
        *line(2, 280, "The", "computer's", "RSA", "key", "fingerprint", "is:", left=700),
        *line(5, 600, "Cancel", "Allow", left=1500),
    ]

    match = locate_allow(words, 2400, 1080)

    assert (match.x, match.y, match.orientation) == (1500 + 140 + 60, 620, "landscape")


@pytest.mark.parametrize(
    ("words", "reason"),
    [
        (line(1, 900, "Allow", "Chrome", "to", "access", "photos?") + line(5, 1200, "Allow"), "title"),
        (line(1, 900, "Allow", "USB", "debugging?") + line(5, 1200, "Allow"), "RSA key fingerprint"),
        (prompt_words()[:-1], "no Allow button"),
        (prompt_words() + [Word("Allow", 900, 1300, 100, 40, (1, 1, 1, 6))], "more than one Allow"),
        (prompt_words(button_top=850), "no Allow button"),
        (prompt_words() + line(9, 1400, "Allow", "USB", "debugging"), "more than once"),
    ],
)
def test_unfamiliar_screens_are_not_treated_as_the_prompt(words, reason):
    with pytest.raises(UnrecognizedPrompt, match=reason):
        locate_allow(words, 1080, 2400)


def test_a_button_outside_the_screenshot_is_rejected():
    with pytest.raises(UnrecognizedPrompt, match="outside the screenshot"):
        locate_allow(prompt_words(allow_left=1100), 1080, 2400)


def test_the_host_key_fingerprint_matches_androids_md5_display(monkeypatch, tmp_path):
    blob = b"synthetic adb public key"
    (tmp_path / "adbkey.pub").write_text(base64.b64encode(blob).decode() + " user@host\n")
    monkeypatch.setenv("ANDROID_USER_HOME", str(tmp_path))
    digest = hashlib.md5(blob).hexdigest().upper()

    assert host_adb_key_fingerprint() == ":".join(digest[index : index + 2] for index in range(0, 32, 2))
    monkeypatch.setenv("ANDROID_USER_HOME", str(tmp_path / "missing"))
    assert host_adb_key_fingerprint() is None


def test_tesseract_is_asked_for_word_boxes(monkeypatch, tmp_path):
    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="level\ttext\n", stderr="")

    monkeypatch.setattr(debugging_prompt.subprocess, "run", run)

    assert TesseractWords().words(tmp_path / "shot.png") == []
    assert seen[0][-1] == "tsv" and "--psm" in seen[0]
    monkeypatch.setattr(debugging_prompt.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("tesseract")))
    with pytest.raises(RecognitionError, match="Could not run Tesseract"):
        TesseractWords().words(tmp_path / "shot.png")


class FakeCorellium:
    def __init__(self, shots, on_touch=None):
        self.shots = list(shots)
        self.on_touch = on_touch
        self.calls = []

    def screenshot(self, instance_id, format="png"):
        self.calls.append(("screenshot", instance_id, format))
        shot = self.shots.pop(0) if len(self.shots) > 1 else self.shots[0]
        if isinstance(shot, Exception):
            raise shot
        return shot

    def touch(self, instance_id, x, y):
        self.calls.append(("touch", instance_id, x, y))
        if self.on_touch:
            self.on_touch()

    def __getattr__(self, name):
        raise AssertionError(f"debugging approval must not call Corellium {name}")


class FakeRecognizer:
    def __init__(self, *results):
        self.results = list(results)

    def words(self, image):
        assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def authorizer(corellium, recognizer, tmp_path, **timeouts):
    settings = {"prompt_timeout": 0.05, "approval_timeout": 0.05, "poll": 0.001, **timeouts}
    return DebuggingAuthorizer(corellium, INSTANCE, SERIAL, diagnostics_dir=tmp_path, recognizer=recognizer, **settings)


def adb_clients():
    return AdbClient("adb"), AdbClient("adb", SERIAL)


def diagnostics(tmp_path):
    folder = tmp_path / "debug-authorization"
    return sorted(path.suffix for path in folder.iterdir()), json.loads(next(folder.glob("*.json")).read_text())


def test_the_prompt_is_approved_by_tapping_allow_and_adb_becomes_ready(adb_host, tmp_path):
    device = adb_host.add(SERIAL, state="unauthorized")
    corellium = FakeCorellium([png(1080, 2400)], on_touch=lambda: setattr(device, "state", "device"))
    approve = authorizer(corellium, FakeRecognizer([], prompt_words()), tmp_path, prompt_timeout=1)

    connect_ready("adb", SERIAL, rooted=False, policy=AdbConnectPolicy(timeout=1, unauthorized_grace=0, poll=0.001),
                  authorize=approve)

    assert [call[0] for call in corellium.calls] == ["screenshot", "screenshot", "touch"]
    assert corellium.calls[-1] == ("touch", INSTANCE, 850, 1250)
    assert not (tmp_path / "debug-authorization").exists()


def test_an_unfamiliar_screen_saves_diagnostics_and_fails_without_tapping(adb_host, tmp_path):
    adb_host.add(SERIAL, state="unauthorized")
    corellium = FakeCorellium([png(2400, 1080)])
    other = line(1, 200, "Allow", "Chrome", "to", "access", "photos?")

    with pytest.raises(DebuggingAuthorizationError, match="unfamiliar screen") as error:
        connect_ready("adb", SERIAL, rooted=False, policy=AdbConnectPolicy(timeout=1, unauthorized_grace=0, poll=0.001),
                      authorize=authorizer(corellium, FakeRecognizer(other), tmp_path))

    assert all(call[0] == "screenshot" for call in corellium.calls)
    suffixes, details = diagnostics(tmp_path)
    assert suffixes == [".json", ".png"]
    assert details["instance_id"] == INSTANCE and details["serial"] == SERIAL
    assert (details["width"], details["height"], details["orientation"]) == (2400, 1080, "landscape")
    assert details["recognized_lines"] == ["Allow Chrome to access photos?"]
    assert error.value.diagnostics.name.endswith(".json") and str(error.value.diagnostics) in str(error.value)
    assert error.value.state == "unauthorized"


def test_adb_that_stays_unauthorized_after_the_tap_times_out_with_diagnostics(adb_host, tmp_path):
    adb_host.add(SERIAL, state="unauthorized")
    corellium = FakeCorellium([png(1080, 2400)])

    with pytest.raises(DebuggingAuthorizationError, match="did not become ready"):
        authorizer(corellium, FakeRecognizer(prompt_words()), tmp_path)(*adb_clients())

    assert ("touch", INSTANCE, 850, 1250) in corellium.calls
    suffixes, details = diagnostics(tmp_path)
    assert suffixes == [".json", ".png"]
    assert details["tap"] == {"x": 850, "y": 1250}


def test_a_recognizer_failure_fails_at_once(adb_host, tmp_path):
    corellium = FakeCorellium([png(1080, 2400)])

    with pytest.raises(DebuggingAuthorizationError, match="text recognition failed: tesseract missing"):
        authorizer(corellium, FakeRecognizer(RecognitionError("tesseract missing")), tmp_path, prompt_timeout=10)(*adb_clients())

    assert corellium.calls == [("screenshot", INSTANCE, "png")]


def test_a_screenshot_failure_records_why_without_an_image(adb_host, tmp_path):
    corellium = FakeCorellium([CorelliumError("HTTP 503", transient=True)])

    with pytest.raises(DebuggingAuthorizationError, match="taking a screenshot failed"):
        authorizer(corellium, FakeRecognizer([]), tmp_path)(*adb_clients())

    suffixes, details = diagnostics(tmp_path)
    assert suffixes == [".json"] and details["screenshot"] is None


def test_an_approved_device_that_never_finishes_booting_times_out_with_diagnostics(adb_host, tmp_path):
    device = adb_host.add(SERIAL, state="unauthorized", boots_after=10**6)
    corellium = FakeCorellium([png(1080, 2400)], on_touch=lambda: setattr(device, "state", "device"))

    with pytest.raises(DebuggingAuthorizationError, match="did not become ready"):
        authorizer(corellium, FakeRecognizer(prompt_words()), tmp_path)(*adb_clients())

    assert adb_host.sent(SERIAL, "shell", "getprop", "sys.boot_completed") > 0
    _, details = diagnostics(tmp_path)
    assert details["tap"] == {"x": 850, "y": 1250}

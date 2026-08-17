from __future__ import annotations

from mobile_playbook.reporting.messages import clean_message


def test_clean_message_strips_selenium_stacktrace_block():
    raw = (
        "Message: The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible\n"
        "Stacktrace:\n"
        "UnknownError: The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible\n"
        "    at getResponseForW3CError (.../errors.js:846:36)"
    )

    assert clean_message(raw) == "The application at '/some/path/LocalKeyboard.ipa' does not exist or is not accessible"


def test_clean_message_passes_through_already_clean_text():
    text = "IPA package can be acquired and unpacked for static analysis"

    assert clean_message(text) == text


def test_clean_message_truncates_long_lines():
    text = "x" * 500

    result = clean_message(text, max_length=50)

    assert len(result) == 50
    assert result.endswith("…")


def test_clean_message_handles_empty_string():
    assert clean_message("") == ""

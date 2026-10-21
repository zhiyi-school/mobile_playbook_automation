from mobile_playbook.platforms.android.device.debugging_warning import debugging_keywords


def test_matches_keywords_case_insensitively():
    assert debugging_keywords('<Text text="USB Debugging Detected"/>') == ["debugging", "usb debugging", "debugging detected"]


def test_returns_nothing_for_an_ordinary_screen():
    assert debugging_keywords('<Text text="Welcome back"/>') == []

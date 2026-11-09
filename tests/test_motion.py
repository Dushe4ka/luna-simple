from luna.ui.motion import breath, motion_enabled


def test_breath_endpoints():
    assert breath(0.0, 4.0, "#000000", "#ffffff") in {"#7f7f7f", "#808080"}
    assert breath(1.0, 4.0, "#000000", "#ffffff") == "#ffffff"


def test_breath_floor_keeps_minimum_brightness():
    # trough of the sine: only the floor remains
    assert breath(3.0, 4.0, "#000000", "#ffffff", floor=0.5) in {"#7f7f7f", "#808080"}


def test_breath_ansi_is_static():
    assert breath(0.3, 4.0, "ansi_blue", "ansi_bright_blue") == "ansi_bright_blue"


def test_motion_enabled():
    assert motion_enabled({}) is True
    assert motion_enabled({"TEXTUAL_ANIMATIONS": "none"}) is False
    assert motion_enabled({"NO_MOTION": "1"}) is False
    assert motion_enabled({}, setting=False) is False

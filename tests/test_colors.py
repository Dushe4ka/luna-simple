from luna.ui.colors import _hex_to_rgb, _lerp_rgb, _rgb_to_hex


def test_hex_to_rgb_and_back_roundtrip():
    assert _hex_to_rgb("#8a9cff") == (0x8A, 0x9C, 0xFF)
    assert _rgb_to_hex((0x8A, 0x9C, 0xFF)) == "#8a9cff"
    for hexcolor in ("#000000", "#ffffff", "#5566a8"):
        assert _rgb_to_hex(_hex_to_rgb(hexcolor)) == hexcolor


def test_lerp_rgb_endpoints_return_exact_colors():
    a, b = (10, 20, 30), (200, 100, 0)
    assert _lerp_rgb(a, b, 0.0) == a
    assert _lerp_rgb(a, b, 1.0) == b


def test_lerp_rgb_midpoint_is_the_average():
    a, b = (0, 0, 0), (100, 200, 50)
    assert _lerp_rgb(a, b, 0.5) == (50, 100, 25)

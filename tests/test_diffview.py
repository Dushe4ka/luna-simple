from luna.ui.diffview import render_diff

DIFF = "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n y = 3"


def _style_of(text, needle):
    start = text.plain.index(needle)
    return {str(s.style) for s in text.spans if s.start <= start < s.end}


def test_render_diff_styles_each_line_kind():
    text = render_diff(DIFF)
    assert text.plain == DIFF
    assert _style_of(text, "+x = 2") == {"luna.diff.add"}
    assert _style_of(text, "-x = 1") == {"luna.diff.del"}
    assert _style_of(text, "@@ -1") == {"luna.diff.hunk"}
    assert _style_of(text, " y = 3") == {"luna.diff.ctx"}
    assert _style_of(text, "--- a.py") == {"luna.muted"}
    assert _style_of(text, "+++ a.py") == {"luna.muted"}


def test_dash_lines_inside_a_hunk_are_changes_not_headers():
    diff = "--- a.sql\n+++ a.sql\n@@ -1,2 +1,2 @@\n--- comment\n+++ added\n x"
    text = render_diff(diff)
    assert _style_of(text, "--- comment") == {"luna.diff.del"}
    assert _style_of(text, "+++ added") == {"luna.diff.add"}
    assert _style_of(text, "--- a.sql") == {"luna.muted"}


def test_palette_gives_explicit_colors():
    palette = {
        "muted": "#111111",
        "primary": "#222222",
        "add": "#333333",
        "add_bg": "#444444",
        "del": "#555555",
        "del_bg": "#666666",
    }
    text = render_diff(DIFF, palette=palette)
    assert _style_of(text, "+x = 2") == {"#333333 on #444444"}
    assert _style_of(text, "-x = 1") == {"#555555 on #666666"}
    assert _style_of(text, "@@ -1") == {"#222222"}
    assert _style_of(text, " y = 3") == {"#111111"}
    assert _style_of(text, "--- a.py") == {"#111111"}

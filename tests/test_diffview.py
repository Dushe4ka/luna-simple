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

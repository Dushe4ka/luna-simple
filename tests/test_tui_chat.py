from luna.tui.commands import filter_commands


def test_filter_commands_matches_prefix():
    results = filter_commands("/cle")
    names = [r[0] for r in results]
    assert all(n.startswith("/cle") for n in names)
    assert len(results) >= 1


def test_filter_commands_empty_slash_returns_all():
    results = filter_commands("/")
    from luna.repl.commands import HELP

    assert len(results) == len(HELP)


def test_filter_commands_no_match_returns_empty():
    assert filter_commands("/zzz-nope") == []

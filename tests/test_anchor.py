from luna.turn import anchor


def test_hash_file_returns_none_for_a_missing_file(tmp_path):
    assert anchor.hash_file(str(tmp_path), "nope.txt") is None


def test_hash_file_returns_the_same_hash_for_unchanged_content(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    first = anchor.hash_file(str(tmp_path), "a.txt")
    second = anchor.hash_file(str(tmp_path), "a.txt")
    assert first is not None
    assert first == second


def test_hash_file_changes_when_content_changes(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    before = anchor.hash_file(str(tmp_path), "a.txt")
    (tmp_path / "a.txt").write_text("world\n")
    after = anchor.hash_file(str(tmp_path), "a.txt")
    assert before != after


def test_tracker_check_is_true_when_there_is_no_anchor_yet(tmp_path):
    tracker = anchor.AnchorTracker()
    assert tracker.check(str(tmp_path), "never-read.txt") is True


def test_tracker_check_is_true_when_content_is_unchanged_since_remember(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    assert tracker.check(str(tmp_path), "a.txt") is True


def test_tracker_check_is_false_when_content_changed_since_remember(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    (tmp_path / "a.txt").write_text("changed\n")
    assert tracker.check(str(tmp_path), "a.txt") is False


def test_tracker_check_is_false_when_the_anchored_file_was_deleted(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    (tmp_path / "a.txt").unlink()
    assert tracker.check(str(tmp_path), "a.txt") is False


def test_tracker_forget_clears_the_anchor(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "a.txt")
    tracker.forget("a.txt")
    (tmp_path / "a.txt").write_text("changed\n")
    # forgotten -> no anchor -> nothing to be stale against -> True again
    assert tracker.check(str(tmp_path), "a.txt") is True


def test_tracker_check_treats_differently_spelled_paths_as_the_same(tmp_path):
    (tmp_path / "a.py").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "/a.py")
    (tmp_path / "a.py").write_text("changed\n")
    assert tracker.check(str(tmp_path), "./a.py") is False


def test_tracker_forget_under_clears_a_directory_and_its_descendants(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("hello\n")
    tracker = anchor.AnchorTracker()
    tracker.remember(str(tmp_path), "pkg/a.py")
    tracker.forget_under("pkg")
    (tmp_path / "pkg" / "a.py").write_text("changed\n")
    assert tracker.check(str(tmp_path), "pkg/a.py") is True

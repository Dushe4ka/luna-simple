import time

from luna.core.persistence import SessionIndex
from luna.core.projects import ProjectIndex


def test_folder_is_untrusted_by_default(tmp_path):
    assert ProjectIndex().is_trusted(str(tmp_path)) is False


def test_trust_is_keyed_by_resolved_path(tmp_path):
    ProjectIndex().trust(str(tmp_path / "."))
    assert ProjectIndex().is_trusted(str(tmp_path)) is True


def test_list_is_newest_opened_first(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    idx = ProjectIndex()
    idx.trust(str(a))
    time.sleep(0.01)
    idx.trust(str(b))
    time.sleep(0.01)
    idx.touch(str(a))
    assert [row.path for row in idx.list()] == [str(a.resolve()), str(b.resolve())]


def test_migration_trusts_folders_that_already_have_sessions(tmp_path):
    SessionIndex().record("t1", str(tmp_path), "hi")
    assert ProjectIndex().is_trusted(str(tmp_path)) is True


def test_migration_skips_folders_that_no_longer_exist(tmp_path):
    gone = tmp_path / "gone"
    SessionIndex().record("t1", str(gone), "hi")
    assert ProjectIndex().is_trusted(str(gone)) is False


def test_migration_runs_only_when_the_table_is_first_created(tmp_path):
    ProjectIndex()  # creates luna_projects
    new = tmp_path / "new"
    new.mkdir()
    SessionIndex().record("t2", str(new), "scripted -p run")
    assert ProjectIndex().is_trusted(str(new)) is False


def test_degrades_to_a_no_op_when_the_db_cannot_open(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocker / "cfg"))
    idx = ProjectIndex()
    assert idx.ok is False
    assert idx.is_trusted(str(tmp_path)) is False
    idx.trust(str(tmp_path))
    idx.touch(str(tmp_path))
    assert idx.list() == []

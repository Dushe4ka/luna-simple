"""Path rules ignore letter case on case-insensitive filesystems."""

import os
import unicodedata

import pytest
from luna_core.policy import Policy, Rules, case_insensitive_fs
from luna_core.tools.files import read_file
from luna_core.workspace import Workspace

# fnmatch folds case through os.path.normcase on Windows, so there every rule already
# matches without case (the stricter side); program names are folded by IS_WINDOWS.
_NT_FNMATCH = "fnmatch ignores case on Windows (os.path.normcase)"


def test_fold_case_matches_paths_without_case():
    rules = Rules(deny=["read_file:.env"], fold_case=True)
    assert rules.match("read_file", {"file_path": ".ENV"}) == "deny"


@pytest.mark.skipif(os.name == "nt", reason=_NT_FNMATCH)
def test_without_fold_case_paths_keep_their_case():
    assert Rules(deny=["read_file:.env"]).match("read_file", {"file_path": ".ENV"}) is None


@pytest.mark.skipif(os.name == "nt", reason=_NT_FNMATCH)
def test_fold_case_never_touches_execute():
    rules = Rules(deny=["execute:rm *"], fold_case=True)
    assert rules.match("execute", {"command": "RM x"}) is None  # Windows is handled elsewhere


def test_case_insensitive_fs_probe_is_consistent(tmp_path):
    (tmp_path / "Probe.txt").write_text("x", encoding="utf-8")
    assert case_insensitive_fs(tmp_path) == (tmp_path / "PROBE.TXT").exists()


def test_probe_leaves_no_files_behind(tmp_path):
    case_insensitive_fs(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="chmod does not make a directory read-only here")
def test_unwritable_root_fails_closed(tmp_path):
    tmp_path.chmod(0o500)
    try:
        if os.access(tmp_path, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        assert case_insensitive_fs(tmp_path) is True
    finally:
        tmp_path.chmod(0o700)


def test_deny_on_dot_env_holds_on_a_case_insensitive_project(tmp_path):
    if not case_insensitive_fs(tmp_path):
        pytest.skip("this filesystem tells .ENV and .env apart")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    policy = Policy(Rules(deny=["read_file:.env"]), workspace=Workspace(tmp_path))
    assert policy.check(read_file, {"file_path": ".ENV"}).action == "deny"


def test_child_policy_keeps_folding(tmp_path):
    if not case_insensitive_fs(tmp_path):
        pytest.skip("this filesystem tells .ENV and .env apart")
    policy = Policy(Rules(deny=["read_file:.env"]), workspace=Workspace(tmp_path))
    assert policy.child().check(read_file, {"file_path": ".ENV"}).action == "deny"


NFC_CAFE = unicodedata.normalize("NFC", "café")
NFD_CAFE = unicodedata.normalize("NFD", "café")
NFD_CAFE_UPPER = unicodedata.normalize("NFD", "CAFÉ")


def test_fold_case_ignores_unicode_normalization_form():
    rules = Rules(deny=[f"read_file:{NFC_CAFE}"], fold_case=True)
    assert rules.match("read_file", {"file_path": NFD_CAFE}) == "deny"
    assert rules.match("read_file", {"file_path": NFD_CAFE_UPPER}) == "deny"


def test_without_fold_case_normalization_form_is_kept():
    rules = Rules(deny=[f"read_file:{NFC_CAFE}"])
    assert rules.match("read_file", {"file_path": NFD_CAFE}) is None


def test_deny_holds_for_nfd_name_on_real_filesystem(tmp_path):
    (tmp_path / NFC_CAFE).write_text("x", encoding="utf-8")
    if not case_insensitive_fs(tmp_path) or not (tmp_path / NFD_CAFE).exists():
        pytest.skip("this filesystem does not unify normalization forms")
    policy = Policy(Rules(deny=[f"read_file:{NFC_CAFE}"]), workspace=Workspace(tmp_path))
    assert policy.check(read_file, {"file_path": NFD_CAFE}).action == "deny"
    assert policy.check(read_file, {"file_path": NFD_CAFE_UPPER}).action == "deny"

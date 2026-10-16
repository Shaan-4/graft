"""The object format is byte-compatible with git. Real git is the referee."""
import shutil
import subprocess

import pytest

from mgit.objects import ObjectError, TreeEntry, decode_commit, decode_tree, encode_tree, hash_object
from tests.conftest import commit_all, write

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(*args, cwd=None, input=None):
    return subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True,
                          check=True).stdout


@needs_git
@pytest.mark.parametrize("content", [b"", b"hello\n", b"no newline", bytes(range(256)) * 10,
                                     "हिन्दी\n".encode()])
def test_blob_ids_match_git(content):
    assert hash_object(b"blob", content) == git("hash-object", "--stdin", input=content).decode().strip()


@needs_git
def test_tree_and_commit_readable_by_git(repo, tmp_path):
    # "a.txt" next to directory "a": git sorts the directory as "a/", so
    # a.txt (0x2e) < a/ (0x2f). Plain name sorting gets this wrong.
    write(repo, "a.txt", "x\n")
    write(repo, "a/inner.txt", "y\n")
    write(repo, "a-b", "z\n")
    write(repo, "README", "hi\n")
    commit_all(repo, "first")
    write(repo, "a/inner.txt", "changed\n")
    commit_all(repo, "second")

    gd = str(repo.gitdir)
    git("--git-dir", gd, "fsck", "--strict")
    assert git("--git-dir", gd, "log", "--format=%s").decode().split() == ["second", "first"]

    # Same files staged by real git produce the identical root tree id.
    other = tmp_path.parent / "real-git"
    other.mkdir()
    for f in ("a.txt", "a-b", "README"):
        shutil.copy(repo.worktree / f, other / f)
    (other / "a").mkdir()
    shutil.copy(repo.worktree / "a/inner.txt", other / "a/inner.txt")
    git("init", "-q", cwd=other)
    git("add", ".", cwd=other)
    ours = repo.objects.commit(repo.head_commit()).tree
    assert git("write-tree", cwd=other).decode().strip() == ours


def test_tree_roundtrip():
    entries = [TreeEntry("100644", "b", "1" * 40), TreeEntry("40000", "a", "2" * 40)]
    assert sorted(decode_tree(encode_tree(entries)), key=lambda e: e.name) == \
        sorted(entries, key=lambda e: e.name)


def test_identical_content_stored_once(repo):
    write(repo, "one.txt", "same\n")
    write(repo, "two.txt", "same\n")
    commit_all(repo, "dup")
    idx = repo.read_index()
    assert idx["one.txt"]["sha"] == idx["two.txt"]["sha"]


def test_corrupted_object_is_detected(repo):
    write(repo, "f", "data\n")
    commit_all(repo, "c")
    sha = repo.read_index()["f"]["sha"]
    path = repo.gitdir / "objects" / sha[:2] / sha[2:]
    import zlib
    path.chmod(0o644)
    path.write_bytes(zlib.compress(b"blob 5\0DATA\n"))
    with pytest.raises(ObjectError, match="corrupt"):
        repo.objects.blob(sha)


def test_changing_one_file_changes_ancestors_only(repo):
    write(repo, "keep/x", "1\n")
    write(repo, "edit/y", "1\n")
    c1 = commit_all(repo, "c1")
    write(repo, "edit/y", "2\n")
    c2 = commit_all(repo, "c2")
    t1 = {e.name: e.sha for e in repo.objects.tree(repo.objects.commit(c1).tree)}
    t2 = {e.name: e.sha for e in repo.objects.tree(repo.objects.commit(c2).tree)}
    assert t1["keep"] == t2["keep"]       # untouched subtree: same id, shared
    assert t1["edit"] != t2["edit"]

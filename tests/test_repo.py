import os
import threading

import pytest

from mgit.repo import RefConflict, Repo, RepoError
from tests.conftest import commit_all, write


def test_status_lifecycle(repo):
    write(repo, "a", "1\n")
    assert repo.status().untracked == ["a"]
    repo.add(["a"])
    assert repo.status().staged == {"a": "added"}
    commit_all(repo, "c")
    assert repo.status().clean
    write(repo, "a", "2\n")
    assert repo.status().unstaged == {"a": "modified"}
    (repo.worktree / "a").unlink()
    assert repo.status().unstaged == {"a": "deleted"}
    repo.add(["a"])
    assert repo.status().staged == {"a": "deleted"}


def test_stat_cache_skips_rehash(repo, monkeypatch):
    write(repo, "a", "1\n")
    commit_all(repo, "c")
    import mgit.objects
    calls = []
    real = mgit.objects.hash_object
    monkeypatch.setattr(mgit.objects, "hash_object", lambda *a: calls.append(1) or real(*a))
    repo.status()
    assert calls == []          # size + mtime unchanged: no read, no hash


def test_ignore_file(repo):
    write(repo, ".mgitignore", "*.log\nbuild/\n")
    write(repo, "app.log", "x")
    write(repo, "build/out.bin", "x")
    write(repo, "src.py", "x")
    assert repo.status().untracked == [".mgitignore", "src.py"]


def test_branch_switch_restores_files(repo):
    write(repo, "f", "main\n")
    commit_all(repo, "c1")
    repo.switch("feat", create=True)
    write(repo, "f", "feat\n")
    write(repo, "only-on-feat/x", "x\n")
    commit_all(repo, "c2")
    repo.switch("main")
    assert (repo.worktree / "f").read_text() == "main\n"
    assert not (repo.worktree / "only-on-feat").exists()
    repo.switch("feat")
    assert (repo.worktree / "only-on-feat/x").exists()


def test_switch_refuses_to_lose_work(repo):
    write(repo, "f", "1\n")
    commit_all(repo, "c")
    repo.switch("feat", create=True)
    repo.switch("main")
    write(repo, "f", "unsaved\n")
    with pytest.raises(RepoError, match="uncommitted"):
        repo.switch("feat")


def test_add_outside_repo_rejected(repo):
    with pytest.raises(RepoError, match="outside"):
        repo.add(["../elsewhere"])


def test_ref_cas_rejects_stale_update(repo):
    write(repo, "f", "1\n")
    c1 = commit_all(repo, "c1")
    write(repo, "f", "2\n")
    c2 = commit_all(repo, "c2")
    with pytest.raises(RefConflict):
        repo.update_ref("refs/heads/main", c1, expected_old=c1)   # it's at c2 now


def test_ref_cas_under_threads(repo):
    """Many threads all read the same old value and race to move the ref.
    Exactly one may win; the rest must be rejected, never overwrite."""
    write(repo, "f", "1\n")
    old = commit_all(repo, "c")
    wins, losses = [], []
    barrier = threading.Barrier(16)

    def attempt(i):
        barrier.wait()
        try:
            repo.update_ref("refs/heads/main", f"{i:040x}", expected_old=old)
            wins.append(i)
        except RefConflict:
            losses.append(i)

    ts = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1 and len(losses) == 15
    assert repo.read_ref("refs/heads/main") == f"{wins[0]:040x}"


def test_stale_lock_is_broken(repo):
    write(repo, "f", "1\n")
    c = commit_all(repo, "c")
    lock = repo.gitdir / "refs/heads/main.lock"
    lock.write_text("crashed holder")
    os.utime(lock, (0, 0))          # pretend it's ancient
    repo.update_ref("refs/heads/main", c, expected_old=c)

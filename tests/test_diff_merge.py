import random

import pytest

from mgit import porcelain
from mgit.diff import apply, edit_distance, lcs_dp, myers, unified
from mgit.merge import merge3
from mgit.repo import RepoError
from tests.conftest import commit_all, write


@pytest.mark.parametrize("seed", range(5))
def test_myers_is_correct_and_minimal(seed):
    """For random inputs: the script rebuilds b, and its length equals the
    optimum from the O(N*M) DP. Small alphabet = many partial matches,
    which is where diff algorithms go wrong."""
    rng = random.Random(seed)
    for _ in range(600):
        a = [rng.choice("abcd") for _ in range(rng.randint(0, 25))]
        b = [rng.choice("abcd") for _ in range(rng.randint(0, 25))]
        ops = myers(a, b)
        assert apply(ops, a, b) == b
        assert edit_distance(ops) == len(a) + len(b) - 2 * lcs_dp(a, b)


def test_unified_output_matches_expected():
    out = unified(b"a\nb\nc\n", b"a\nB\nc\n", "a/f", "b/f")
    assert "@@ -1,3 +1,3 @@" in out and "-b\n" in out and "+B\n" in out


def test_merge3_clean_and_conflict():
    base = b"1\n2\n3\n4\n5\n"
    assert merge3(base, b"one\n2\n3\n4\n5\n", b"1\n2\n3\n4\nfive\n").content == \
        b"one\n2\n3\n4\nfive\n"
    m = merge3(base, b"1\nX\n3\n4\n5\n", b"1\nY\n3\n4\n5\n")
    assert m.conflicts == 1 and b"<<<<<<<" in m.content and b"X\n" in m.content


@pytest.mark.parametrize("seed", range(3))
def test_merge3_properties(seed):
    rng = random.Random(seed)
    for _ in range(300):
        base = "".join(rng.choice("abc\n") for _ in range(rng.randint(0, 30))).encode()
        side = "".join(rng.choice("abc\n") for _ in range(rng.randint(0, 30))).encode()
        # If one side didn't change anything, the result is the other side.
        assert merge3(base, side, base).content == side
        assert merge3(base, base, side).content == side
        # Same change on both sides never conflicts.
        assert merge3(base, side, side).content == side


def test_fast_forward(repo):
    write(repo, "f", "1\n")
    commit_all(repo, "c1")
    repo.switch("feat", create=True)
    write(repo, "f", "2\n")
    c2 = commit_all(repo, "c2")
    repo.switch("main")
    res = porcelain.merge(repo, "feat")
    assert res.kind == "fast-forward" and repo.head_commit() == c2
    assert (repo.worktree / "f").read_text() == "2\n"


def test_three_way_merge_creates_merge_commit(repo):
    write(repo, "f", "1\n2\n3\n")
    write(repo, "g", "x\n")
    base = commit_all(repo, "base")
    repo.switch("feat", create=True)
    write(repo, "f", "1\n2\nthree\n")
    write(repo, "new", "n\n")
    feat = commit_all(repo, "feat")
    repo.switch("main")
    write(repo, "f", "one\n2\n3\n")
    repo.worktree.joinpath("g").unlink()
    main = commit_all(repo, "main")

    assert repo.merge_base(main, feat) == base
    res = porcelain.merge(repo, "feat")
    assert res.kind == "merged"
    c = repo.objects.commit(res.commit)
    assert c.parents == [main, feat]
    assert (repo.worktree / "f").read_text() == "one\n2\nthree\n"
    assert (repo.worktree / "new").exists() and not (repo.worktree / "g").exists()
    assert repo.status().clean


def test_conflict_then_resolve(repo):
    write(repo, "f", "a\nb\nc\n")
    commit_all(repo, "base")
    repo.switch("feat", create=True)
    write(repo, "f", "a\nFEAT\nc\n")
    feat = commit_all(repo, "feat")
    repo.switch("main")
    write(repo, "f", "a\nMAIN\nc\n")
    commit_all(repo, "main")

    res = porcelain.merge(repo, "feat")
    assert res.kind == "conflict" and res.conflicts == ["f"]
    text = (repo.worktree / "f").read_text()
    assert "<<<<<<< HEAD" in text and "MAIN" in text and "FEAT" in text

    repo.add(["f"])                                   # still has markers
    with pytest.raises(RepoError, match="conflict markers"):
        porcelain.commit(repo, "oops")
    write(repo, "f", "a\nBOTH\nc\n")
    sha = commit_all(repo, "resolved")
    assert repo.objects.commit(sha).parents[1] == feat


def test_merge_base_with_criss_cross_history(repo):
    write(repo, "f", "0\n")
    root = commit_all(repo, "root")
    repo.switch("b", create=True)
    write(repo, "g", "b\n")
    commit_all(repo, "b1")
    repo.switch("main")
    write(repo, "h", "a\n")
    a1 = commit_all(repo, "a1")
    assert repo.merge_base(a1, repo.resolve("b")) == root

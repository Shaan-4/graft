"""User-facing operations that combine the lower layers."""
from __future__ import annotations

from dataclasses import dataclass, field

from .merge import merge_trees
from .repo import Repo, RepoError


@dataclass
class MergeResult:
    kind: str                       # "up-to-date" | "fast-forward" | "merged" | "conflict"
    commit: str | None = None
    conflicts: list[str] = field(default_factory=list)


def merge(repo: Repo, other_name: str) -> MergeResult:
    if (repo.gitdir / "MERGE_HEAD").exists():
        raise RepoError("a merge is in progress; resolve conflicts, `graft add`, then `graft commit`")
    ours = repo.resolve("HEAD")
    theirs = repo.resolve(other_name)
    if not repo.status().clean:
        raise RepoError("commit or discard your changes before merging")

    base = repo.merge_base(ours, theirs)
    if base == theirs:
        return MergeResult("up-to-date", ours)
    if base == ours:
        # Our branch has nothing the other lacks: just move our pointer
        # forward. No merge commit, history stays linear.
        repo.checkout_tree(theirs)
        repo.update_ref(repo.head_ref(), theirs, expected_old=ours)
        return MergeResult("fast-forward", theirs)

    s = repo.objects
    flat = lambda c: s.flatten_tree(s.commit(c).tree) if c else {}
    m = merge_trees(s, flat(base), flat(ours), flat(theirs), other_name)

    if not m.conflicts:
        _write_result(repo, m.files, ours)
        sha = repo.commit(f"Merge {other_name} into {repo.current_branch() or 'HEAD'}",
                          extra_parents=[theirs])
        return MergeResult("merged", sha)

    # Leave conflict markers in the working tree; non-conflicted results go
    # straight to the index. The user fixes the files, adds them, commits.
    _write_result(repo, m.files, ours, conflicted=set(m.conflicts))
    (repo.gitdir / "MERGE_HEAD").write_bytes((theirs + "\n").encode())
    return MergeResult("conflict", None, m.conflicts)


def _write_result(repo: Repo, files: dict, ours: str, conflicted: set = frozenset()) -> None:
    index = repo.read_index()
    s = repo.objects
    for path in list(index):
        if path not in files:
            (repo.worktree / path).unlink(missing_ok=True)
            del index[path]
    for path, (mode, sha) in files.items():
        p = repo.worktree / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(s.blob(sha))
        if path in conflicted:
            continue  # stays at our version in the index until resolved
        st = p.stat()
        index[path] = {"sha": sha, "mode": mode, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    repo.write_index(index)


def commit(repo: Repo, message: str) -> str:
    mh = repo.gitdir / "MERGE_HEAD"
    if mh.exists():
        for path, e in repo.read_index().items():
            if b"<<<<<<< " in repo.objects.blob(e["sha"]):
                raise RepoError(f"{path} still contains conflict markers")
        return repo.commit(message, extra_parents=[mh.read_text(encoding="utf-8").strip()], allow_empty=True)
    return repo.commit(message)

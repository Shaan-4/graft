"""
Repository: refs, index (staging area), working tree, commits.

Layout of .graft/ (same as .git/ where git cares, so git can read it):
  HEAD                    "ref: refs/heads/main\\n"  or a raw commit id (detached)
  refs/heads/<branch>     commit id
  refs/remotes/<r>/<b>    last known position of a branch on a remote
  objects/                see objects.py
  graft-index.json         staging area (own format; git's index is binary)
  graft.json               config: user, remotes
  MERGE_HEAD              present while a conflicted merge is being resolved
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .objects import MODE_EXEC, MODE_FILE, Commit, ObjectError, ObjectStore, encode_commit

DIR_NAME = ".graft"
LOCK_STALE_S = 60
clock = time.time   # swappable so tests/demo can control commit timestamps


class RepoError(Exception):
    pass


class RefConflict(RepoError):
    """Ref moved since the caller read it (compare-and-swap failed)."""


def _now_signature(name: str, email: str) -> str:
    ts = int(clock())
    off = -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"{name} <{email}> {ts} {sign}{off // 3600:02d}{(off % 3600) // 60:02d}"


@dataclass
class Status:
    staged: dict[str, str]      # path -> "added" | "modified" | "deleted"
    unstaged: dict[str, str]    # path -> "modified" | "deleted"
    untracked: list[str]

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged)


class Repo:
    def __init__(self, gitdir: Path, worktree: Path | None):
        self.gitdir = gitdir
        self.worktree = worktree          # None for a bare (server-side) repo
        self.objects = ObjectStore(gitdir / "objects")

    # ------------------------------------------------------------ setup

    @classmethod
    def init(cls, path: Path, bare: bool = False, branch: str = "main") -> "Repo":
        path = Path(path)
        gitdir = path if bare else path / DIR_NAME
        if (gitdir / "HEAD").exists():
            raise RepoError(f"repository already exists at {gitdir}")
        for d in ("objects", "refs/heads", "refs/tags", "refs/remotes"):
            (gitdir / d).mkdir(parents=True, exist_ok=True)
        # Bytes, not text: in text mode Windows turns "\n" into "\r\n", and
        # git rejects a HEAD or ref file with a stray "\r".
        cls._atomic_write(gitdir / "HEAD", f"ref: refs/heads/{branch}\n".encode())
        cls._atomic_write(gitdir / "graft.json", json.dumps({"remotes": {}}, indent=2).encode())
        return cls(gitdir, None if bare else path)

    @classmethod
    def find(cls, start: Path | None = None) -> "Repo":
        p = Path(start or Path.cwd()).resolve()
        for d in (p, *p.parents):
            if (d / DIR_NAME / "HEAD").is_file():
                return cls(d / DIR_NAME, d)
        raise RepoError("not inside an graft repository (run `graft init`)")

    # ------------------------------------------------------------ config

    def config(self) -> dict:
        f = self.gitdir / "graft.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"remotes": {}}

    def save_config(self, cfg: dict) -> None:
        self._atomic_write(self.gitdir / "graft.json", json.dumps(cfg, indent=2).encode())

    def identity(self) -> tuple[str, str]:
        user = self.config().get("user", {})
        name = os.getenv("GRAFT_AUTHOR_NAME") or user.get("name") or "graft user"
        email = os.getenv("GRAFT_AUTHOR_EMAIL") or user.get("email") or "user@example.com"
        return name, email

    # ------------------------------------------------------------ refs

    def head_ref(self) -> str | None:
        """Branch HEAD points at ("refs/heads/main"), or None if detached."""
        h = (self.gitdir / "HEAD").read_text(encoding="utf-8").strip()
        return h[5:] if h.startswith("ref: ") else None

    def current_branch(self) -> str | None:
        r = self.head_ref()
        return r.removeprefix("refs/heads/") if r else None

    def read_ref(self, ref: str) -> str | None:
        f = self.gitdir / ref
        return f.read_text(encoding="utf-8").strip() if f.is_file() else None

    def head_commit(self) -> str | None:
        r = self.head_ref()
        if r is None:
            return (self.gitdir / "HEAD").read_text(encoding="utf-8").strip()
        return self.read_ref(r)

    def branches(self) -> dict[str, str]:
        base = self.gitdir / "refs" / "heads"
        return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8").strip()
                for p in base.rglob("*") if p.is_file() and not p.name.endswith(".lock")}

    def all_refs(self) -> dict[str, str]:
        base = self.gitdir / "refs"
        return {"refs/" + p.relative_to(base).as_posix(): p.read_text(encoding="utf-8").strip()
                for p in base.rglob("*") if p.is_file() and not p.name.endswith(".lock")}

    def update_ref(self, ref: str, new: str, expected_old: str | None | object = ...) -> None:
        """
        Move a ref, optionally only if it still equals `expected_old`
        (compare-and-swap). This is what makes two simultaneous pushes safe:
        both read old=X, both try to set it; the second finds the ref no
        longer equals X and is rejected instead of silently overwriting.

        Mutual exclusion uses a lock file created with O_CREAT|O_EXCL, which
        the OS guarantees only one process can win. It works across processes
        and on Windows, unlike an in-memory lock. git does the same thing.
        """
        path = self.gitdir / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        self._break_stale_lock(lock)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RefConflict(f"{ref} is locked by another update; retry")
        try:
            with os.fdopen(fd, "wb") as f:
                current = self.read_ref(ref)
                if expected_old is not ... and current != expected_old:
                    raise RefConflict(
                        f"{ref} is at {current and current[:8]}, expected "
                        f"{expected_old and expected_old[:8]}")
                f.write((new + "\n").encode())
                f.flush()
                os.fsync(f.fileno())
            # The rename is the commit point: readers see the old file or the
            # new one, never a partial write.
            replace_with_retry(lock, path)
        finally:
            if lock.exists():
                lock.unlink()

    @staticmethod
    def _break_stale_lock(lock: Path) -> None:
        # A process that crashed while holding the lock would block the ref
        # forever. Treat a lock older than LOCK_STALE_S as abandoned.
        try:
            if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                lock.unlink()
        except FileNotFoundError:
            pass

    def delete_ref(self, ref: str) -> None:
        (self.gitdir / ref).unlink(missing_ok=True)

    def set_head(self, branch: str) -> None:
        self._atomic_write(self.gitdir / "HEAD", f"ref: refs/heads/{branch}\n".encode())

    def resolve(self, name: str) -> str:
        """branch / remote branch / HEAD / full or abbreviated commit id -> commit id."""
        if name == "HEAD":
            sha = self.head_commit()
            if not sha:
                raise RepoError("HEAD has no commits yet")
            return sha
        for ref in (name, f"refs/heads/{name}", f"refs/remotes/{name}", f"refs/tags/{name}"):
            sha = self.read_ref(ref)
            if sha:
                return sha
        try:
            sha = self.objects.resolve_prefix(name)
        except ObjectError as e:
            raise RepoError(f"unknown revision '{name}' ({e})")
        self.objects.commit(sha)  # must be a commit
        return sha

    # ------------------------------------------------------------ index

    def _index_path(self) -> Path:
        return self.gitdir / "graft-index.json"

    def read_index(self) -> dict[str, dict]:
        """{path: {"sha", "mode", "mtime_ns", "size"}}"""
        f = self._index_path()
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def write_index(self, index: dict[str, dict]) -> None:
        self._atomic_write(self._index_path(), json.dumps(index, sort_keys=True).encode())

    # ------------------------------------------------------------ working tree

    def _ignore_patterns(self) -> list[str]:
        f = self.worktree / ".graftignore"
        pats = [DIR_NAME]
        if f.exists():
            pats += [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
        return pats

    def _ignored(self, rel: str, pats: list[str]) -> bool:
        parts = rel.split("/")
        for p in pats:
            p = p.rstrip("/")
            if fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(x, p) for x in parts):
                return True
        return False

    def walk_worktree(self) -> list[str]:
        pats = self._ignore_patterns()
        out = []
        for dirpath, dirnames, filenames in os.walk(self.worktree):
            reldir = Path(dirpath).relative_to(self.worktree).as_posix()
            reldir = "" if reldir == "." else reldir + "/"
            dirnames[:] = sorted(d for d in dirnames if not self._ignored(reldir + d, pats))
            for fn in filenames:
                rel = reldir + fn
                if not self._ignored(rel, pats):
                    out.append(rel)
        return sorted(out)

    def _mode_of(self, p: Path) -> str:
        return MODE_EXEC if os.name != "nt" and os.access(p, os.X_OK) else MODE_FILE

    def _hash_file(self, rel: str, index_entry: dict | None, index_mtime_ns: int = 0) -> str:
        """
        Stat cache: if size and mtime match what the index recorded when the
        file was staged, assume the content is unchanged and skip reading and
        hashing it. Turns `status` on a big repo from "read every byte" into
        "stat every file". Same trick git uses.

        "Racily clean" guard: timestamps have finite resolution. If a file was
        staged and then edited again within the same tick, size and mtime can
        both still match while the content differs. Such an entry is only
        trusted if its mtime is strictly older than the index file itself;
        anything at or after that moment gets rehashed. (git's rule too.)
        """
        p = self.worktree / rel
        st = p.stat()
        if index_entry and index_entry.get("size") == st.st_size \
                and index_entry.get("mtime_ns") == st.st_mtime_ns \
                and st.st_mtime_ns < index_mtime_ns:
            return index_entry["sha"]
        from .objects import hash_object
        return hash_object(b"blob", p.read_bytes())

    def add(self, paths: list[str]) -> list[str]:
        index = self.read_index()
        files = self.walk_worktree()
        tracked = set(index)
        changed = []
        # CLI semantics: paths are relative to where you're standing, if that's
        # inside the repo. Otherwise (library use) relative to the repo root.
        cwd = Path.cwd().resolve()
        root = self.worktree.resolve()
        base = cwd if cwd == root or root in cwd.parents else root
        for spec in paths:
            target = (base / spec).resolve()
            if target != root and root not in target.parents:
                raise RepoError(f"'{spec}' is outside the repository")
            spec_rel = Path(os.path.relpath(target, root)).as_posix()
            spec_rel = "" if spec_rel == "." else spec_rel
            matched = [f for f in files if not spec_rel or f == spec_rel
                       or f.startswith(spec_rel + "/")]
            gone = [t for t in tracked if (not spec_rel or t == spec_rel
                    or t.startswith(spec_rel + "/")) and not (self.worktree / t).exists()]
            if not matched and not gone:
                raise RepoError(f"pathspec '{spec}' did not match any files")
            for rel in matched:
                p = self.worktree / rel
                st = p.stat()
                sha = self.objects.write(b"blob", p.read_bytes())
                if index.get(rel, {}).get("sha") != sha:
                    changed.append(rel)
                index[rel] = {"sha": sha, "mode": self._mode_of(p),
                              "mtime_ns": st.st_mtime_ns, "size": st.st_size}
            for rel in gone:
                index.pop(rel, None)
                changed.append(rel)
        self.write_index(index)
        return changed

    def status(self) -> Status:
        index = self.read_index()
        head = self.head_commit()
        head_files = self.objects.flatten_tree(self.objects.commit(head).tree) if head else {}

        staged = {}
        for path in set(index) | set(head_files):
            i, h = index.get(path), head_files.get(path)
            if i and not h:
                staged[path] = "added"
            elif h and not i:
                staged[path] = "deleted"
            elif i["sha"] != h[1] or i["mode"] != h[0]:
                staged[path] = "modified"

        unstaged, untracked = {}, []
        ip = self._index_path()
        index_mtime = ip.stat().st_mtime_ns if ip.exists() else 0
        on_disk = set(self.walk_worktree())
        for path in sorted(on_disk | set(index)):
            entry = index.get(path)
            if entry is None:
                untracked.append(path)
            elif path not in on_disk:
                unstaged[path] = "deleted"
            elif self._hash_file(path, entry, index_mtime) != entry["sha"]:
                unstaged[path] = "modified"
        return Status(dict(sorted(staged.items())), unstaged, untracked)

    # ------------------------------------------------------------ commits

    def commit(self, message: str, extra_parents: list[str] | None = None,
               allow_empty: bool = False) -> str:
        index = self.read_index()
        files = {p: (e["mode"], e["sha"]) for p, e in index.items()}
        tree = self.objects.write_tree_from_flat(files) if files else \
            self.objects.write(b"tree", b"")
        parent = self.head_commit()
        parents = ([parent] if parent else []) + (extra_parents or [])
        if parent and not extra_parents and not allow_empty \
                and self.objects.commit(parent).tree == tree:
            raise RepoError("nothing to commit")
        name, email = self.identity()
        sig = _now_signature(name, email)
        sha = self.objects.write(b"commit", encode_commit(
            Commit(tree, parents, sig, sig, message)))
        ref = self.head_ref()
        if ref:
            self.update_ref(ref, sha, expected_old=parent)
        else:
            self._atomic_write(self.gitdir / "HEAD", (sha + "\n").encode())
        (self.gitdir / "MERGE_HEAD").unlink(missing_ok=True)
        return sha

    def log(self, start: str, limit: int | None = None):
        """Commits reachable from start, newest first (by commit time)."""
        import heapq
        seen, heap, n = {start}, [], 0
        c = self.objects.commit(start)
        heapq.heappush(heap, (-c.timestamp, start, c))
        while heap and (limit is None or n < limit):
            _, sha, c = heapq.heappop(heap)
            yield sha, c
            n += 1
            for p in c.parents:
                if p not in seen:
                    seen.add(p)
                    pc = self.objects.commit(p)
                    heapq.heappush(heap, (-pc.timestamp, p, pc))

    # ------------------------------------------------------------ graph

    def ancestors(self, sha: str) -> set[str]:
        seen, q = {sha}, deque([sha])
        while q:
            for p in self.objects.commit(q.popleft()).parents:
                if p not in seen:
                    seen.add(p)
                    q.append(p)
        return seen

    def is_ancestor(self, a: str, b: str) -> bool:
        """Is a reachable from b? (a == b counts.)"""
        return a in self.ancestors(b)

    def merge_base(self, a: str, b: str) -> str | None:
        """
        Lowest common ancestor in the commit DAG.

        1. BFS all ancestors of a.
        2. BFS from b; every commit also in a's set is a common ancestor,
           and BFS does not need to continue past it.
        3. Drop any candidate that is an ancestor of another candidate:
           what remains are the "lowest" ones.
        With criss-cross merges there can be more than one; git then merges
        the bases recursively. This picks the most recent one instead.
        """
        anc_a = self.ancestors(a)
        candidates, seen, q = set(), {b}, deque([b])
        while q:
            x = q.popleft()
            if x in anc_a:
                candidates.add(x)
                continue
            for p in self.objects.commit(x).parents:
                if p not in seen:
                    seen.add(p)
                    q.append(p)
        if not candidates:
            return None
        lowest = [c for c in candidates
                  if not any(c != o and c in self.ancestors(o) for o in candidates)]
        return max(lowest, key=lambda s: self.objects.commit(s).timestamp)

    # ------------------------------------------------------------ checkout

    def checkout_tree(self, target_commit: str, force: bool = False) -> None:
        """Make working tree and index match target_commit."""
        st = self.status()
        if not force and not st.clean:
            dirty = sorted(set(st.staged) | set(st.unstaged))
            raise RepoError("uncommitted changes would be overwritten: " + ", ".join(dirty[:5]))
        index = self.read_index()
        target = self.objects.flatten_tree(self.objects.commit(target_commit).tree)
        for path in index:
            if path not in target:
                (self.worktree / path).unlink(missing_ok=True)
                self._prune_empty_dirs((self.worktree / path).parent)
        new_index = {}
        for path, (mode, sha) in target.items():
            p = self.worktree / path
            if index.get(path, {}).get("sha") != sha or not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(self.objects.blob(sha))
                if mode == MODE_EXEC and os.name != "nt":
                    p.chmod(0o755)
            s = p.stat()
            new_index[path] = {"sha": sha, "mode": mode, "mtime_ns": s.st_mtime_ns, "size": s.st_size}
        self.write_index(new_index)

    def _prune_empty_dirs(self, d: Path) -> None:
        while d != self.worktree and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    def switch(self, branch: str, create: bool = False) -> None:
        ref = f"refs/heads/{branch}"
        exists = self.read_ref(ref) is not None
        if create:
            if exists:
                raise RepoError(f"branch '{branch}' already exists")
            head = self.head_commit()
            if head:
                self.update_ref(ref, head, expected_old=None)
            self.set_head(branch)
            return
        if not exists:
            raise RepoError(f"no branch named '{branch}'")
        self.checkout_tree(self.read_ref(ref))
        self.set_head(branch)

    # ------------------------------------------------------------ util

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + f".tmp{os.getpid()}-{threading.get_ident()}")
        tmp.write_bytes(data)
        replace_with_retry(tmp, path)


def replace_with_retry(src: Path, dst: Path, attempts: int = 20) -> None:
    """
    os.replace, but tolerant of Windows. There, renaming onto a file that
    another thread or process has open fails with PermissionError instead of
    just working as on POSIX. The holder is a reader that closes the file
    within milliseconds, so back off briefly and retry (git for Windows does
    the same). On POSIX the first attempt always succeeds.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or i == attempts - 1:
                raise
            time.sleep(0.005 * (i + 1))

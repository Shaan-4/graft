"""
Content-addressed object storage, byte-compatible with git.

Every object is stored as   "<type> <size>\\0<content>"   and named by the
SHA-1 of exactly those bytes. Because the format matches git's, a blob hashed
here gets the same id as `git hash-object`, and real git can read a repository
this tool writes.

Three object types:
  blob    raw file contents (no filename, no permissions)
  tree    one directory: sorted entries of (mode, name, child id)
  commit  a tree id, zero or more parent ids, author, message

A tree holds its children's hashes and a commit holds its tree's hash, so the
whole snapshot is a Merkle tree: change one byte in one file and the ids of
that blob, every tree above it, and the commit all change. Two snapshots are
identical iff their root ids are equal.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

TYPES = (b"blob", b"tree", b"commit")

MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_DIR = "40000"


class ObjectError(Exception):
    pass


def frame(obj_type: bytes, content: bytes) -> bytes:
    return obj_type + b" " + str(len(content)).encode() + b"\0" + content


def hash_object(obj_type: bytes, content: bytes) -> str:
    return hashlib.sha1(frame(obj_type, content)).hexdigest()


# ---------------------------------------------------------------- trees

@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    sha: str

    @property
    def is_dir(self) -> bool:
        return self.mode == MODE_DIR


def _tree_sort_key(e: TreeEntry) -> bytes:
    # git compares directory names as if they ended in "/", so "a.txt"
    # (0x2e) sorts before directory "a" ("a/", 0x2f) sorts before "a0".
    # Getting this wrong produces a tree git rejects in fsck.
    return e.name.encode() + (b"/" if e.is_dir else b"")


def encode_tree(entries: list[TreeEntry]) -> bytes:
    out = bytearray()
    for e in sorted(entries, key=_tree_sort_key):
        out += e.mode.encode() + b" " + e.name.encode() + b"\0" + bytes.fromhex(e.sha)
    return bytes(out)


def decode_tree(data: bytes) -> list[TreeEntry]:
    entries, i = [], 0
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        mode = data[i:sp].decode()
        name = data[sp + 1:nul].decode()
        sha = data[nul + 1:nul + 21].hex()
        entries.append(TreeEntry(mode, name, sha))
        i = nul + 21
    return entries


# ---------------------------------------------------------------- commits

@dataclass
class Commit:
    tree: str
    parents: list[str]
    author: str          # "Name <email> <unix-seconds> <+hhmm>"
    committer: str
    message: str

    @property
    def timestamp(self) -> int:
        return int(self.committer.rsplit(" ", 2)[1])

    @property
    def author_name(self) -> str:
        return self.author.split(" <", 1)[0]


def encode_commit(c: Commit) -> bytes:
    lines = [f"tree {c.tree}"]
    lines += [f"parent {p}" for p in c.parents]
    lines += [f"author {c.author}", f"committer {c.committer}", ""]
    msg = c.message if c.message.endswith("\n") else c.message + "\n"
    return ("\n".join(lines) + "\n" + msg).encode()


def decode_commit(data: bytes) -> Commit:
    header, _, message = data.decode().partition("\n\n")
    tree, parents, author, committer = "", [], "", ""
    for line in header.split("\n"):
        key, _, val = line.partition(" ")
        if key == "tree":
            tree = val
        elif key == "parent":
            parents.append(val)
        elif key == "author":
            author = val
        elif key == "committer":
            committer = val
    return Commit(tree, parents, author, committer, message)


# ---------------------------------------------------------------- store

class ObjectStore:
    """objects/ab/cdef...  (first two hex chars as a directory, like git, so
    no single directory ends up with millions of entries)."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:]

    def has(self, sha: str) -> bool:
        return self._path(sha).is_file()

    def write(self, obj_type: bytes, content: bytes) -> str:
        raw = frame(obj_type, content)
        sha = hashlib.sha1(raw).hexdigest()
        path = self._path(sha)
        if path.exists():
            return sha  # content-addressed: same bytes, same file. Free dedup.
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then rename. A crash
        # mid-write leaves a stray temp file, never a half-written object
        # under a real name. Rename within one filesystem is atomic.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(zlib.compress(raw))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def read(self, sha: str) -> tuple[bytes, bytes]:
        try:
            raw = zlib.decompress(self._path(sha).read_bytes())
        except FileNotFoundError:
            raise ObjectError(f"object {sha} not found")
        # Verify on read: catches disk corruption or tampering.
        if hashlib.sha1(raw).hexdigest() != sha:
            raise ObjectError(f"object {sha} is corrupt")
        header, _, content = raw.partition(b"\0")
        obj_type, _, size = header.partition(b" ")
        if int(size) != len(content):
            raise ObjectError(f"object {sha} has wrong length")
        return obj_type, content

    def read_typed(self, sha: str, expected: bytes) -> bytes:
        t, content = self.read(sha)
        if t != expected:
            raise ObjectError(f"{sha} is a {t.decode()}, expected {expected.decode()}")
        return content

    def commit(self, sha: str) -> Commit:
        return decode_commit(self.read_typed(sha, b"commit"))

    def tree(self, sha: str) -> list[TreeEntry]:
        return decode_tree(self.read_typed(sha, b"tree"))

    def blob(self, sha: str) -> bytes:
        return self.read_typed(sha, b"blob")

    def resolve_prefix(self, prefix: str) -> str:
        if len(prefix) == 40:
            return prefix
        if len(prefix) < 4:
            raise ObjectError("abbreviated id must be at least 4 characters")
        d = self.root / prefix[:2]
        matches = [prefix[:2] + p.name for p in d.glob(prefix[2:] + "*")] if d.is_dir() else []
        if len(matches) != 1:
            raise ObjectError(f"{prefix} is {'ambiguous' if matches else 'unknown'}")
        return matches[0]

    # ------------------------------------------------------------ helpers

    def flatten_tree(self, tree_sha: str | None, prefix: str = "") -> dict[str, tuple[str, str]]:
        """tree -> {path: (mode, blob_sha)} for every file under it."""
        out: dict[str, tuple[str, str]] = {}
        if tree_sha is None:
            return out
        for e in self.tree(tree_sha):
            path = f"{prefix}{e.name}"
            if e.is_dir:
                out.update(self.flatten_tree(e.sha, path + "/"))
            else:
                out[path] = (e.mode, e.sha)
        return out

    def write_tree_from_flat(self, files: dict[str, tuple[str, str]]) -> str:
        """{path: (mode, blob_sha)} -> root tree id. Builds nested trees bottom up."""
        root: dict = {}
        for path, (mode, sha) in files.items():
            node = root
            parts = path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = (mode, sha)

        def build(node: dict) -> str:
            entries = []
            for name, child in node.items():
                if isinstance(child, dict):
                    entries.append(TreeEntry(MODE_DIR, name, build(child)))
                else:
                    entries.append(TreeEntry(child[0], name, child[1]))
            return self.write(b"tree", encode_tree(entries))

        return build(root)

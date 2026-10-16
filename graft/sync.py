"""
Push / fetch / clone over HTTP.

Protocol (deliberately small, not git's real wire protocol):

  GET  <url>/info/refs      -> {"head": "refs/heads/main", "refs": {ref: id}}
  POST <url>/upload-pack    body {"wants": [...], "haves": [...]}  -> pack
  POST <url>/receive-pack   body = pack, headers say which ref to move
                            from which old id to which new id

Negotiation, i.e. "send only what's missing": the receiver says which
commits it already has ("haves"). Because a repository always holds the full
history behind any commit it has, having commit X implies having all of X's
ancestors and every tree and blob they point to. The sender walks back from
what's wanted and stops the moment it reaches anything implied by a have.
Unchanged files have unchanged blob ids, so they are skipped too.

Pack format (all integers big-endian):
  "GRPK" | u32 version | u32 count | count x (u8 type | u32 len | bytes)
  | 20-byte SHA-1 of everything before it
then the whole thing is zlib-compressed for the wire.

The receiver never trusts object names from the sender: it recomputes every
id from the bytes. A tampered object just gets a different id, which then
fails the connectivity check because nothing references it.
"""
from __future__ import annotations

import hashlib
import json
import struct
import urllib.error
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .objects import ObjectError, ObjectStore
from .repo import Repo, RepoError

MAGIC = b"GRPK"
VERSION = 1
TYPE_CODE = {b"blob": 1, b"tree": 2, b"commit": 3}
CODE_TYPE = {v: k for k, v in TYPE_CODE.items()}
MAX_UNPACKED = 200 * 1024 * 1024   # guard against decompression bombs
ZERO = "0" * 40


class SyncError(Exception):
    pass


# ------------------------------------------------------------ object selection

def _commit_closure(store: ObjectStore, tips: list[str]) -> set[str]:
    seen, q = set(), deque(t for t in tips if store.has(t))
    seen.update(q)
    while q:
        for p in store.commit(q.popleft()).parents:
            if p not in seen:
                seen.add(p)
                q.append(p)
    return seen


def _tree_closure(store: ObjectStore, tree: str, into: set[str]) -> None:
    if tree in into:
        return
    into.add(tree)
    for e in store.tree(tree):
        if e.is_dir:
            _tree_closure(store, e.sha, into)
        else:
            into.add(e.sha)


def objects_to_send(store: ObjectStore, wants: list[str], haves: list[str]) -> list[str]:
    haves = [h for h in haves if store.has(h)]     # ignore ids we've never seen
    hidden_commits = _commit_closure(store, haves)

    # Trees/blobs at the boundary: everything the receiver's tips contain.
    known: set[str] = set()
    for h in haves:
        _tree_closure(store, store.commit(h).tree, known)

    send: list[str] = []
    sent: set[str] = set()
    q, seen = deque(), set()
    for w in wants:
        if w not in hidden_commits and w not in seen:
            seen.add(w)
            q.append(w)
    while q:
        c = q.popleft()
        commit = store.commit(c)
        send.append(c)
        _collect_tree(store, commit.tree, known, sent, send)
        for p in commit.parents:
            if p not in hidden_commits and p not in seen:
                seen.add(p)
                q.append(p)
    return send


def _collect_tree(store, tree, known, sent, send):
    if tree in known or tree in sent:
        return
    sent.add(tree)
    send.append(tree)
    for e in store.tree(tree):
        if e.is_dir:
            _collect_tree(store, e.sha, known, sent, send)
        elif e.sha not in known and e.sha not in sent:
            sent.add(e.sha)
            send.append(e.sha)


# ------------------------------------------------------------ pack encode / decode

def build_pack(store: ObjectStore, ids: list[str]) -> bytes:
    body = bytearray(MAGIC + struct.pack(">II", VERSION, len(ids)))
    for sha in ids:
        t, content = store.read(sha)
        body += struct.pack(">BI", TYPE_CODE[t], len(content)) + content
    body += hashlib.sha1(body).digest()
    return zlib.compress(bytes(body), 6)


def unpack(store: ObjectStore, wire: bytes) -> list[str]:
    d = zlib.decompressobj()
    try:
        data = d.decompress(wire, MAX_UNPACKED)
    except zlib.error:
        raise SyncError("pack is not valid zlib data")
    if d.unconsumed_tail:
        raise SyncError("pack too large")
    if len(data) < 32 or data[:4] != MAGIC:
        raise SyncError("not a pack")
    body, trailer = data[:-20], data[-20:]
    if hashlib.sha1(body).digest() != trailer:
        raise SyncError("pack checksum mismatch (corrupted in transit)")
    version, count = struct.unpack(">II", body[4:12])
    if version != VERSION:
        raise SyncError(f"unsupported pack version {version}")
    i, written = 12, []
    for _ in range(count):
        if i + 5 > len(body):
            raise SyncError("truncated pack")
        code, size = struct.unpack(">BI", body[i:i + 5])
        i += 5
        if code not in CODE_TYPE or i + size > len(body):
            raise SyncError("malformed pack entry")
        written.append(store.write(CODE_TYPE[code], body[i:i + size]))
        i += size
    if i != len(body):
        raise SyncError("trailing bytes in pack")
    return written


def check_connected(store: ObjectStore, tip: str, new_ids: set[str]) -> None:
    """
    Make sure everything reachable from `tip` exists before a ref points at
    it; otherwise the repo would have a branch you can't check out.

    Only objects that arrived in this pack need walking. Anything that was
    already here was connected when it arrived (that check ran then too),
    so by induction its whole subgraph is present. That keeps the check
    proportional to the push, not to the size of the repository.
    """
    stack, seen = [("commit", tip)], set()
    while stack:
        kind, sha = stack.pop()
        if sha in seen:
            continue
        seen.add(sha)
        if not store.has(sha):
            raise SyncError(f"missing {kind} {sha[:10]}")
        if sha not in new_ids:
            continue
        try:
            if kind == "commit":
                c = store.commit(sha)
                stack.append(("tree", c.tree))
                stack += [("commit", p) for p in c.parents]
            elif kind == "tree":
                for e in store.tree(sha):
                    stack.append(("tree" if e.is_dir else "blob", e.sha))
            else:
                store.blob(sha)
        except ObjectError as e:
            raise SyncError(str(e))


# ------------------------------------------------------------ server side

def receive(repo: Repo, wire: bytes, ref: str, old: str, new: str, force: bool) -> int:
    if not ref.startswith("refs/heads/") or ".." in ref:
        raise SyncError("can only push to refs/heads/<branch>")
    new_ids = set(unpack(repo.objects, wire))
    check_connected(repo.objects, new, new_ids)
    old_val = None if old == ZERO else old
    current = repo.read_ref(ref)
    if current and not force and not repo.is_ancestor(current, new):
        raise SyncError("non-fast-forward: pull and merge first")
    # CAS: rejected if someone else moved the ref after the client read it.
    repo.update_ref(ref, new, expected_old=old_val)
    return len(new_ids)


# ------------------------------------------------------------ client side

@dataclass
class Transfer:
    objects: int = 0
    bytes: int = 0


def _request(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    headers = dict(headers or {})
    import os
    token = os.getenv("GRAFT_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise SyncError(f"server said {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SyncError(f"could not reach {url}: {e.reason}")


def remote_url(repo: Repo, remote: str) -> str:
    url = repo.config().get("remotes", {}).get(remote)
    if not url:
        raise RepoError(f"no remote named '{remote}' (use `graft remote add`)")
    return url.rstrip("/")


def ls_remote(url: str) -> dict:
    return json.loads(_request(f"{url}/info/refs"))


def fetch(repo: Repo, remote: str = "origin") -> Transfer:
    url = remote_url(repo, remote)
    info = ls_remote(url)
    refs = {r: s for r, s in info["refs"].items() if r.startswith("refs/heads/")}
    wants = sorted({s for s in refs.values() if not repo.objects.has(s)})
    t = Transfer()
    if wants:
        haves = sorted(set(repo.all_refs().values()))
        wire = _request(f"{url}/upload-pack",
                        json.dumps({"wants": wants, "haves": haves}).encode(),
                        {"Content-Type": "application/json"})
        new_ids = set(unpack(repo.objects, wire))
        for w in wants:
            check_connected(repo.objects, w, new_ids)
        t = Transfer(len(new_ids), len(wire))
    for ref, sha in refs.items():
        branch = ref.removeprefix("refs/heads/")
        repo.update_ref(f"refs/remotes/{remote}/{branch}", sha)
    return t


def push(repo: Repo, remote: str = "origin", branch: str | None = None,
         force: bool = False) -> Transfer:
    branch = branch or repo.current_branch()
    if not branch:
        raise RepoError("detached HEAD: say which branch to push")
    local = repo.read_ref(f"refs/heads/{branch}")
    if not local:
        raise RepoError(f"no local branch '{branch}'")
    url = remote_url(repo, remote)
    refs = ls_remote(url)["refs"]
    ref = f"refs/heads/{branch}"
    remote_sha = refs.get(ref)
    if remote_sha == local:
        return Transfer()
    if remote_sha and not force:
        if not repo.objects.has(remote_sha) or not repo.is_ancestor(remote_sha, local):
            raise SyncError("rejected: remote has commits you don't (non-fast-forward). "
                            "Run `graft pull` first.")
    haves = [s for s in refs.values() if repo.objects.has(s)]
    ids = objects_to_send(repo.objects, [local], haves)
    wire = build_pack(repo.objects, ids)
    _request(f"{url}/receive-pack", wire, {
        "Content-Type": "application/x-graft-pack",
        "X-Graft-Ref": ref, "X-Graft-Old": remote_sha or ZERO, "X-Graft-New": local,
        "X-Graft-Force": "1" if force else "0",
    })
    repo.update_ref(f"refs/remotes/{remote}/{branch}", local)
    return Transfer(len(ids), len(wire))


def clone(url: str, dest: Path) -> tuple[Repo, Transfer]:
    url = url.rstrip("/")
    info = ls_remote(url)                       # fail before creating anything
    head = info.get("head", "refs/heads/main").removeprefix("refs/heads/")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise RepoError(f"destination '{dest}' is not empty")
    repo = Repo.init(dest, branch=head)
    cfg = repo.config()
    cfg["remotes"]["origin"] = url
    repo.save_config(cfg)
    t = fetch(repo, "origin")
    tip = repo.read_ref(f"refs/remotes/origin/{head}")
    if tip:
        repo.update_ref(f"refs/heads/{head}", tip)
        repo.checkout_tree(tip, force=True)
    return repo, t

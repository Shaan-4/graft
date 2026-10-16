import json
import threading
import urllib.request

import pytest

from mgit import porcelain, sync
from mgit.repo import Repo
from mgit.sync import SyncError
from tests.conftest import commit_all, write


def make_remote(r: Repo, url: str, name: str = "proj"):
    cfg = r.config()
    cfg["remotes"]["origin"] = f"{url}/{name}"
    r.save_config(cfg)


def test_push_clone_pull_roundtrip(repo, server, tmp_path_factory, monkeypatch):
    url, _ = server
    write(repo, "src/a.py", "a = 1\n")
    write(repo, "README", "hi\n")
    commit_all(repo, "first")
    make_remote(repo, url)
    t = sync.push(repo)
    assert t.objects == 5                            # commit, 2 trees, 2 blobs

    other_dir = tmp_path_factory.mktemp("clone")
    other, ct = sync.clone(f"{url}/proj", other_dir / "proj")
    assert (other.worktree / "src/a.py").read_text() == "a = 1\n"
    assert other.head_commit() == repo.head_commit()

    # Change in the clone, push, pull back into the original.
    monkeypatch.chdir(other.worktree)
    write(other, "src/a.py", "a = 2\n")
    commit_all(other, "second")
    sync.push(other)
    monkeypatch.chdir(repo.worktree)
    sync.fetch(repo)
    assert porcelain.merge(repo, "origin/main").kind == "fast-forward"
    assert (repo.worktree / "src/a.py").read_text() == "a = 2\n"


def test_incremental_push_sends_only_changed_objects(repo, server):
    url, _ = server
    import random
    rng = random.Random(0)
    for i in range(40):
        # Varied content: repeated text would compress to nothing and make
        # the byte comparison meaningless.
        lines = [f"{rng.getrandbits(64):x} {rng.random()}\n" for _ in range(50)]
        write(repo, f"pkg{i % 4}/file{i}.txt", "".join(lines))
    commit_all(repo, "big initial")
    make_remote(repo, url)
    full = sync.push(repo)

    write(repo, "pkg2/file2.txt", "edited\n")
    commit_all(repo, "small edit")
    inc = sync.push(repo)
    # new commit + root tree + pkg2 tree + one blob. Nothing else.
    assert inc.objects == 4
    assert inc.bytes < full.bytes / 5


def test_concurrent_push_one_wins(repo, server, tmp_path_factory, monkeypatch):
    """Two clones both start from the same commit and push at once.
    Exactly one is accepted; the other must be rejected, not overwrite."""
    url, _ = server
    write(repo, "f", "base\n")
    commit_all(repo, "base")
    make_remote(repo, url)
    sync.push(repo)

    clones = []
    for i in range(2):
        d = tmp_path_factory.mktemp(f"c{i}")
        c, _ = sync.clone(f"{url}/proj", d / "p")
        monkeypatch.chdir(c.worktree)
        write(c, f"from{i}", f"{i}\n")
        commit_all(c, f"change {i}")
        clones.append(c)

    barrier = threading.Barrier(2)
    results = {}

    def go(i):
        barrier.wait()
        try:
            sync.push(clones[i])
            results[i] = "ok"
        except SyncError as e:
            results[i] = str(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    oks = [i for i, r in results.items() if r == "ok"]
    assert len(oks) == 1, results
    loser = 1 - oks[0]
    assert "409" in results[loser] or "fast-forward" in results[loser]

    # The loser recovers the normal way: pull (merge), then push.
    monkeypatch.chdir(clones[loser].worktree)
    sync.fetch(clones[loser])
    assert porcelain.merge(clones[loser], "origin/main").kind == "merged"
    sync.push(clones[loser])
    final = sync.ls_remote(f"{url}/proj")["refs"]["refs/heads/main"]
    names = {p for p in clones[loser].objects.flatten_tree(
        clones[loser].objects.commit(final).tree)}
    assert {"from0", "from1"} <= names


def test_non_fast_forward_rejected(repo, server, tmp_path_factory, monkeypatch):
    url, _ = server
    write(repo, "f", "1\n")
    commit_all(repo, "c1")
    make_remote(repo, url)
    sync.push(repo)
    d = tmp_path_factory.mktemp("o")
    other, _ = sync.clone(f"{url}/proj", d / "p")
    monkeypatch.chdir(other.worktree)
    write(other, "f", "other\n")
    commit_all(other, "other")
    sync.push(other)
    monkeypatch.chdir(repo.worktree)
    write(repo, "f", "mine\n")
    commit_all(repo, "mine")
    with pytest.raises(SyncError, match="non-fast-forward"):
        sync.push(repo)


def _raw_push(url, body, old, new, token="s3cret"):
    req = urllib.request.Request(f"{url}/proj/receive-pack", data=body, method="POST", headers={
        "X-Mgit-Ref": "refs/heads/main", "X-Mgit-Old": old, "X-Mgit-New": new,
        "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_rejects_tampered_and_incomplete_packs(repo, server):
    url, repos = server
    write(repo, "f", "real\n")
    c = commit_all(repo, "c")
    ids = sync.objects_to_send(repo.objects, [c], [])

    # 1. Drop the blob: commit claims a file the server never receives.
    blob = repo.read_index()["f"]["sha"]
    status, body = _raw_push(url, sync.build_pack(repo.objects, [i for i in ids if i != blob]),
                             sync.ZERO, c)
    assert status == 400 and "missing" in body["detail"]

    # 2. Flip a byte in transit: the pack checksum catches it.
    import zlib
    raw = bytearray(zlib.decompress(sync.build_pack(repo.objects, ids)))
    raw[20] ^= 0xFF
    status, body = _raw_push(url, zlib.compress(bytes(raw)), sync.ZERO, c)
    assert status == 400 and "checksum" in body["detail"]

    # 3. Wrong token.
    status, _ = _raw_push(url, sync.build_pack(repo.objects, ids), sync.ZERO, c, token="nope")
    assert status == 401

    # Nothing above was allowed to create the branch.
    assert not (repos / "proj" / "refs/heads/main").exists()


def test_bad_repo_names_rejected(server):
    url, _ = server
    for bad in ("..", "a%2F..%2Fb", ".hidden"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{url}/{bad}/info/refs")
        assert e.value.code in (400, 404)


def test_web_ui_escapes_content(repo, server):
    url, _ = server
    write(repo, "<script>alert(1)</script>.txt", "x\n")
    commit_all(repo, "<img src=x onerror=alert(1)>")
    make_remote(repo, url)
    sync.push(repo)
    page = urllib.request.urlopen(f"{url}/proj").read().decode()
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;img src=x" in page

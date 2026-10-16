import os
import socket
import threading
import time
from pathlib import Path

import pytest

import mgit.repo as repo_mod
from mgit.repo import Repo


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("MGIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("MGIT_AUTHOR_EMAIL", "test@example.com")
    # Strictly increasing commit times so ordering is deterministic.
    t = [1_700_000_000]

    def clock():
        t[0] += 60
        return t[0]
    monkeypatch.setattr(repo_mod, "clock", clock)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Repo.init(tmp_path)


def write(r: Repo, rel: str, text: str | bytes) -> None:
    p = r.worktree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode() if isinstance(text, str) else text)


def commit_all(r: Repo, msg: str) -> str:
    from mgit import porcelain
    r.add(["."])
    return porcelain.commit(r, msg)


@pytest.fixture
def server(tmp_path_factory, monkeypatch):
    """A real HTTP server on a free port, so the client exercises actual sockets."""
    import uvicorn
    repos = tmp_path_factory.mktemp("srv")
    monkeypatch.setenv("MGIT_REPOS", str(repos))
    import importlib
    import server.app as app_mod
    importlib.reload(app_mod)
    app_mod.PUSH_TOKEN = "s3cret"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app_mod.app, port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.02)
    monkeypatch.setenv("MGIT_TOKEN", "s3cret")
    yield f"http://127.0.0.1:{port}", repos
    srv.should_exit = True
    th.join(timeout=5)

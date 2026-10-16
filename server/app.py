"""
mgit server: hosts bare repositories over HTTP, plus a read-only web UI.

Clone URL for a repo is just  https://<host>/<repo-name>  (same shape as git).
Reads are public. Pushes need `Authorization: Bearer $MGIT_PUSH_TOKEN` when
that variable is set, so a public demo can't be filled with junk.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mgit.diff import is_binary, unified
from mgit.objects import ObjectError
from mgit.repo import Repo, RefConflict, RepoError
from mgit.sync import SyncError, build_pack, objects_to_send, receive

REPOS = Path(os.getenv("MGIT_REPOS", "repos")).resolve()
PUSH_TOKEN = os.getenv("MGIT_PUSH_TOKEN")
MAX_PUSH_BYTES = int(os.getenv("MGIT_MAX_PUSH_BYTES", str(20 * 1024 * 1024)))
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")



@asynccontextmanager
async def lifespan(app):
    seed_demo()
    yield


app = FastAPI(title="mgit server", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------ helpers

def _repo_path(name: str) -> Path:
    # Whitelist, not blacklist: rules out "../", absolute paths, NUL bytes.
    if not NAME_RE.match(name) or name.startswith("."):
        raise HTTPException(400, "invalid repository name")
    return REPOS / name


def _open(name: str, must_exist: bool = True) -> Repo | None:
    p = _repo_path(name)
    if not (p / "HEAD").is_file():
        if must_exist:
            raise HTTPException(404, f"no repository '{name}'")
        return None
    return Repo(p, None)


def _check_token(request: Request) -> None:
    if PUSH_TOKEN and request.headers.get("authorization") != f"Bearer {PUSH_TOKEN}":
        raise HTTPException(401, "push requires a valid token (set MGIT_TOKEN on the client)")


# ------------------------------------------------------------ protocol

@app.get("/{name}/info/refs")
def info_refs(name: str):
    repo = _open(name, must_exist=False)
    if repo is None:
        return {"head": "refs/heads/main", "refs": {}}
    return {"head": repo.head_ref() or "refs/heads/main", "refs": repo.all_refs()}


@app.post("/{name}/upload-pack")
async def upload_pack(name: str, request: Request):
    repo = _open(name)
    body = await request.json()
    wants, haves = body.get("wants", []), body.get("haves", [])
    if not isinstance(wants, list) or not isinstance(haves, list) or len(wants) > 1000:
        raise HTTPException(400, "bad negotiation request")
    try:
        for w in wants:
            repo.objects.commit(w)
        pack = build_pack(repo.objects, objects_to_send(repo.objects, wants, haves))
    except ObjectError as e:
        raise HTTPException(404, str(e))
    return Response(pack, media_type="application/x-mgit-pack")


@app.post("/{name}/receive-pack")
async def receive_pack(name: str, request: Request):
    _check_token(request)
    path = _repo_path(name)
    declared = int(request.headers.get("content-length", "0"))
    if declared > MAX_PUSH_BYTES:
        raise HTTPException(413, "push too large")
    data = bytearray()
    async for chunk in request.stream():        # enforce the limit while reading,
        data += chunk                           # not after buffering everything
        if len(data) > MAX_PUSH_BYTES:
            raise HTTPException(413, "push too large")

    h = request.headers
    ref, old, new = h.get("x-mgit-ref", ""), h.get("x-mgit-old", ""), h.get("x-mgit-new", "")
    if not (re.fullmatch(r"[0-9a-f]{40}", old) and re.fullmatch(r"[0-9a-f]{40}", new)):
        raise HTTPException(400, "missing or malformed ref update headers")

    repo = _open(name, must_exist=False)
    if repo is None:
        REPOS.mkdir(parents=True, exist_ok=True)
        try:
            repo = Repo.init(path, bare=True, branch=ref.removeprefix("refs/heads/") or "main")
        except RepoError:          # created by a concurrent push a moment ago
            repo = Repo(path, None)
    try:
        n = receive(repo, bytes(data), ref, old, new, h.get("x-mgit-force") == "1")
    except RefConflict as e:
        raise HTTPException(409, f"rejected: {e}. Someone pushed first; pull and retry.")
    except SyncError as e:
        raise HTTPException(409 if "fast-forward" in str(e) else 400, str(e))
    return {"ok": True, "ref": ref, "new": new, "objects": n}


@app.get("/api/repos")
def api_repos():
    return [p.name for p in sorted(REPOS.glob("*")) if (p / "HEAD").is_file()] if REPOS.exists() else []


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------ web UI

CSS = """
:root{--bg:#fbfaf7;--ink:#1c1b19;--mute:#6b675f;--line:#e4e0d6;--card:#fff;--add:#e6f4ea;--del:#fdecea;
--addi:#1a7f37;--deli:#b42318;--acc:#8a4b14;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--ink:#ecebe6;--mute:#9b978d;--line:#2c2b27;--card:#1b1a17;
--add:#14301e;--del:#3a1714;--addi:#6fd38c;--deli:#ff8a7a;--acc:#e3a35f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 16px 64px}a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:22px}header .logo{font:600 18px var(--mono)}
header .crumb{color:var(--mute)}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 10px}
.mute{color:var(--mute)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.row{display:flex;gap:12px;padding:10px 14px;border-top:1px solid var(--line);align-items:baseline}.row:first-child{border-top:0}
.sha{font:12.5px var(--mono);color:var(--mute);min-width:64px}.msg{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tag{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mute)}
pre{margin:0;font:12.5px/1.5 var(--mono);overflow-x:auto}.diff .l{padding:0 14px;white-space:pre}
.diff .a{background:var(--add);color:var(--addi)}.diff .d{background:var(--del);color:var(--deli)}.diff .h{color:var(--mute);background:var(--bg)}
.file{padding:8px 14px;font:600 13px var(--mono);border-bottom:1px solid var(--line);background:var(--bg)}
code{font:13px var(--mono);background:var(--card);border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.branches{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.branches a{border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:13px}
.branches a.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
"""

e = html.escape


def _page(title: str, crumb: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body><main><header><a class="logo" href="/">mgit</a>
<span class="crumb">{crumb}</span></header>{body}</main></body></html>""")


def _when(sig: str) -> str:
    ts = int(sig.rsplit(" ", 2)[1])
    secs = int(time.time()) - ts
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            k = secs // n
            return f"{k} {unit}{'s' if k > 1 else ''} ago"
    return "just now"


@app.get("/", response_class=HTMLResponse)
def ui_home(request: Request):
    rows = []
    for name in api_repos():
        r = Repo(REPOS / name, None)
        head = r.head_commit()
        if head and r.objects.has(head):
            c = r.objects.commit(head)
            rows.append(f'<div class="row"><a href="/{e(name)}"><b>{e(name)}</b></a>'
                        f'<span class="msg mute">{e(c.message.splitlines()[0] if c.message else "")}</span>'
                        f'<span class="mute">{_when(c.committer)}</span></div>')
        else:
            rows.append(f'<div class="row"><a href="/{e(name)}"><b>{e(name)}</b></a><span class="mute">empty</span></div>')
    base = str(request.base_url).rstrip("/")
    body = f"""<h1>Repositories</h1>
<p class="mute">Hosted by a version-control system written from scratch. Object format is byte-compatible with git.</p>
<div class="card">{''.join(rows) or '<div class="row mute">No repositories yet.</div>'}</div>
<h2>Try it</h2><pre class="card" style="padding:12px 14px">pip install git+https://github.com/&lt;you&gt;/mgit
mgit clone {e(base)}/demo
cd demo
mgit log --oneline</pre>"""
    return _page("mgit", "", body)


@app.get("/{name}", response_class=HTMLResponse)
def ui_repo(name: str, branch: str | None = None):
    r = _open(name)
    branches = r.branches()
    cur = branch or r.current_branch() or next(iter(branches), "main")
    if cur not in branches:
        raise HTTPException(404, "no such branch")
    pills = "".join(f'<a class="{"on" if b == cur else ""}" href="/{e(name)}?branch={e(b)}">{e(b)}</a>'
                    for b in sorted(branches))
    rows = []
    for sha, c in r.log(branches[cur], 100):
        merge = '<span class="tag">merge</span>' if len(c.parents) > 1 else ""
        rows.append(f'<div class="row"><a class="sha" href="/{e(name)}/commit/{sha}">{sha[:8]}</a>'
                    f'<span class="msg">{e(c.message.splitlines()[0] if c.message else "")}</span>{merge}'
                    f'<span class="mute">{e(c.author_name)} · {_when(c.committer)}</span></div>')
    files = r.objects.flatten_tree(r.objects.commit(branches[cur]).tree)
    frows = "".join(f'<div class="row"><a href="/{e(name)}/blob/{sha}?path={e(p)}">{e(p)}</a></div>'
                    for p, (_, sha) in sorted(files.items()))
    body = f"""<h1>{e(name)}</h1><div class="branches">{pills}</div>
<h2>History</h2><div class="card">{''.join(rows)}</div>
<h2>Files at {e(cur)}</h2><div class="card">{frows}</div>"""
    return _page(name, e(name), body)


@app.get("/{name}/commit/{sha}", response_class=HTMLResponse)
def ui_commit(name: str, sha: str):
    r = _open(name)
    try:
        sha = r.objects.resolve_prefix(sha)
        c = r.objects.commit(sha)
    except ObjectError:
        raise HTTPException(404, "no such commit")
    parent = c.parents[0] if c.parents else None
    old = r.objects.flatten_tree(r.objects.commit(parent).tree) if parent else {}
    new = r.objects.flatten_tree(c.tree)
    blocks = []
    for p in sorted(set(old) | set(new)):
        if old.get(p) == new.get(p):
            continue
        a = r.objects.blob(old[p][1]) if p in old else b""
        b = r.objects.blob(new[p][1]) if p in new else b""
        lines = []
        for line in unified(a, b, f"a/{p}", f"b/{p}").splitlines()[2:]:
            cls = {"+": "a", "-": "d", "@": "h"}.get(line[:1], "")
            lines.append(f'<div class="l {cls}">{e(line)}</div>')
        blocks.append(f'<div class="card diff" style="margin-bottom:14px"><div class="file">{e(p)}</div>'
                      f'<pre>{"".join(lines)}</pre></div>')
    parents = " ".join(f'<a class="sha" href="/{e(name)}/commit/{p}">{p[:8]}</a>' for p in c.parents)
    body = f"""<h1>{e(c.message.splitlines()[0] if c.message else "")}</h1>
<p class="mute">{e(c.author.rsplit(" ", 2)[0])} · {_when(c.committer)}<br>
<span class="sha">{sha}</span>{(" · parents " + parents) if parents else " · root commit"}</p>
<pre class="mute" style="white-space:pre-wrap">{e(c.message.split(chr(10), 1)[1].strip() if chr(10) in c.message.strip() else "")}</pre>
<h2>Changes{" (against first parent)" if len(c.parents) > 1 else ""}</h2>
{''.join(blocks) or '<p class="mute">No file changes.</p>'}"""
    return _page(sha[:8], f'<a href="/{e(name)}">{e(name)}</a> / {sha[:8]}', body)


@app.get("/{name}/blob/{sha}", response_class=HTMLResponse)
def ui_blob(name: str, sha: str, path: str = ""):
    r = _open(name)
    try:
        data = r.objects.blob(sha)
    except ObjectError:
        raise HTTPException(404, "no such file")
    text = "(binary file)" if is_binary(data) else data.decode("utf-8", errors="replace")
    body = f'<div class="card"><div class="file">{e(path)}</div><pre style="padding:12px 14px">{e(text)}</pre></div>'
    return _page(path or sha[:8], f'<a href="/{e(name)}">{e(name)}</a> / {e(path)}', body)


# ------------------------------------------------------------ demo seed

def seed_demo():
    if os.getenv("MGIT_SEED_DEMO") != "1" or (REPOS / "demo" / "HEAD").exists():
        return
    from server.demo import build_demo
    REPOS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = build_demo(Path(tmp) / "demo")
        staging = REPOS / f".demo-{os.getpid()}"
        shutil.copytree(work.gitdir, staging)
        (staging / "mgit-index.json").unlink(missing_ok=True)
        try:
            os.replace(staging, REPOS / "demo")      # another worker may have won
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)

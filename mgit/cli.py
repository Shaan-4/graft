"""mgit command line. Run `mgit -h` for the list of commands."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import porcelain, sync
from .diff import unified
from .objects import ObjectError, decode_commit, decode_tree, hash_object
from .repo import Repo, RepoError

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _fmt_time(sig: str) -> str:
    ts, tz = sig.rsplit(" ", 2)[1:]
    sign = 1 if tz[0] == "+" else -1
    off = dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    return dt.datetime.fromtimestamp(int(ts), dt.timezone(off)).strftime("%a %b %d %H:%M %Y %z")


def cmd_init(a):
    r = Repo.init(Path(a.path))
    print(f"Initialized empty mgit repository in {r.gitdir.resolve()}")


def cmd_config(a):
    r = Repo.find()
    cfg = r.config()
    section, _, key = a.key.partition(".")
    if a.value is None:
        print(cfg.get(section, {}).get(key, ""))
    else:
        cfg.setdefault(section, {})[key] = a.value
        r.save_config(cfg)


def cmd_add(a):
    r = Repo.find()
    for p in r.add(a.paths):
        print(f"staged {p}")


def cmd_commit(a):
    r = Repo.find()
    sha = porcelain.commit(r, a.message)
    print(f"[{r.current_branch() or 'detached'} {sha[:8]}] {a.message.splitlines()[0]}")


def cmd_status(a):
    r = Repo.find()
    st = r.status()
    print(f"On branch {r.current_branch() or '(detached)'}")
    if (r.gitdir / "MERGE_HEAD").exists():
        print("Merge in progress: fix conflicts, `mgit add` them, then `mgit commit`.")
    if st.staged:
        print("\nChanges to be committed:")
        for p, s in st.staged.items():
            print(f"    {s:9} {p}")
    if st.unstaged:
        print("\nChanges not staged:")
        for p, s in st.unstaged.items():
            print(f"    {s:9} {p}")
    if st.untracked:
        print("\nUntracked files:")
        for p in st.untracked:
            print(f"    {p}")
    if st.clean and not st.untracked:
        print("nothing to commit, working tree clean")


def cmd_log(a):
    r = Repo.find()
    start = r.resolve(a.rev)
    for sha, c in r.log(start, a.n):
        if a.oneline:
            print(f"{sha[:8]} {c.message.splitlines()[0] if c.message else ''}")
        else:
            print(f"commit {sha}")
            if len(c.parents) > 1:
                print("Merge:  " + " ".join(p[:8] for p in c.parents))
            print(f"Author: {c.author.rsplit(' ', 2)[0]}")
            print(f"Date:   {_fmt_time(c.author)}\n")
            for line in c.message.rstrip("\n").splitlines():
                print(f"    {line}")
            print()


def cmd_diff(a):
    r = Repo.find()
    s = r.objects
    index = r.read_index()
    if a.revs:
        left = s.flatten_tree(s.commit(r.resolve(a.revs[0])).tree)
        if len(a.revs) > 1:
            right_files = s.flatten_tree(s.commit(r.resolve(a.revs[1])).tree)
            right = lambda p: s.blob(right_files[p][1]) if p in right_files else b""
            paths = set(left) | set(right_files)
        else:
            right = lambda p: (r.worktree / p).read_bytes() if (r.worktree / p).exists() else b""
            paths = set(left) | set(index)
        old = lambda p: s.blob(left[p][1]) if p in left else b""
    elif a.cached:
        head = r.head_commit()
        left = s.flatten_tree(s.commit(head).tree) if head else {}
        old = lambda p: s.blob(left[p][1]) if p in left else b""
        right = lambda p: s.blob(index[p]["sha"]) if p in index else b""
        paths = set(left) | set(index)
    else:
        old = lambda p: s.blob(index[p]["sha"])
        right = lambda p: (r.worktree / p).read_bytes() if (r.worktree / p).exists() else b""
        paths = set(index)
    for p in sorted(paths):
        out = unified(old(p), right(p), f"a/{p}", f"b/{p}")
        if out:
            sys.stdout.write(f"diff --mgit a/{p} b/{p}\n" + out)


def cmd_branch(a):
    r = Repo.find()
    if a.delete:
        if a.delete == r.current_branch():
            raise RepoError("cannot delete the branch you are on")
        r.delete_ref(f"refs/heads/{a.delete}")
        print(f"deleted branch {a.delete}")
    elif a.name:
        head = r.resolve("HEAD")
        r.update_ref(f"refs/heads/{a.name}", head, expected_old=None)
        print(f"created branch {a.name} at {head[:8]}")
    else:
        cur = r.current_branch()
        for b, sha in sorted(r.branches().items()):
            print(f"{'*' if b == cur else ' '} {b:20} {sha[:8]}")


def cmd_checkout(a):
    r = Repo.find()
    r.switch(a.branch, create=a.b)
    print(f"Switched to {'a new ' if a.b else ''}branch '{a.branch}'")


def cmd_merge(a):
    r = Repo.find()
    res = porcelain.merge(r, a.branch)
    if res.kind == "conflict":
        print("Automatic merge failed. Conflicts in:")
        for p in res.conflicts:
            print(f"    {p}")
        print("Fix them, `mgit add` the files, then `mgit commit`.")
        sys.exit(1)
    print({"up-to-date": "Already up to date.",
           "fast-forward": f"Fast-forward to {res.commit[:8]}",
           "merged": f"Merged {a.branch}, created {res.commit[:8]}"}[res.kind])


def cmd_cat_file(a):
    r = Repo.find()
    sha = r.objects.resolve_prefix(a.object)
    t, content = r.objects.read(sha)
    if a.t:
        print(t.decode())
    elif t == b"tree":
        for e in decode_tree(content):
            kind = "tree" if e.is_dir else "blob"
            print(f"{e.mode.zfill(6)} {kind} {e.sha}\t{e.name}")
    else:
        sys.stdout.buffer.write(content)


def cmd_hash_object(a):
    data = Path(a.file).read_bytes()
    if a.w:
        print(Repo.find().objects.write(b"blob", data))
    else:
        print(hash_object(b"blob", data))


def cmd_remote(a):
    r = Repo.find()
    cfg = r.config()
    if a.action == "add":
        cfg.setdefault("remotes", {})[a.name] = a.url
        r.save_config(cfg)
    else:
        for n, u in cfg.get("remotes", {}).items():
            print(f"{n}\t{u}")


def _kb(n):
    return f"{n / 1024:.1f} KiB" if n >= 1024 else f"{n} B"


def cmd_push(a):
    r = Repo.find()
    t = sync.push(r, a.remote, a.branch, a.force)
    print("Everything up to date" if not t.objects else
          f"Pushed {t.objects} objects ({_kb(t.bytes)}) to {a.remote}")


def cmd_fetch(a):
    r = Repo.find()
    t = sync.fetch(r, a.remote)
    print(f"Fetched {t.objects} objects ({_kb(t.bytes)})" if t.objects else "Already up to date.")


def cmd_pull(a):
    r = Repo.find()
    t = sync.fetch(r, a.remote)
    branch = a.branch or r.current_branch()
    if t.objects:
        print(f"Fetched {t.objects} objects ({_kb(t.bytes)})")
    a.branch = f"{a.remote}/{branch}"
    cmd_merge(a)


def cmd_clone(a):
    dest = Path(a.dir or a.url.rstrip("/").rsplit("/", 1)[-1])
    r, t = sync.clone(a.url, dest)
    print(f"Cloned into '{dest}' ({t.objects} objects, {_kb(t.bytes)})")


def main(argv=None):
    p = argparse.ArgumentParser(prog="mgit", description="A small git, built from scratch.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("path", nargs="?", default="."); s.set_defaults(f=cmd_init)
    s = sub.add_parser("config"); s.add_argument("key"); s.add_argument("value", nargs="?"); s.set_defaults(f=cmd_config)
    s = sub.add_parser("add"); s.add_argument("paths", nargs="+"); s.set_defaults(f=cmd_add)
    s = sub.add_parser("commit"); s.add_argument("-m", "--message", required=True); s.set_defaults(f=cmd_commit)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("log"); s.add_argument("rev", nargs="?", default="HEAD")
    s.add_argument("--oneline", action="store_true"); s.add_argument("-n", type=int); s.set_defaults(f=cmd_log)
    s = sub.add_parser("diff"); s.add_argument("revs", nargs="*")
    s.add_argument("--cached", action="store_true"); s.set_defaults(f=cmd_diff)
    s = sub.add_parser("branch"); s.add_argument("name", nargs="?")
    s.add_argument("-d", "--delete"); s.set_defaults(f=cmd_branch)
    s = sub.add_parser("checkout"); s.add_argument("branch"); s.add_argument("-b", action="store_true"); s.set_defaults(f=cmd_checkout)
    s = sub.add_parser("merge"); s.add_argument("branch"); s.set_defaults(f=cmd_merge)
    s = sub.add_parser("cat-file"); s.add_argument("object"); s.add_argument("-t", action="store_true")
    s.add_argument("-p", action="store_true"); s.set_defaults(f=cmd_cat_file)
    s = sub.add_parser("hash-object"); s.add_argument("file"); s.add_argument("-w", action="store_true"); s.set_defaults(f=cmd_hash_object)
    s = sub.add_parser("remote"); s.add_argument("action", nargs="?", choices=["add", "list"], default="list")
    s.add_argument("name", nargs="?"); s.add_argument("url", nargs="?"); s.set_defaults(f=cmd_remote)
    s = sub.add_parser("push"); s.add_argument("remote", nargs="?", default="origin")
    s.add_argument("branch", nargs="?"); s.add_argument("-f", "--force", action="store_true"); s.set_defaults(f=cmd_push)
    s = sub.add_parser("fetch"); s.add_argument("remote", nargs="?", default="origin"); s.set_defaults(f=cmd_fetch)
    s = sub.add_parser("pull"); s.add_argument("remote", nargs="?", default="origin")
    s.add_argument("branch", nargs="?"); s.set_defaults(f=cmd_pull)
    s = sub.add_parser("clone"); s.add_argument("url"); s.add_argument("dir", nargs="?"); s.set_defaults(f=cmd_clone)

    a = p.parse_args(argv)
    try:
        a.f(a)
    except (RepoError, ObjectError, sync.SyncError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
Benchmarks behind the numbers in the README. Run:  python bench/bench.py

1. Myers diff vs O(N*M) LCS dynamic programming on a real source file
   with a few edits (the common case).
2. Push transfer size: full push vs. pushing a one-file change, on a repo
   made from a real directory of Python's standard library.
3. `status` with and without the stat cache.
"""
from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graft import objects as obj_mod  # noqa: E402
from graft.diff import lcs_dp, myers  # noqa: E402
from graft.repo import Repo  # noqa: E402
from graft.sync import build_pack, objects_to_send  # noqa: E402

STDLIB = Path(os.__file__).parent


def timeit(fn, reps=3):
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def bench_diff():
    src = (STDLIB / "argparse.py").read_text().splitlines(keepends=True)
    rng = random.Random(42)
    print(f"\n## Diff: argparse.py ({len(src)} lines)\n")
    print("| edits | Myers | LCS DP | speedup |")
    print("|---|---|---|---|")
    for k in (1, 10, 50, 200):
        b = list(src)
        for _ in range(k):
            i = rng.randrange(len(b))
            b[i] = f"# edited {rng.random()}\n"
        assert len(src) + len(b) - 2 * lcs_dp(src, b) == \
            sum(1 for o in myers(src, b) if o.kind != "=")
        tm = timeit(lambda: myers(src, b))
        td = timeit(lambda: lcs_dp(src, b), reps=1)
        print(f"| {k} | {tm*1000:.1f} ms | {td*1000:.0f} ms | {td/tm:.0f}x |")


def bench_push(tmp: Path):
    tree = STDLIB / "email"
    work = tmp / "work"
    shutil.copytree(tree, work, ignore=shutil.ignore_patterns("__pycache__"))
    os.environ.setdefault("GRAFT_AUTHOR_NAME", "bench")
    os.environ.setdefault("GRAFT_AUTHOR_EMAIL", "bench@example.com")
    r = Repo.init(work)
    cwd = os.getcwd()
    os.chdir(work)
    try:
        r.add(["."])
        c1 = r.commit("import")
        full_ids = objects_to_send(r.objects, [c1], [])
        full = build_pack(r.objects, full_ids)

        target = work / "message.py"
        target.write_text(target.read_text() + "\n# one-line change\n")
        r.add(["."])
        c2 = r.commit("edit one file")
        inc_ids = objects_to_send(r.objects, [c2], [c1])
        inc = build_pack(r.objects, inc_ids)
    finally:
        os.chdir(cwd)
    nfiles = len(r.read_index())
    raw = sum(p.stat().st_size for p in work.rglob("*") if p.is_file() and ".graft" not in p.parts)
    print(f"\n## Push size: stdlib/email ({nfiles} files, {raw/1024:.0f} KiB on disk)\n")
    print("| push | objects | bytes on wire |")
    print("|---|---|---|")
    print(f"| first push (everything) | {len(full_ids)} | {len(full)/1024:.1f} KiB |")
    print(f"| after editing one file | {len(inc_ids)} | {len(inc)/1024:.1f} KiB |")
    print(f"\nincremental push is {len(full)/len(inc):.0f}x smaller")


def bench_status(tmp: Path):
    work = tmp / "status"
    work.mkdir()
    for i in range(3000):
        d = work / f"d{i % 30}"
        d.mkdir(exist_ok=True)
        (d / f"f{i}.txt").write_bytes(os.urandom(4096))
    r = Repo.init(work)
    cwd = os.getcwd()
    os.chdir(work)
    try:
        r.add(["."])
        r.commit("files")
        with_cache = timeit(r.status)
        # Disable the cache by making every index entry's mtime look stale.
        idx = r.read_index()
        for e in idx.values():
            e["mtime_ns"] = 0
        r.write_index(idx)
        without = timeit(r.status)
    finally:
        os.chdir(cwd)
    print("\n## `status` on 3,000 files (12 MiB)\n")
    print("| | time |")
    print("|---|---|")
    print(f"| stat cache (size + mtime match, skip hashing) | {with_cache*1000:.0f} ms |")
    print(f"| no cache (read + SHA-1 every file) | {without*1000:.0f} ms |")


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, {os.cpu_count()} CPUs")
    bench_diff()
    with tempfile.TemporaryDirectory() as t:
        bench_push(Path(t))
        bench_status(Path(t))

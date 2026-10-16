# Graft

A version control system written from scratch in Python, with a server you can push to and clone from.

The object format is byte-for-byte the same as git's. Blob and tree ids match `git hash-object` and `git write-tree`, and real git can open a repository graft wrote: `git fsck --strict` passes and `git log --graph` draws the history. The test suite uses real git as the referee for this.

**Live server:** `<your-render-url>` · browse the demo repo's history, merges and diffs in the browser, or clone it with the CLI.

```
pip install git+https://github.com/Shaan-4/graft
graft clone <your-render-url>/demo
cd demo
graft log --oneline
```

The client is pure standard library. No dependencies, and it works on Windows, macOS and Linux (CI runs on Ubuntu and Windows).

## What's implemented

| Area | What | Where |
|---|---|---|
| Storage | Content-addressed blobs, trees and commits (a Merkle DAG). Atomic writes via temp file + rename, hash verified on every read | `objects.py` |
| Working tree | `add`, `status`, `commit`, branches, `checkout`, `.graftignore`, a stat cache so `status` skips unchanged files | `repo.py` |
| Diff | Myers' O((N+M)·D) algorithm, unified output | `diff.py` |
| Merge | Merge base (lowest common ancestor in the commit DAG), fast-forward, 3-way merge per file and per line (diff3), conflict markers and resolution | `merge.py`, `porcelain.py` |
| Sync | `push`, `fetch`, `pull`, `clone` over HTTP; sends only objects the other side is missing | `sync.py` |
| Server | Bare repos, pack verification, atomic ref compare-and-swap, push token, web UI | `server/app.py` |

## How the interesting parts work

**Objects are named by their content.** A blob is stored as `blob <size>\0<bytes>` under the SHA-1 of exactly that string. A tree lists `(mode, name, child id)`, and a commit points at a tree and its parents. Changing one byte of one file changes that blob's id, every tree above it, and the commit, while untouched subdirectories keep their ids and are shared between commits. Two snapshots are identical iff their root ids match.

One detail that real git enforces and is easy to get wrong: trees are sorted as if directory names ended in `/`. So `a.txt` sorts before the directory `a` (because `.` < `/`), even though a plain string sort puts `a` first. `test_tree_and_commit_readable_by_git` plants exactly that case and checks the resulting tree id against git's.

**Diff.** Classic LCS by dynamic programming is O(N·M) no matter how similar the files are. Myers searches the edit graph by number of edits D: for D = 0, 1, 2, … it tracks the furthest point reachable on each diagonal, sliding along runs of matching lines for free, and stops at the first D that reaches the end. Work grows with the number of differences, not the file size squared. A property test checks on 3,000 random inputs that the script both rebuilds the target and has the same length as the DP optimum.

**Merge.** Two-way merge can't tell "we added this line" from "they deleted it". Only the common ancestor can. Each region is compared against the base: if only one side changed it, take that side; if both changed it the same way, fine; otherwise it's a conflict. The base is the lowest common ancestor in the commit DAG, found by BFS. With criss-cross history there can be several; git then merges them recursively, whereas graft picks the most recent (documented limitation).

**Sync: send only what's missing.** The receiver lists the commits it already has. Having a commit implies having its entire history and every object it points to, so the sender walks back from what's wanted and stops at anything the receiver has. Unchanged files have unchanged blob ids, so they're skipped too.

The server trusts nothing it receives:
- it recomputes every object id from the bytes (a tampered object just gets a different id),
- it checks a SHA-1 trailer over the whole pack,
- it caps decompressed size to stop zip bombs,
- it checks connectivity before moving a branch, so a branch can never point at a commit whose files are missing.

That connectivity check only walks objects in the new pack: anything already on the server was checked when it arrived, so by induction its whole history is present.

**Two people push at the same time.** Both read `main = X` and both send "move main from X to my commit". The ref update is a compare-and-swap. It takes a lock file opened with `O_CREAT | O_EXCL`, which the OS guarantees only one process can create, then checks the ref still equals X, writes the new value, `fsync`s, and atomically renames it into place. The second push finds the ref moved and gets a 409; it pulls, merges, and pushes again. This works across multiple server processes, unlike an in-memory lock, which is why the server can run several workers. A lock left behind by a crashed process is treated as abandoned after 60 seconds.

## Numbers

From `python bench/bench.py`, Python 3.13, 2-core machine.

**Diff, Myers vs LCS DP** on `argparse.py` from the standard library (2,690 lines) with k random line edits:

| edits | Myers | LCS DP | speedup |
|---|---|---|---|
| 1 | 1.1 ms | 651 ms | 576x |
| 10 | 2.7 ms | 736 ms | 274x |
| 50 | 4.3 ms | 827 ms | 191x |
| 200 | 26.9 ms | 905 ms | 34x |

The speedup shrinks as edits grow. That's the O(N·D) behaviour showing: DP's cost doesn't depend on how different the files are, Myers' does. With a single edit, trimming the common prefix and suffix does most of the work.

**Push size** for a repo built from the standard library's `email` package (30 files, 391 KiB):

| | objects | on the wire |
|---|---|---|
| first push | 33 | 98.4 KiB |
| after a one-line edit to one file | 3 | 12.9 KiB |

Only 8x smaller, because the edited file is still sent in full. Real git sends a delta against the old version, which would be a few dozen bytes. Delta compression is the obvious next step.

**`status` on 3,000 files:** 55 ms with the stat cache vs 98 ms rehashing everything. A modest win here because the OS page cache makes rereading small files cheap; it matters more for large files and cold caches.

## Running it

```
pip install -e ".[dev]"
python -m pytest -q                 # 40 tests, incl. git-compat and push races
python bench/bench.py

# server
set GRAFT_REPOS=repos                (Windows cmd; `export` on macOS/Linux)
set GRAFT_SEED_DEMO=1
uvicorn server.app:app --port 8000
```

Then `graft clone http://localhost:8000/demo`.

Pushing to a server with `GRAFT_PUSH_TOKEN` set needs the same value in `GRAFT_TOKEN` on the client. Pushing to a repo name that doesn't exist yet creates it.

## Commands

`init`, `add`, `commit -m`, `status`, `log [--oneline] [-n N] [rev]`, `diff [--cached] [rev [rev]]`, `branch [name] [-d name]`, `checkout [-b] branch`, `merge branch`, `remote add name url`, `push [-f]`, `fetch`, `pull`, `clone url [dir]`, `cat-file [-t] id`, `hash-object [-w] file`, `config user.name "..."`.

## What's not here

- **Delta compression and packfiles.** Objects are sent and stored whole (zlib-compressed). See the push-size numbers above.
- **git's wire protocol.** graft's sync protocol is its own (simpler) HTTP protocol, so you can't `git push` to the graft server, only read its repos with git locally.
- **The binary index.** The staging area is a JSON file, so git can read history from an graft repo but not its staged changes.
- **Recursive merge** for criss-cross histories (picks one merge base), rename detection, rebase, tags beyond plain refs, submodules.
- **Persistence on the free hosting tier.** The live server's disk is wiped on restart; it rebuilds the demo repo on boot. Anything else pushed there is temporary.
- **Line endings.** Files are stored byte for byte, with no CRLF conversion.

## Layout

```
graft/
  objects.py     object format, store, tree build/flatten
  repo.py        refs + CAS, index, status, checkout, commit graph
  diff.py        Myers, LCS DP baseline, unified output
  merge.py       diff3 line merge, per-file tree merge
  porcelain.py   merge and commit as the user sees them
  sync.py        negotiation, pack format, verification, push/fetch/clone
  cli.py
server/
  app.py         HTTP protocol endpoints + web UI
  demo.py        builds the demo repo (two merges, one with a resolved conflict)
tests/           40 tests
bench/bench.py
```

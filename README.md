# Graft

Graft is a version control system I wrote from scratch in Python, along with a small server you can push to and clone from.

I wanted to understand how Git actually works underneath, so I built the main pieces myself: storing snapshots, diffing, merging, and syncing with a remote. I also made the storage format match Git's exactly, so I could use the real `git` to check my work. Repositories created by Graft pass `git fsck --strict`, and `git log --graph` can read and draw their history.

Live server: https://graft-5ize.onrender.com (you can browse the demo repo's history, merges and diffs in the browser)

To try the command line tool:

```
pip install git+https://github.com/Shaan-4/graft
graft clone https://graft-5ize.onrender.com/demo
cd demo
graft log --oneline
```

The client only uses the Python standard library, and CI runs the tests on both Ubuntu and Windows.

## What it can do

| part | what's in it | file |
|---|---|---|
| Storage | blobs, trees and commits stored by their SHA-1 hash, written atomically, and checked again whenever they're read | `objects.py` |
| Working directory | `add`, `status`, `commit`, branches, `checkout`, a `.graftignore` file, and a cache so `status` doesn't re-read unchanged files | `repo.py` |
| Diff | the Myers diff algorithm, with output in the usual unified format | `diff.py` |
| Merge | finding the merge base, fast-forwards, 3-way merges by file and by line, conflict markers | `merge.py`, `porcelain.py` |
| Sync | `push`, `fetch`, `pull` and `clone` over HTTP, sending only the objects the other side doesn't have | `sync.py` |
| Server | hosts repositories, checks everything it receives, handles two pushes at once safely, and has a small web UI | `server/app.py` |

## How the main parts work

**Storage.** Every file is saved as a "blob" and named by the SHA-1 hash of its contents (Git hashes `blob <size>\0<bytes>`, so I do the same). A tree is a folder listing that points to blobs and other trees by their hashes, and a commit points to one tree plus its parent commits. This means if you change one file, its blob gets a new hash, so does every folder above it, and so does the commit. Folders that didn't change keep the same hash and are shared between commits instead of being stored twice.

One thing that tripped me up: Git sorts the entries in a tree as if folder names end with `/`. So a file called `a.txt` comes before a folder called `a`, because `.` comes before `/`, even though plain string sorting puts `a` first. If you get this wrong, `git fsck` rejects the tree. There's a test that sets up exactly this case and compares my tree hash with Git's.

**Diff.** The textbook approach is finding the longest common subsequence with dynamic programming, which takes time proportional to N times M no matter how similar the two files are. The Myers algorithm works by how many edits are needed instead. It tries 0 edits, then 1, then 2 and so on, skipping over matching lines for free, and stops as soon as it reaches the end. So when two versions are mostly the same, which is the normal case, it's much faster. I tested it on 3,000 random pairs of inputs to make sure it always gives a correct and minimal edit script, by comparing against the DP answer.

**Merge.** You can't merge properly by only looking at the two branches, because if a line is in one and not the other, you can't tell whether it was added on one side or deleted on the other. You need the last commit both branches share (the merge base), which I find with a breadth-first search through the commit history. Then each section is compared against that base: if only one side changed it, take that side; if both made the same change, that's fine; if they changed it differently, it's a conflict. When the history has more than one possible merge base, Git merges those bases together first. I just pick the most recent one, which is a known shortcut.

**Sync.** When fetching or pushing, one side tells the other which commits it already has. If you have a commit, you must also have everything before it, so the sender walks back through the history and stops as soon as it reaches something the other side already has. Files that didn't change keep the same hash, so they're skipped too.

The server doesn't trust what it receives:
- it recalculates the hash of every object instead of trusting the name it was sent with
- it checks a checksum over the whole upload
- it limits how big an upload can get after decompression
- it makes sure every file a new commit refers to is actually present before moving a branch to it

For that last check it only walks the objects in the new upload. Everything already on the server was checked when it arrived, so that's enough.

**Two pushes at the same time.** Say two people both see `main` at commit X and both push. Each push says "move main from X to my commit". The server only does that if main is still at X. To make the check-and-update safe, it creates a lock file using `O_CREAT | O_EXCL`, which the operating system guarantees only one process can do. It then checks the branch, writes the new value, calls `fsync`, and renames the file into place, which happens atomically. The second push sees that main has moved and gets rejected, so it has to pull and merge first. Because this uses the filesystem and not a lock in memory, it still works when the server runs as several processes. If a process crashes while holding the lock, the lock is treated as abandoned after 60 seconds.

## Numbers

All from `python bench/bench.py`, run on a 2-core machine with Python 3.13.

**Myers diff vs the DP approach**, on `argparse.py` from the Python standard library (2,690 lines), with k random lines changed:

| lines changed | Myers | DP | speedup |
|---|---|---|---|
| 1 | 1.1 ms | 651 ms | 576x |
| 10 | 2.7 ms | 736 ms | 274x |
| 50 | 4.3 ms | 827 ms | 191x |
| 200 | 26.9 ms | 905 ms | 34x |

The speedup gets smaller as more lines change, which is what you'd expect since Myers does more work the more the files differ, and DP doesn't care. With only one change, most of the gain comes from trimming the identical lines at the start and end.

**Push size**, for a repo made from the standard library's `email` package (30 files, 391 KiB):

| | objects sent | size |
|---|---|---|
| first push | 33 | 98.4 KiB |
| after editing one line in one file | 3 | 12.9 KiB |

It's only 8x smaller because the edited file still gets sent in full. Git would send just the difference from the old version, which would be tiny. That's the main thing I'd add next.

**`status` on 3,000 files:** 55 ms with the cache, 98 ms when it re-hashes every file. The gain is small here because the operating system has the files cached in memory anyway. It would matter more with big files or a cold start.

## Running it locally

```
pip install -e ".[dev]"
python -m pytest -q
python bench/bench.py
```

To run the server (Windows cmd; use `export` instead of `set` on Linux or macOS):

```
set GRAFT_REPOS=repos
set GRAFT_SEED_DEMO=1
uvicorn server.app:app --port 8000
```

Then `graft clone http://localhost:8000/demo`.

If the server has `GRAFT_PUSH_TOKEN` set, the client needs the same value in `GRAFT_TOKEN` to push. Pushing to a repo name that doesn't exist yet creates it.

## Commands

`init`, `add`, `commit -m`, `status`, `log [--oneline] [-n N] [rev]`, `diff [--cached] [rev [rev]]`, `branch [name] [-d name]`, `checkout [-b] branch`, `merge branch`, `remote add name url`, `push [-f]`, `fetch`, `pull`, `clone url [dir]`, `cat-file [-t] id`, `hash-object [-w] file`, `config user.name "..."`

## What's missing

- Delta compression and packfiles. Objects are stored and sent whole (compressed with zlib), which is why the push numbers above aren't better.
- Git's own network protocol. Graft uses its own simpler HTTP protocol, so you can't `git push` to a Graft server. Git can only read Graft repositories locally.
- Git's binary index file. My staging area is a JSON file, so Git can read the history of a Graft repo but not what's currently staged.
- Recursive merges for complicated histories, rename detection, rebase, and submodules.
- Persistent storage on the free hosting plan. The live server's disk is wiped when it restarts, and it rebuilds the demo repo on startup, so anything else pushed there won't last.
- Line ending conversion. Files are stored exactly as they are, with no CRLF handling.

## Project layout

```
graft/
  objects.py     object format, storage, building and flattening trees
  repo.py        branches and locking, the index, status, checkout, commit history
  diff.py        Myers diff, the DP version for comparison, unified output
  merge.py       line-by-line 3-way merge, merging whole trees
  porcelain.py   the merge and commit commands
  sync.py        working out what to send, the upload format, checks, push/fetch/clone
  cli.py
server/
  app.py         HTTP endpoints and the web pages
  demo.py        builds the demo repo (two merges, one with a conflict that was fixed)
tests/           41 tests
bench/bench.py
```

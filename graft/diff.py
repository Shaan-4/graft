"""
Line diff: Myers' O((N+M)·D) algorithm, plus the textbook O(N·M) LCS DP for
comparison (used only in tests and the benchmark).

Both find a shortest edit script: the fewest line insertions + deletions that
turn `a` into `b`. That is the same thing as finding a longest common
subsequence, since every line not in the LCS must be deleted or inserted.

Why Myers is fast in practice: picture an (N+1)x(M+1) grid. Moving right =
delete a line of a, moving down = insert a line of b, moving diagonally =
the lines match (free). A shortest edit script is a path from top-left to
bottom-right with the fewest non-diagonal moves. Myers explores, for
d = 0, 1, 2, ..., the furthest point reachable on each diagonal k = x - y
using exactly d edits, sliding along free diagonals as far as possible.
It stops at the first d that reaches the corner, so the work is
proportional to D (the number of differences), not to N·M. Two versions of
a source file usually differ in a handful of lines, so D is small.
"""
from __future__ import annotations

from dataclasses import dataclass

EQ, DEL, INS = "=", "-", "+"


@dataclass(frozen=True)
class Op:
    kind: str           # EQ, DEL or INS
    a_idx: int | None   # index into a (for EQ and DEL)
    b_idx: int | None   # index into b (for EQ and INS)


def myers(a: list, b: list) -> list[Op]:
    # Common prefix and suffix are pure diagonal; strip them first. Cheap,
    # and it often removes most of the file.
    pre = 0
    while pre < len(a) and pre < len(b) and a[pre] == b[pre]:
        pre += 1
    suf = 0
    while suf < len(a) - pre and suf < len(b) - pre and a[-1 - suf] == b[-1 - suf]:
        suf += 1

    mid = _myers_core(a[pre:len(a) - suf], b[pre:len(b) - suf])
    ops = [Op(EQ, i, i) for i in range(pre)]
    ops += [Op(o.kind,
               None if o.a_idx is None else o.a_idx + pre,
               None if o.b_idx is None else o.b_idx + pre) for o in mid]
    ops += [Op(EQ, len(a) - suf + i, len(b) - suf + i) for i in range(suf)]
    return ops


def _myers_core(a: list, b: list) -> list[Op]:
    n, m = len(a), len(b)
    if n == 0:
        return [Op(INS, None, j) for j in range(m)]
    if m == 0:
        return [Op(DEL, i, None) for i in range(n)]

    # Compare small ints instead of whole lines in the hot loop.
    ids: dict = {}
    a_ = [ids.setdefault(x, len(ids)) for x in a]
    b_ = [ids.setdefault(x, len(ids)) for x in b]

    max_d = n + m
    offset = max_d
    v = [0] * (2 * max_d + 2)   # v[k + offset] = furthest x on diagonal k
    trace = []                  # snapshot of v per d, for backtracking

    for d in range(max_d + 1):
        trace.append(v[offset - d: offset + d + 2].copy())
        for k in range(-d, d + 1, 2):
            # Reach diagonal k either from k+1 by moving down (insert) or
            # from k-1 by moving right (delete); pick whichever got further.
            if k == -d or (k != d and v[offset + k - 1] < v[offset + k + 1]):
                x = v[offset + k + 1]
            else:
                x = v[offset + k - 1] + 1
            y = x - k
            while x < n and y < m and a_[x] == b_[y]:   # free diagonal moves
                x += 1
                y += 1
            v[offset + k] = x
            if x >= n and y >= m:
                return _backtrack(trace, d, n, m)
    raise AssertionError("unreachable")


def _backtrack(trace: list[list[int]], d_final: int, n: int, m: int) -> list[Op]:
    ops: list[Op] = []
    x, y = n, m
    for d in range(d_final, 0, -1):
        v = trace[d]                # this is v as it was *before* step d
        base = -d                   # trace[d] holds diagonals -d .. d+1
        k = x - y
        get = lambda kk: v[kk - base]
        if k == -d or (k != d and get(k - 1) < get(k + 1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = get(prev_k)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:      # diagonal (equal) run
            x -= 1
            y -= 1
            ops.append(Op(EQ, x, y))
        if x == prev_x:
            y -= 1
            ops.append(Op(INS, None, y))
        else:
            x -= 1
            ops.append(Op(DEL, x, None))
    while x > 0 and y > 0:                    # leading diagonal at d = 0
        x -= 1
        y -= 1
        ops.append(Op(EQ, x, y))
    ops.reverse()
    return ops


def lcs_dp(a: list, b: list) -> int:
    """Length of the LCS by classic DP, O(N·M) time, O(M) memory."""
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def edit_distance(ops: list[Op]) -> int:
    return sum(1 for o in ops if o.kind != EQ)


def apply(ops: list[Op], a: list, b: list) -> list:
    """Rebuild b from a using the script. Used by tests to check correctness."""
    out = []
    for o in ops:
        if o.kind == EQ:
            out.append(a[o.a_idx])
        elif o.kind == INS:
            out.append(b[o.b_idx])
    return out


# ------------------------------------------------------------ presentation

def split_lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def unified(a: bytes, b: bytes, a_name: str, b_name: str, context: int = 3) -> str:
    if a == b:
        return ""
    if is_binary(a) or is_binary(b):
        return f"Binary files {a_name} and {b_name} differ\n"
    al, bl = split_lines(a), split_lines(b)
    ops = myers(al, bl)
    out = [f"--- {a_name}\n", f"+++ {b_name}\n"]

    changes = [i for i, o in enumerate(ops) if o.kind != EQ]
    # Group changes whose context windows overlap into one hunk.
    hunks, start = [], None
    for i in changes:
        if start is None:
            start, end = i, i
        elif i - end <= 2 * context:
            end = i
        else:
            hunks.append((start, end))
            start, end = i, i
    if start is not None:
        hunks.append((start, end))

    def text(line: bytes) -> str:
        s = line.decode("utf-8", errors="replace")
        return s if s.endswith("\n") else s + "\n\\ No newline at end of file\n"

    for s, e in hunks:
        lo, hi = max(0, s - context), min(len(ops), e + context + 1)
        chunk = ops[lo:hi]
        a_start = next((o.a_idx for o in chunk if o.a_idx is not None), None)
        b_start = next((o.b_idx for o in chunk if o.b_idx is not None), None)
        a_len = sum(1 for o in chunk if o.kind != INS)
        b_len = sum(1 for o in chunk if o.kind != DEL)
        a_start = (a_start + 1) if a_len else (a_start or 0)
        b_start = (b_start + 1) if b_len else (b_start or 0)
        out.append(f"@@ -{a_start},{a_len} +{b_start},{b_len} @@\n")
        for o in chunk:
            if o.kind == EQ:
                out.append(" " + text(al[o.a_idx]))
            elif o.kind == DEL:
                out.append("-" + text(al[o.a_idx]))
            else:
                out.append("+" + text(bl[o.b_idx]))
    return "".join(out)

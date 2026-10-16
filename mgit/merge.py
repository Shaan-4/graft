"""
Three-way merge.

Two-way merge is impossible to do correctly: if ours has a line and theirs
doesn't, was it added by us or deleted by them? Only the common ancestor
(the merge base) can say. So every decision compares each side to the base:

  ours == base       -> only they changed it, take theirs
  theirs == base     -> only we changed it, take ours
  ours == theirs     -> both made the same change, take it
  otherwise          -> both changed it differently: conflict

That rule is applied twice: once per file (by blob id, cheap), and for
files both sides edited, once per region of lines (diff3).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .diff import EQ, myers, split_lines, is_binary


@dataclass
class TextMerge:
    content: bytes
    conflicts: int


def merge3(base: bytes, ours: bytes, theirs: bytes,
           ours_label: str = "ours", theirs_label: str = "theirs") -> TextMerge:
    """
    diff3 by stable lines: diff base->ours and base->theirs; a base line that
    survives unchanged in *both* is a sync point. Between consecutive sync
    points each side has a chunk, and the rule above decides each chunk.
    """
    b, o, t = split_lines(base), split_lines(ours), split_lines(theirs)
    in_o = {op.a_idx: op.b_idx for op in myers(b, o) if op.kind == EQ}
    in_t = {op.a_idx: op.b_idx for op in myers(b, t) if op.kind == EQ}

    # Both maps come from LCS alignments, so they're monotonic; the sync
    # points are monotonic in all three files at once.
    sync = [(i, in_o[i], in_t[i]) for i in range(len(b)) if i in in_o and i in in_t]
    sync.append((len(b), len(o), len(t)))   # sentinel: end of all three

    out: list[bytes] = []
    conflicts = 0
    pb = po = pt = 0
    for i, j, k in sync:
        cb, co, ct = b[pb:i], o[po:j], t[pt:k]
        if co == cb:
            out += ct
        elif ct == cb:
            out += co
        elif co == ct:
            out += co
        else:
            conflicts += 1
            out.append(f"<<<<<<< {ours_label}\n".encode())
            out += _terminated(co)
            out.append(b"=======\n")
            out += _terminated(ct)
            out.append(f">>>>>>> {theirs_label}\n".encode())
        if i < len(b):
            out.append(b[i])
        pb, po, pt = i + 1, j + 1, k + 1
    return TextMerge(b"".join(out), conflicts)


def _terminated(lines: list[bytes]) -> list[bytes]:
    # A last line without "\n" would otherwise glue itself to the marker.
    if lines and not lines[-1].endswith(b"\n"):
        return lines[:-1] + [lines[-1] + b"\n"]
    return lines


@dataclass
class TreeMerge:
    files: dict[str, tuple[str, str]]       # path -> (mode, blob id), result
    conflicts: list[str] = field(default_factory=list)
    conflict_blobs: dict[str, bytes] = field(default_factory=dict)  # with markers


def merge_trees(store, base: dict, ours: dict, theirs: dict,
                theirs_label: str) -> TreeMerge:
    """Per-file merge over flattened trees {path: (mode, blob id)}."""
    res = TreeMerge(files={})
    for path in sorted(set(base) | set(ours) | set(theirs)):
        bb, oo, tt = base.get(path), ours.get(path), theirs.get(path)
        if oo == tt:
            chosen = oo
        elif oo == bb:
            chosen = tt
        elif tt == bb:
            chosen = oo
        elif oo is None or tt is None:
            # One side deleted, the other modified. No line-level answer.
            res.conflicts.append(path)
            chosen = oo or tt
        else:
            base_bytes = store.blob(bb[1]) if bb else b""
            ob, tb = store.blob(oo[1]), store.blob(tt[1])
            if is_binary(ob) or is_binary(tb) or is_binary(base_bytes):
                res.conflicts.append(path)
                chosen = oo
            else:
                m = merge3(base_bytes, ob, tb, "HEAD", theirs_label)
                sha = store.write(b"blob", m.content)
                chosen = (oo[0], sha)
                if m.conflicts:
                    res.conflicts.append(path)
                    res.conflict_blobs[path] = m.content
        if chosen is not None:
            res.files[path] = chosen
    return res

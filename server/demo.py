"""Builds the demo repository shown on the live server: a small project with
a feature branch, a clean merge, and a conflicting merge that was resolved."""
from __future__ import annotations

import os
import time
from pathlib import Path

import mgit.repo as repo_mod
from mgit import porcelain
from mgit.repo import Repo


def build_demo(path: Path) -> Repo:
    path.mkdir(parents=True)
    r = Repo.init(path)
    t = [int(time.time()) - 5 * 86400]

    def tick(hours: float = 3):
        t[0] += int(hours * 3600)
        repo_mod.clock = lambda: t[0]

    def write(rel: str, text: str):
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(msg: str, author=("Shaan Hasan", "shaan@example.com")):
        os.environ["MGIT_AUTHOR_NAME"], os.environ["MGIT_AUTHOR_EMAIL"] = author
        r.add(["."])
        tick()
        return porcelain.commit(r, msg)

    try:
        write("README.md", "# unitconv\n\nConvert between units from the command line.\n")
        write("unitconv/__init__.py", "")
        write("unitconv/length.py",
              "FACTORS = {\"m\": 1.0, \"km\": 1000.0, \"cm\": 0.01}\n\n\n"
              "def convert(value, src, dst):\n"
              "    return value * FACTORS[src] / FACTORS[dst]\n")
        commit("Initial length conversions")

        write("unitconv/length.py",
              "FACTORS = {\"m\": 1.0, \"km\": 1000.0, \"cm\": 0.01, \"mi\": 1609.344, \"ft\": 0.3048}\n\n\n"
              "def convert(value, src, dst):\n"
              "    return value * FACTORS[src] / FACTORS[dst]\n")
        commit("Add miles and feet")

        r.switch("temperature", create=True)
        write("unitconv/temperature.py",
              "def c_to_f(c):\n    return c * 9 / 5 + 32\n\n\n"
              "def f_to_c(f):\n    return (f - 32) * 5 / 9\n")
        commit("Add temperature conversions", ("Ananya Rao", "ananya@example.com"))
        write("unitconv/temperature.py",
              "def c_to_f(c):\n    return c * 9 / 5 + 32\n\n\n"
              "def f_to_c(f):\n    return (f - 32) * 5 / 9\n\n\n"
              "def c_to_k(c):\n    return c + 273.15\n")
        commit("Add Kelvin", ("Ananya Rao", "ananya@example.com"))

        r.switch("main")
        write("README.md", "# unitconv\n\nConvert between units from the command line.\n\n"
                           "## Usage\n\n    python -m unitconv 5 km mi\n")
        commit("Document usage")
        tick(1)
        porcelain.merge(r, "temperature")          # clean 3-way merge

        # Two branches edit the same line: a real conflict, then resolved.
        r.switch("rounding", create=True)
        write("unitconv/length.py",
              "FACTORS = {\"m\": 1.0, \"km\": 1000.0, \"cm\": 0.01, \"mi\": 1609.344, \"ft\": 0.3048}\n\n\n"
              "def convert(value, src, dst):\n"
              "    return round(value * FACTORS[src] / FACTORS[dst], 6)\n")
        commit("Round results to 6 places", ("Ananya Rao", "ananya@example.com"))
        r.switch("main")
        write("unitconv/length.py",
              "FACTORS = {\"m\": 1.0, \"km\": 1000.0, \"cm\": 0.01, \"mi\": 1609.344, \"ft\": 0.3048}\n\n\n"
              "def convert(value, src, dst):\n"
              "    if src not in FACTORS or dst not in FACTORS:\n"
              "        raise ValueError(f\"unknown unit: {src} or {dst}\")\n"
              "    return value * FACTORS[src] / FACTORS[dst]\n")
        commit("Reject unknown units")
        tick(1)
        res = porcelain.merge(r, "rounding")
        assert res.kind == "conflict", res
        write("unitconv/length.py",
              "FACTORS = {\"m\": 1.0, \"km\": 1000.0, \"cm\": 0.01, \"mi\": 1609.344, \"ft\": 0.3048}\n\n\n"
              "def convert(value, src, dst):\n"
              "    if src not in FACTORS or dst not in FACTORS:\n"
              "        raise ValueError(f\"unknown unit: {src} or {dst}\")\n"
              "    return round(value * FACTORS[src] / FACTORS[dst], 6)\n")
        commit("Merge rounding into main (resolve conflict in convert)")
    finally:
        repo_mod.clock = time.time
    return r

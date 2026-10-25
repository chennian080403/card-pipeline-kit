#!/usr/bin/env python3
"""Copy card-core into every skill that uses it, or check that the copies have not drifted.

    python3 sync.py            copy core/ -> <skill>/scripts/core/ for each consumer, and the shared docs
    python3 sync.py --check    exit 1 when a copy differs from the source (nothing is written)

Shared docs (consumers.json "docs": {source: [copies]}) are copied verbatim, so a skill installed
alone still carries them (e.g. the ecosystem contract lives in the video skill, the image skill has a copy).

Consumers are listed in consumers.json (paths relative to the folder that holds card-core and the skills). A skill opts in by
being listed; it then imports `from core import library, jobs, ...` and never edits its copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "core"
MYSKILLS = HERE.parent
HEADER = "# card-core copy -- do not edit here: edit card-core/core (next to this skill), then run card-core/sync.py\n"


def source_files():
    return sorted(p for p in SOURCE.glob("*.py"))


def consumers():
    return [MYSKILLS / rel for rel in json.loads((HERE / "consumers.json").read_text(encoding="utf-8"))["consumers"]]


def docs():
    spec = json.loads((HERE / "consumers.json").read_text(encoding="utf-8")).get("docs") or {}
    return [(MYSKILLS / src, [MYSKILLS / dst for dst in dsts]) for src, dsts in spec.items()]


def expected(path):
    return HEADER + path.read_text(encoding="utf-8")


def drift(target):
    """Differences between the source and one copy, as text lines."""
    out = []
    names = {p.name for p in source_files()}
    for src in source_files():
        dst = target / src.name
        if not dst.is_file():
            out.append(f"missing {dst}")
        elif dst.read_text(encoding="utf-8") != expected(src):
            out.append(f"differs {dst}")
    for extra in sorted(target.glob("*.py")) if target.is_dir() else []:
        if extra.name not in names:
            out.append(f"extra   {extra}")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    bad = 0
    for skill in consumers():
        target = skill / "scripts" / "core"
        if args.check:
            problems = drift(target)
            bad += bool(problems)
            print(("✗ " if problems else "✓ ") + f"{skill.name}" + "".join(f"\n    {p}" for p in problems))
            continue
        target.mkdir(parents=True, exist_ok=True)
        for src in source_files():
            (target / src.name).write_text(expected(src), encoding="utf-8")
        for extra in target.glob("*.py"):
            if not (SOURCE / extra.name).is_file():
                extra.unlink()
        shutil.rmtree(target / "__pycache__", ignore_errors=True)
        print(f"✓ synced {target}")
    for src, copies in docs():
        if not src.is_file():          # the owning skill is not installed here: nothing to copy from
            continue
        for dst in copies:
            if not dst.parent.is_dir():
                continue
            if args.check:
                same = dst.is_file() and dst.read_bytes() == src.read_bytes()
                bad += not same
                print(("✓ " if same else "✗ ") + f"doc {dst.relative_to(MYSKILLS)}" + ("" if same else f"\n    differs from {src.relative_to(MYSKILLS)}"))
            else:
                shutil.copyfile(src, dst)
                print(f"✓ synced {dst}")
    if bad:
        print("copies drifted: edit card-core/core, then run sync.py (never edit the copies)")
        sys.exit(1)


if __name__ == "__main__":
    main()

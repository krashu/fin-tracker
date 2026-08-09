#!/usr/bin/env python3
"""Copy library-shipped .agents/skills/<lib>/ directories into .claude/skills/<lib>/.

Some libraries ship Claude Code skills inside their distribution at
<package>/.agents/skills/<name>/. This script discovers those in the installed
backend venv and frontend node_modules, then copies them into the project's
.claude/skills/ so Claude Code's standard discovery picks them up.

Idempotent: destinations are wiped and rewritten so upstream changes (after
`uv sync` or `pnpm install` upgrades) propagate cleanly. Safe to run repeatedly.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

DEST_ROOT = Path(".claude/skills")


def discover_roots() -> list[Path]:
    """Locate site-packages / node_modules roots to scan. Missing roots are skipped."""
    roots: list[Path] = []
    win_venv = Path("backend/.venv/Lib/site-packages")
    if win_venv.exists():
        roots.append(win_venv)
    unix_venv_lib = Path("backend/.venv/lib")
    if unix_venv_lib.exists():
        roots.extend(unix_venv_lib.glob("python*/site-packages"))
    node_modules = Path("frontend/node_modules")
    if node_modules.exists():
        roots.append(node_modules)
    return roots


def main() -> int:
    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    roots = discover_roots()
    if not roots:
        print("[refresh-skills] no .venv or node_modules found; nothing to scan")
        return 0

    copied = 0
    for root in roots:
        for skill_dir in root.glob("**/.agents/skills/*"):
            if not skill_dir.is_dir():
                continue
            lib_name = skill_dir.name
            dest = DEST_ROOT / lib_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
            print(f"[refresh-skills] copied {lib_name}")
            copied += 1

    if copied == 0:
        print("[refresh-skills] no library-shipped .agents/skills/ found")
    else:
        print(f"[refresh-skills] done ({copied} skill(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Zip the working-tree source for transfer, excluding all git-ignored artifacts.

Source set comes from `git ls-files -co --exclude-standard`, so node_modules/, .venv/,
.next/, caches, data/, dist/, .scratch/, .claude/settings.local.json and .env are never
included (they're git-ignored) while .env.example and uncommitted/untracked source files
are. Source only — no .git history.

Secret safety depends on `.env*` staying in .gitignore: the -o (untracked) flag would
otherwise pull a real .env into the zip.

Shared agent config rides along: `.devcontainer/`, `.claude/` (settings, hooks, skills)
and the CLAUDE.md shims. `--exclude-standard` also applies the *host's* global gitignore,
which on the Windows machine has excluded `.devcontainer/` and `CLAUDE.md` before;
.gitignore re-includes them, and _REQUIRED below fails the run loudly if any still went
missing rather than shipping a zip without them.

`plans/` is the one git-ignored directory that *does* ride along: working plans are kept out of
commits but must reach the other laptop. It is walked from disk rather than taken from
`git ls-files`, so neither the repo's nor a host's ignore rule can drop it.

Clean-machine safety: the zip is meant to run on a *non-corporate* laptop, so any
corporate TLS-proxy CA cert (the `frontend/certs/` hook — read by both the frontend
Docker build and the Makefile's `ca-bundle` target) is stripped explicitly here, not
just relied upon staying git-ignored. That covers the generated
`backend/data/corp-ca-bundle.pem` twice over, since `data/` is git-ignored as well.
The `frontend/certs/` docs still ride along, so the dir arrives empty of certs and
that laptop builds against public roots with no extra steps. See
frontend/certs/README.md.

File modes are written as 0644 for every entry rather than copied from disk: the dev
container's bind mount reports every file as 0777, which a Mac/Linux unzip would turn
into an executable bit on everything. Git tracks no executables here.

Usage:  python scripts/package_source.py
Output: <repo>/dist/fin-tracker.zip  (dist/ is git-ignored, and inside the repo so the
        dev container's bind mount makes it visible on the host)
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # scripts/ lives at the repo root
TOP = "fin-tracker"  # top folder in the zip = the GitHub repo name, not the checkout dir name
OUT = ROOT / "dist" / f"{TOP}.zip"

# Cert/key material that must never ride a source zip destined for a clean machine,
# even if a stray copy slipped past .gitignore (the repo tracks no such files — see
# the guard in main()). Matched case-insensitively on the file suffix.
_CERT_SUFFIXES = (".crt", ".pem", ".cer", ".key", ".p12", ".pfx")

# Files a usable zip must contain. A miss means they were deleted, or an ignore rule (usually
# the host's global gitignore) dropped them from `git ls-files`.
_REQUIRED = (
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/settings.json",
    ".claude/hooks/guard_bash.py",
    ".devcontainer/devcontainer.json",
)


def _is_cert(rel: str) -> bool:
    return rel.lower().endswith(_CERT_SUFFIXES)


def main() -> None:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "-z", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # -c still lists tracked files deleted from the working tree; package what is on disk.
    files = [f for f in listing.split("\0") if f and (ROOT / f).is_file()]
    # Git-ignored on purpose, shipped on purpose — see the module docstring.
    plans = [p.relative_to(ROOT).as_posix() for p in (ROOT / "plans").rglob("*") if p.is_file()]
    files = sorted(set(files) | set(plans))

    missing = [r for r in _REQUIRED if r not in files]
    if missing:
        sys.exit(
            "Refusing to package, missing: "
            + ", ".join(missing)
            + ".\nDeleted, or hidden by an ignore rule — check `git check-ignore -v <path>`, "
            "including your global gitignore (git config core.excludesFile)."
        )

    kept = [f for f in files if not _is_cert(f)]
    stripped = [f for f in files if _is_cert(f)]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in kept:
            info = zipfile.ZipInfo.from_file(ROOT / f, arcname=f"{TOP}/{f}")
            info.external_attr = (0o100644 & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (ROOT / f).read_bytes())

    print(f"Created {OUT}  ({OUT.stat().st_size / 1_000_000:.2f} MB, {len(kept)} files)")
    if stripped:
        print(f"Stripped {len(stripped)} cert/key file(s) for clean-machine safety:")
        for f in stripped:
            print(f"  - {f}")


if __name__ == "__main__":
    main()

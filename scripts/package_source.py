#!/usr/bin/env python3
"""Zip the working-tree source for transfer, excluding all git-ignored artifacts.

Source set comes from `git ls-files -co --exclude-standard`, so node_modules/, .venv/,
.next/, caches, data/, and .env are never included (they're git-ignored) while
.env.example and uncommitted/untracked source files are. Source only — no .git history.

Secret safety depends on `.env*` staying in .gitignore: the -o (untracked) flag would
otherwise pull a real .env into the zip.

Clean-machine safety: the zip is meant to run on a *non-corporate* laptop, so any
corporate TLS-proxy CA cert (the `frontend/certs/` hook — read by both the frontend
Docker build and the Makefile's `ca-bundle` target) is stripped explicitly here, not
just relied upon staying git-ignored. That covers the generated
`backend/data/corp-ca-bundle.pem` twice over, since `data/` is git-ignored as well.
The `frontend/certs/` docs still ride along, so the dir arrives empty of certs and
that laptop builds against public roots with no extra steps. See
frontend/certs/README.md.

Usage:  python scripts/package_source.py
Output: <repo-parent>/fin-tracker.zip
"""
from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # scripts/ lives at the repo root
TOP = ROOT.name                                # "fin-tracker" — top folder in the zip
OUT = ROOT.parent / f"{TOP}.zip"               # outside the repo

# Cert/key material that must never ride a source zip destined for a clean machine,
# even if a stray copy slipped past .gitignore (the repo tracks no such files — see
# the guard in main()). Matched case-insensitively on the file suffix.
_CERT_SUFFIXES = (".crt", ".pem", ".cer", ".key", ".p12", ".pfx")


def _is_cert(rel: str) -> bool:
    return rel.lower().endswith(_CERT_SUFFIXES)


def main() -> None:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "-z", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files = [f for f in listing.split("\0") if f]

    kept = [f for f in files if not _is_cert(f)]
    stripped = [f for f in files if _is_cert(f)]

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in kept:
            z.write(ROOT / f, arcname=f"{TOP}/{f}")

    print(f"Created {OUT}  ({OUT.stat().st_size / 1_000_000:.2f} MB, {len(kept)} files)")
    if stripped:
        print(f"Stripped {len(stripped)} cert/key file(s) for clean-machine safety:")
        for f in stripped:
            print(f"  - {f}")


if __name__ == "__main__":
    main()

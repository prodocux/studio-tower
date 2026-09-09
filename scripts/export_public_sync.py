#!/usr/bin/env python3
r"""Deterministic public export gate for studiotower (Mode B).

Labs incubator tree is the source of truth for development.
`D:\ProDocuX\studiotower` is the public clone / standalone repository.

Workflow:
    labs source
      -> export to empty staging dir
      -> optional checks
      -> compare / sync into public clone
      -> commit/push only in the public clone
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from list_public_export import iter_public_export_paths  # noqa: E402

DEFAULT_PUBLIC_ROOT = Path(r"D:\ProDocuX\studiotower")
FORBIDDEN_PUBLIC_NAMES = {".env", ".venv", "venv", "runs", "build", "dist", ".tmp", "data", "scratch", "node_modules"}
_SKIP_SECRET_SCAN_DIR_PARTS = {"tests", "test-results", "playwright-report", "node_modules", "__pycache__"}
_SECRET_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("grafana cloud write token", re.compile(r"glc_eyJ[A-Za-z0-9._-]{20,}")),
    ("grafana service account token", re.compile(r"glsa_(?!test(?:_|$))[A-Za-z0-9._-]{24,}")),
    ("google api key", re.compile(r"AIzaSy[A-Za-z0-9_-]{20,}")),
    ("private key pem", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip_secret_scan(rel: str) -> bool:
    parts = {part.lower() for part in Path(rel).parts}
    if parts & _SKIP_SECRET_SCAN_DIR_PARTS:
        return True
    name = Path(rel).name.lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".zip"))


def scan_staging_for_secrets(staging: Path, export_rels: list[str]) -> list[str]:
    """Fail-closed scan for live credentials copied into the public export set."""
    hits: list[str] = []
    for rel in export_rels:
        if _skip_secret_scan(rel):
            continue
        path = staging / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in _SECRET_SCAN_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}: {label}")
    return hits


def _export_to_staging(src_root: Path, staging: Path) -> list[str]:
    rels = iter_public_export_paths(src_root)
    for rel in rels:
        s = src_root / rel
        d = staging / rel
        if not s.is_file():
            raise FileNotFoundError(f"missing source file: {rel}")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    return rels


def _tracked_or_present_files(public_root: Path) -> set[str]:
    out: set[str] = set()
    if not public_root.is_dir():
        return out
    for p in public_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(public_root).as_posix()
        if rel.startswith(".git/"):
            continue
        top = rel.split("/", 1)[0]
        if top in FORBIDDEN_PUBLIC_NAMES or top.startswith(".venv"):
            continue
        if "/__pycache__/" in f"/{rel}/" or rel.endswith((".pyc", ".pyo")):
            continue
        out.add(rel)
    return out


def _public_has_local_changes(public_root: Path) -> list[str]:
    if not (public_root / ".git").exists():
        return []
    proc = subprocess.run(
        ["git", "-C", str(public_root), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return [f"git status failed: {(proc.stderr or proc.stdout or '').strip()}"]
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return lines


def compare_trees(staging: Path, public_root: Path, export_rels: list[str]) -> dict[str, list[str]]:
    export_set = set(export_rels)
    public_files = _tracked_or_present_files(public_root)
    missing = sorted(export_set - public_files)
    extra = sorted(public_files - export_set)
    changed: list[str] = []
    for rel in sorted(export_set & public_files):
        if _sha256(staging / rel) != _sha256(public_root / rel):
            changed.append(rel)
    return {"missing": missing, "extra": extra, "changed": changed}


def sync_into_public(staging: Path, public_root: Path, export_rels: list[str], *, delete_extra: bool) -> None:
    public_root.mkdir(parents=True, exist_ok=True)
    for rel in export_rels:
        s = staging / rel
        d = public_root / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    if delete_extra:
        extras = sorted(_tracked_or_present_files(public_root) - set(export_rels))
        for rel in extras:
            path = public_root / rel
            if path.is_file():
                path.unlink()
                parent = path.parent
                while parent != public_root and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent


def main() -> int:
    p = argparse.ArgumentParser(description="StudioTower Mode B public export gate")
    p.add_argument("--source", type=Path, default=ROOT, help="labs incubator source root")
    p.add_argument(
        "--public-root",
        type=Path,
        default=DEFAULT_PUBLIC_ROOT,
        help="public root path (commit/push exit)",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="export to staging and compare to public root (no writes to public)",
    )
    mode.add_argument(
        "--sync",
        action="store_true",
        help="export then sync into public root (fails if public has uncommitted changes)",
    )
    p.add_argument(
        "--allow-dirty-public",
        action="store_true",
        help="allow sync even if public clone has local modifications",
    )
    p.add_argument(
        "--delete-extra",
        action="store_true",
        help="with --sync, delete public files not in export set",
    )
    args = p.parse_args()

    src = args.source.resolve()
    public = args.public_root.resolve()
    if src == public:
        print("FAIL: source and public-root must differ", file=sys.stderr)
        return 2
    if not src.is_dir():
        print(f"FAIL: source missing: {src}", file=sys.stderr)
        return 2

    dirty = _public_has_local_changes(public) if public.exists() else []
    if args.sync and dirty and not args.allow_dirty_public:
        print("FAIL: public clone has local changes; refuse to overwrite:", file=sys.stderr)
        for line in dirty[:50]:
            print(f"  {line}", file=sys.stderr)
        print("Commit/discard in the public clone, or pass --allow-dirty-public.", file=sys.stderr)
        return 3

    with tempfile.TemporaryDirectory(prefix="studiotower_export_") as td:
        staging = Path(td)
        export_rels = _export_to_staging(src, staging)
        leaks = scan_staging_for_secrets(staging, export_rels)
        if leaks:
            print("FAIL: public export contains credential-like material:", file=sys.stderr)
            for line in leaks[:50]:
                print(f"  {line}", file=sys.stderr)
            return 4
        diff = compare_trees(staging, public, export_rels)

        print(f"Export set count: {len(export_rels)}")
        print(f"  missing in public : {len(diff['missing'])}")
        print(f"  changed in public : {len(diff['changed'])}")
        print(f"  extra in public   : {len(diff['extra'])}")

        if args.check:
            if diff["missing"] or diff["changed"] or (diff["extra"] and args.delete_extra):
                print("CHECK: differences detected between incubator and public root.")
                return 1
            print("OK: public root is completely up-to-date with incubator export.")
            return 0

        sync_into_public(staging, public, export_rels, delete_extra=args.delete_extra)
        print(f"SUCCESS: synced {len(export_rels)} files into {public}")
        return 0


if __name__ == "__main__":
    sys.exit(main())

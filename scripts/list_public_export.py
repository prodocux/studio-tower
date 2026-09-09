#!/usr/bin/env python3
"""Print the intended public-repo file set for studiotower (dry-run; does not copy)."""

from __future__ import annotations

import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INCLUDE_DIRS = ("backend", "frontend", "docs", "scripts")
INCLUDE_FILES = (
    "README.md",
    "LICENSE",
    "pyproject.toml",
    ".gitignore",
    ".gcloudignore",
    ".env.example",
    "cloudbuild.yaml",
    "Dockerfile",
    "firebase.json",
    "firestore.indexes.json",
    "PRIOR_ART.md",
)
EXCLUDE_NAME = {
    ".env",
    ".env.local",
    "AGENT_SYNC.md",
    "PROJECT_STORY.md",
    "JUDGES_GUIDE.md",
    "AI_DEVELOPMENT_LOG.md",
    "FRONTEND_PRODUCT_IMPROVEMENT_PLAN.md",
    "phase0_baseline.md",
    "_tmp_secret_scan.py",
}
EXCLUDE_GLOBS = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/runs/**",
    "**/dist/**",
    "**/build/**",
    "**/test-results/**",
    "**/.last-run.json",
    "**/playwright-report/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/.firebase/**",
    "**/node_modules/**",
    "**/data/**",
    "**/backend/data/**",
    "**/scratch/**",
    "**/*.timestamp-*.mjs",
    "**/*.egg-info/**",
    "**/*.zip",
)


def _excluded(rel: str) -> bool:
    name = Path(rel).name
    if name in EXCLUDE_NAME or name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    for pat in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(rel.replace("\\", "/"), pat):
            return True
    return False


def iter_public_export_paths(root: Path | None = None) -> list[str]:
    base_root = root if root is not None else ROOT
    paths: list[str] = []
    for name in INCLUDE_FILES:
        p = base_root / name
        if p.is_file():
            paths.append(name)
    for d in INCLUDE_DIRS:
        base = base_root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(base_root).as_posix()
            if not _excluded(rel):
                paths.append(rel)
    return sorted(set(paths))


if __name__ == "__main__":
    for path in iter_public_export_paths():
        print(path)

"""Export reviewed source without development state or historical Git objects.

Example: python tools/export_public.py --output ../eljazari-public
The destination must not exist. The original repository is never rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

EXCLUDED_PARTS = {
    ".git",
    ".local",
    ".claude",
    ".codex",
    ".codex-tmp",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".next",
    ".pytest_cache",
    ".pytest-local-tmp",
    ".ruff_cache",
    ".mypy_cache",
    ".turbo",
    "experiment-results",
    "artifacts",
    "cli_runs",
    "chroma_db",
    "vector_index",
}

# Maintainer-only files kept in the private repository: an assistant skill that
# describes an older design, and the release checklist with private-history notes.
PRIVATE_FILES = {
    "SKILL.md",
    "docs/OPEN_SOURCE_REVIEW_2026-09-30.md",
}


def allowed(path: Path) -> bool:
    if path.as_posix() in PRIVATE_FILES:
        return False
    if path.is_absolute() or any(
        p in EXCLUDED_PARTS or p == ".." or ":" in p or "\\" in p for p in path.parts
    ):
        return False
    if path.name.startswith(".env") and path.name != ".env.example":
        return False
    if path.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".log", ".pyc"}:
        return False
    if path.parts[:2] == ("data", "storage"):
        return False
    return path.parts[:3] != ("data", "components", "evidence")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--git-dir",
        type=Path,
        help="Optional Git metadata location for a cross-OS worktree",
    )
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    destination = args.output.resolve()
    if destination.exists():
        parser.error("Destination exists; choose a new empty export location")
    if source == destination or source in destination.parents:
        parser.error("Destination must be outside the source tree")
    git = ["git", "-c", "core.autocrlf=true"]
    if args.git_dir:
        git += [f"--git-dir={args.git_dir.resolve()}", f"--work-tree={source}"]
    else:
        git += ["-C", str(source)]
    raw = subprocess.check_output(
        git + ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    names = sorted(set(raw.decode("utf-8").split("\0")) - {""})
    selected = []
    for name in names:
        relative = Path(name)
        original = source / relative
        if not allowed(relative) or not original.exists():
            continue
        if original.is_symlink() or not original.is_file():
            raise ValueError(f"Refusing non-regular source file: {relative}")
        selected.append(relative)
    if not selected:
        raise ValueError("No source files selected")
    # Defense in depth: public catalog must contain no sourced declarations.
    for name in ("motors.json", "propulsion_tests.json"):
        catalog = json.loads(
            (source / "data/components" / name).read_text(encoding="utf-8")
        )
        if isinstance(catalog, dict):
            if catalog.get("tests"):
                raise ValueError(
                    "Public propulsion table still contains imported tests"
                )
        elif any(record.get("thrust_curve_evidence") for record in catalog):
            raise ValueError("Public motor catalog still contains imported evidence")
    destination.mkdir(parents=True)
    manifest = []
    for relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
        manifest.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    (destination / "PUBLIC_EXPORT.json").write_text(
        json.dumps(
            {
                "format": 2,
                "purpose": "Source snapshot without historical Git objects, secrets, local imports or runtime state",
                "files": manifest,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Exported {len(manifest)} files to {destination}")
    print(
        "Scan this snapshot, initialize a new Git history, then run CI before publication."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

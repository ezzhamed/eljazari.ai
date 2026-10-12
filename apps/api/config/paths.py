"""Paths shared by the repository checkout and flattened API/Celery images."""

from pathlib import Path


def project_root(module_file: Path) -> Path:
    """A module one package below the API root works in either layout."""
    api_root = module_file.parent.parent
    if api_root.name == "api" and api_root.parent.name == "apps":
        return api_root.parent.parent
    return api_root


PROJECT_ROOT = project_root(Path(__file__).resolve())

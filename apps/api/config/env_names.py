"""Environment variable naming, with backward compatibility.

The project's env vars were prefixed ``DRONEPROMPT_`` before it settled on the
name eljazari.ai. The canonical prefix is now ``ELJAZARI_``, but the old names
keep working: renaming config keys outright would break every existing ``.env``,
deployment, and prepared WSL environment for no functional gain.

Precedence is always canonical-first, so a deployment that sets both is not
surprised by the legacy value winning.

Pydantic settings fields use :func:`env_alias`; plain ``os.environ`` reads use
:func:`getenv`.
"""

from __future__ import annotations

import os

from pydantic import AliasChoices

CANONICAL_PREFIX = "ELJAZARI_"
LEGACY_PREFIX = "DRONEPROMPT_"


def canonical(suffix: str) -> str:
    """``"SITL_RUNTIME"`` -> ``"ELJAZARI_SITL_RUNTIME"``."""
    return f"{CANONICAL_PREFIX}{suffix}"


def legacy(suffix: str) -> str:
    """``"SITL_RUNTIME"`` -> ``"DRONEPROMPT_SITL_RUNTIME"``."""
    return f"{LEGACY_PREFIX}{suffix}"


def env_alias(suffix: str) -> AliasChoices:
    """Accept both spellings for a pydantic settings field, canonical first."""
    return AliasChoices(canonical(suffix), legacy(suffix))


def getenv(suffix: str, default: str | None = None) -> str | None:
    """Read ``ELJAZARI_<suffix>``, falling back to the legacy name."""
    value = os.environ.get(canonical(suffix))
    if value is not None:
        return value
    return os.environ.get(legacy(suffix), default)


def env_names(suffix: str) -> tuple[str, str]:
    """Both spellings, for callers that scan or clear the environment."""
    return canonical(suffix), legacy(suffix)

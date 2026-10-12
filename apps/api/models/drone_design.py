"""eljazari.ai — DEPRECATED: replaced by design.py

This module is kept only for backward-compatibility with old import paths.
Import the real model from ``models.design`` instead:

    from models.design import Design, DesignStatus
"""

# Re-export so `from models.drone_design import Design` still works.
from models.design import Design, DesignStatus  # noqa: F401

__all__ = ["Design", "DesignStatus"]

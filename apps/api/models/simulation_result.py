"""eljazari.ai — DEPRECATED: replaced by simulation.py

This module is kept only for backward-compatibility with old import paths.
Import the real model from ``models.simulation`` instead:

    from models.simulation import Simulation, SimulationStatus, SimulatorType
"""

# Re-export so `from models.simulation_result import Simulation` still works.
from models.simulation import (  # noqa: F401
    Simulation,
    SimulationStatus,
    SimulatorType,
)

__all__ = ["Simulation", "SimulationStatus", "SimulatorType"]

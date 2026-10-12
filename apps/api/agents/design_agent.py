"""eljazari.ai — Design Agent v2

Text prompt → DroneDesignTree + PerformanceEstimate
via AircraftVerse RAG + LLM generation.

Reference: AGENT model (arxiv:2504.08981)

Option B (RAG): LLM + embedded vector index vector store on AircraftVerse dataset (27,714 designs)

Pipeline:
  1. Embed user prompt with OllamaEmbeddings
  2. Retrieve k=5 most similar AircraftVerse designs from embedded vector index
  3. Build context-enriched GSCE-style prompt
  4. Call LLM → JSON generation
  5. Parse + validate → DroneDesignTree
  6. Compute physics-based PerformanceEstimate
"""

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_ollama import OllamaEmbeddings, OllamaLLM

from agents.design_models import (
    BatterySpec,
    CenterBody,
    DesignResult,
    DroneDesignTree,
    ElectronicsSpec,
    FrameSpec,
    MotorSpec,
    PayloadSpec,
    PerformanceEstimate,
)
from config.settings import settings
from services.llm_service import LLMService
from utils.mission_parser import parse_mission_spec

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

RAG_TOP_K = 5  # similar designs to retrieve
RAG_INDEX_LIMIT = 5000  # documents embedded into the vector store
# A similarity query takes milliseconds; a stuck one must not stall a design.
RAG_QUERY_TIMEOUT_S = 60.0
# Embedding the index takes minutes on a CPU; a stuck build must not either.
RAG_BUILD_TIMEOUT_S = 900.0
LEGACY_LLM_TIMEOUT_S = 30
DIRECT_LLM_TIMEOUT_S = settings.ollama_design_timeout_s


def get_aircraftverse_path() -> Path:
    return settings.resolved_aircraftverse_path


# One index per process and corpus. Every DesignAgent used to embed the corpus
# again into a collection with a fixed name, in memory (shared by every client
# in the process) or on disk (kept across restarts). Each design appended 5,000
# more documents: the persisted stores reached 2.6 GB and 6 GB, and a benchmark
# query on one of them never returned (2026-09-29).
_INDEXES: dict[tuple[str, ...], tuple[Any, bool]] = {}
_INDEX_LOCK = threading.Lock()
# Set once a build or query has hung: LocalVectorIndex calls in this process are not
# retried, since the next one would queue behind the stuck thread.
_RAG_DISABLED: str | None = None


TOPOLOGY_MOTORS: dict[str, int] = {
    "quadcopter": 4,
    "hexacopter": 6,
    "octocopter": 8,
    "tricopter": 3,
    "bicopter": 2,
    "fixed_wing": 1,
}

MOTOR_POSITIONS: dict[str, list[str]] = {
    "quadcopter": ["front_left", "front_right", "rear_left", "rear_right"],
    "hexacopter": [
        "front_left",
        "front_right",
        "mid_left",
        "mid_right",
        "rear_left",
        "rear_right",
    ],
    "octocopter": [
        "front_left",
        "front_right",
        "mid_left_front",
        "mid_right_front",
        "mid_left_rear",
        "mid_right_rear",
        "rear_left",
        "rear_right",
    ],
    "tricopter": ["front_left", "front_right", "rear_center"],
    "bicopter": ["left", "right"],
    "fixed_wing": ["nose"],
}

# ─────────────────────────────────────────────────────────────
# Physics-based performance calculator
# ─────────────────────────────────────────────────────────────


class PerformanceCalculator:
    """Estimates UAV performance from a DroneDesignTree using simplified physics."""

    # (KV_min, KV_max) → factor used as: thrust_g = voltage * factor / 10
    # Calibrated so realistic motor+prop combos produce correct static thrust.
    _KV_THRUST: list[tuple[tuple[int, int], float]] = [
        ((0, 800), 900.0),  # very low KV (large cargo, e.g. 4114-320KV, 15" prop)
        ((800, 1500), 500.0),  # low KV (photography, e.g. 3508-580KV, 12" prop)
        ((1500, 2200), 380.0),  # mid KV (general, e.g. 2212-1400KV, 8" prop)
        ((2200, 3000), 450.0),  # high KV (5-inch freestyle, e.g. 2306-2450KV, 5045)
        ((3000, 9999), 200.0),  # very high KV (racing/micro, 3" prop)
    ]

    def estimate(self, design: DroneDesignTree) -> PerformanceEstimate:
        n_motors = len(design.propulsion) or TOPOLOGY_MOTORS.get(design.topology, 4)
        bat = design.battery
        v_nom = bat.cells * 3.7  # nominal LiPo voltage per cell
        e_wh = (bat.capacity_mah / 1000.0) * v_nom

        first_motor = design.propulsion[0].motor if design.propulsion else "2206-2300KV"
        first_prop = design.propulsion[0].propeller if design.propulsion else "5045"

        kv = self._parse_kv(first_motor)
        thrust_per_motor_g = self._thrust_per_motor(kv, v_nom)
        total_thrust_g = thrust_per_motor_g * n_motors

        # Weight estimate
        arm_m = design.frame.arm_length_mm / 1000.0
        frame_g = arm_m * 1000 * 0.8 + 150  # carbon tube + plates
        motor_g = n_motors * 45  # avg motor+ESC+prop
        elec_g = 80  # FC + GPS + wiring

        # Assess the complete declared load consistently with the BOM solver.
        payload_load_g = design.payload.design_load_g

        total_weight_g = frame_g + bat.weight_g + motor_g + elec_g + payload_load_g

        # Hover: thrust used = weight; current ∝ fraction of max thrust
        hover_fraction = total_weight_g / max(total_thrust_g, 1.0)
        hover_current_a = hover_fraction * n_motors * 12.0  # ~12A max per motor
        if hover_current_a <= 0:
            hover_current_a = 10.0
        hover_time_s = (e_wh * 0.85 / (hover_current_a * v_nom)) * 3600.0

        twr = total_thrust_g / max(total_weight_g, 1.0)

        # Speed computation using Loaded RPM and Propeller Pitch (Aerodynamically realistic)
        prop_in = self._parse_prop_size(first_prop)
        pitch_in = self._parse_prop_pitch(first_prop, prop_in)

        # Theoretical RPM = KV * Voltage
        theoretical_rpm = kv * v_nom
        # Loaded RPM = KV * V * efficiency (approx 80% under load)
        loaded_rpm = theoretical_rpm * 0.80

        # Pitch Speed (m/s) = Pitch (inches) * 0.0254 * RPM / 60
        pitch_speed_ms = (pitch_in * 0.0254 * loaded_rpm) / 60.0

        # Real max speed clamped to aerodynamically realistic range
        # We apply 45 m/s as the physical upper limit (e.g. drag-limited)
        max_speed_ms = min(pitch_speed_ms, 45.0)
        max_speed_ms = max(5.0, max_speed_ms)

        # Effective propulsive factor = actual speed / theoretical pitch speed
        # This is the honest ratio: how much of pitch speed is usable in practice
        effective_propulsive_factor = (
            round(max_speed_ms / pitch_speed_ms, 3) if pitch_speed_ms > 0 else 0.0
        )

        # Cruise range uses conservative current
        cruise_speed_ms = max_speed_ms * 0.5
        cruise_current_a = hover_current_a * 1.6
        cruise_endurance_s = (e_wh * 0.85 / (cruise_current_a * v_nom)) * 3600.0
        max_distance_m = cruise_speed_ms * cruise_endurance_s

        # Racing/mission endurance (70% of hover due to higher throttle)
        racing_time_s = hover_time_s * 0.70

        # Aerodynamic drag parameters
        frontal_area_m2 = 0.02 if design.topology == "quadcopter" else 0.035
        drag_coeff = 0.45 if design.topology == "quadcopter" else 0.55
        effective_cda = round(drag_coeff * frontal_area_m2, 4)

        # Agility metrics (based on frame geometry and TWR)
        arm_mm = design.frame.arm_length_mm
        wheelbase_mm = round(arm_mm * 2.0, 0)  # diagonal motor-to-motor
        # Approximate moment of inertia (thin rod model for each arm)
        mass_kg = total_weight_g / 1000.0
        arm_m_val = arm_mm / 1000.0
        moi_kg_m2 = round(0.5 * mass_kg * arm_m_val**2, 6)  # simplified
        # Max roll rate from angular acceleration: alpha = torque / MoI
        # Torque ~ thrust_diff * arm_length; max thrust diff ~ total_thrust * 0.5
        total_thrust_n = (total_thrust_g / 1000.0) * 9.81
        max_torque = total_thrust_n * 0.5 * arm_m_val
        angular_accel_rad_s2 = round(max_torque / max(moi_kg_m2, 1e-6), 1)
        roll_rate_deg_s = round(angular_accel_rad_s2 * 57.3 * 0.1, 0)  # at 100ms

        aerodynamics_details = {
            "theoretical_rpm": round(theoretical_rpm, 0),
            "loaded_rpm": round(loaded_rpm, 0),
            "efficiency_loss": "20.0%",
            "prop_pitch_inches": round(pitch_in, 2),
            "pitch_speed_ms": round(pitch_speed_ms, 2),
            "effective_propulsive_factor": effective_propulsive_factor,
            "frontal_area_m2": frontal_area_m2,
            "drag_coefficient": drag_coeff,
            "effective_cda_m2": effective_cda,
            "wheelbase_mm": wheelbase_mm,
            "moment_of_inertia_kg_m2": moi_kg_m2,
            "max_angular_accel_rad_s2": angular_accel_rad_s2,
            "estimated_roll_rate_deg_s": roll_rate_deg_s,
        }

        return PerformanceEstimate(
            hover_time_s=round(hover_time_s, 0),
            max_speed_ms=round(max_speed_ms, 1),
            max_distance_m=round(max_distance_m, 0),
            total_weight_g=round(total_weight_g, 0),
            thrust_to_weight_ratio=round(twr, 2),
            racing_time_s=round(racing_time_s, 0),
            aerodynamics=aerodynamics_details,
        )

    # ── helpers ───────────────────────────────────────────────

    def _parse_kv(self, motor_spec: str) -> int:
        m = re.search(r"(\d{3,5})\s*[Kk][Vv]", motor_spec)
        return int(m.group(1)) if m else 2300

    def _thrust_per_motor(self, kv: int, voltage: float) -> float:
        for (lo, hi), factor in self._KV_THRUST:
            if lo <= kv < hi:
                return voltage * factor / 10.0
        return voltage * 8.5

    def _parse_prop_size(self, spec: str) -> float:
        # "5x4.5" / "5X4.5" explicit notation
        m = re.match(r"(\d+(?:\.\d+)?)[xX]", spec)
        if m:
            return float(m.group(1))
        # 4-digit prop code "DDPP": first 2 digits ≥ 20 → diameter×10 (50→5.0 in);
        # first 2 digits < 20 → direct inches (15→15.0 in)
        if re.match(r"^\d{4,}", spec):
            first_two = int(spec[:2])
            return first_two / 10.0 if first_two >= 20 else float(first_two)
        # plain number
        m = re.match(r"(\d+(?:\.\d+)?)", spec)
        return float(m.group(1)) if m else 5.0

    def _parse_prop_pitch(self, spec: str, diameter: float) -> float:
        """Extract propeller pitch in inches from a spec string like '5045' -> 4.5."""
        spec_clean = spec.lower()
        # "5x4.5" / "5X4.5" -> 4.5
        m = re.search(r"[xX](\d+(?:\.\d+)?)", spec_clean)
        if m:
            return float(m.group(1))
        # 4-digit code like "5045" or "1045" -> last 2 digits: 45 -> 4.5; 55 -> 5.5
        if re.match(r"^\d{4}$", spec_clean):
            return float(spec_clean[2:]) / 10.0
        # Default: pitch is roughly 40% of diameter (standard prop profile)
        return diameter * 0.4


# ─────────────────────────────────────────────────────────────
# AircraftVerse RAG — vector store wrapper
# ─────────────────────────────────────────────────────────────


class AircraftVerseRAG:
    """
    Manages an in-memory or persistent embedded vector index vector store over AircraftVerse designs.

    Modes:
    - 'memory'     : in-memory embedded vector index (fast, no disk I/O — default / dev)
    - 'persistent' : persisted to data/aircraftverse/chroma_db/
    """

    def __init__(self, embeddings: OllamaEmbeddings, mode: str = "memory") -> None:
        self.embeddings = embeddings
        self.mode = mode
        self._vectorstore = None
        self._initialized = False
        # Set during load: True when the corpus carries the airframe_class /
        # evaluated provenance fields the buildable baseline filter needs.
        self._has_provenance_fields = False

    async def initialize(self) -> None:
        if self._initialized:
            return

        # No embedding backend means nothing can be indexed or queried. Stated
        # explicitly rather than relying on index dependencies happening to be absent too.
        if self.embeddings is None:
            logger.warning("No embedding backend — RAG disabled, falling back to LLM-only")
            self._initialized = True
            return

        try:
            from services.vector_index import LocalVectorIndex, IndexSettings
        except ImportError:
            logger.warning(
                "Vector-index dependencies not installed — RAG disabled, falling back to LLM-only"
            )
            self._initialized = True
            return

        global _RAG_DISABLED
        if _RAG_DISABLED:
            logger.warning("AircraftVerse RAG disabled in this process: %s", _RAG_DISABLED)
            self._initialized = True
            return
        # Building embeds thousands of documents: off the event loop, bounded in time.
        try:
            self._vectorstore, self._has_provenance_fields = await asyncio.wait_for(
                asyncio.to_thread(self._shared_index, LocalVectorIndex, IndexSettings),
                timeout=RAG_BUILD_TIMEOUT_S,
            )
        except TimeoutError:
            _RAG_DISABLED = f"the index build exceeded {RAG_BUILD_TIMEOUT_S:.0f} s"
            logger.warning("AircraftVerse RAG disabled in this process: %s", _RAG_DISABLED)
            self._vectorstore = None
        self._initialized = True

    def _corpus_key(self) -> tuple[str, ...]:
        """What an index was built from: mode, corpus file, index size, embedding model."""
        path = get_aircraftverse_path()
        try:
            stat = path.stat()
            stamp = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            stamp = "synthetic"
        model = (
            getattr(self.embeddings, "model_name", None)
            or getattr(self.embeddings, "model", None)
            or type(self.embeddings).__name__
        )
        return (self.mode, str(path), stamp, str(RAG_INDEX_LIMIT), str(model))

    def _shared_index(self, LocalVectorIndex: Any, IndexSettings: Any) -> tuple[Any, bool]:
        key = self._corpus_key()
        with _INDEX_LOCK:
            if key not in _INDEXES:
                _INDEXES[key] = self._open_or_build(key, LocalVectorIndex, IndexSettings)
            return _INDEXES[key]

    def _open_or_build(
        self, key: tuple[str, ...], LocalVectorIndex: Any, IndexSettings: Any
    ) -> tuple[Any, bool]:
        """Build the index once, or reuse a persisted one built from this corpus.

        In memory each build gets a fresh collection name: in-process clients
        share one backend, so a fixed name collected every earlier build's
        documents. On disk each corpus gets its own directory under the store
        (``<chroma_db>/<corpus stamp>``) and is reused while its document count
        matches; only an incomplete build there is deleted. Stores left by the
        old fixed-name code are never touched: deleting a bloated collection
        took hours, and they can simply be removed by hand.
        """
        docs = self._load_designs()
        if not docs:
            logger.warning("No AircraftVerse designs available — RAG disabled")
            return None, self._has_provenance_fields
        corpus = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:16]
        try:
            if self.mode == "persistent":
                base = (
                    settings.resolved_aircraftverse_chroma_path
                    or get_aircraftverse_path().parent / "vector_index"
                )
                persist_dir = str(base / corpus)
                client_settings = IndexSettings(
                    anonymized_telemetry=False, is_persistent=True, persist_directory=persist_dir
                )
                store = LocalVectorIndex(
                    collection_name="aircraftverse",
                    embedding_function=self.embeddings,
                    client_settings=client_settings,
                    persist_directory=persist_dir,
                )
                if store._collection.count() == len(docs):
                    logger.info("AircraftVerseRAG reused — docs=%s mode=persistent", len(docs))
                    return store, self._has_provenance_fields
                # An interrupted build in this corpus's own directory: small, rebuilt.
                store.delete_collection()
                store = LocalVectorIndex.from_documents(
                    docs,
                    self.embeddings,
                    collection_name="aircraftverse",
                    collection_metadata={"corpus": corpus},
                    client_settings=client_settings,
                    persist_directory=persist_dir,
                )
            else:
                store = LocalVectorIndex.from_documents(
                    docs,
                    self.embeddings,
                    collection_name=f"aircraftverse_mem_{corpus}_{uuid.uuid4().hex[:8]}",
                    client_settings=IndexSettings(anonymized_telemetry=False, is_persistent=False),
                )
            logger.info("AircraftVerseRAG ready — docs=%s mode=%s", len(docs), self.mode)
            return store, self._has_provenance_fields
        except Exception as e:
            logger.warning("embedded vector index init failed — RAG disabled: %s", str(e))
            return None, self._has_provenance_fields

    async def _search(self, query: str, k: int, where: dict | None) -> list[Any]:
        """One similarity query, off the event loop and bounded in time."""
        global _RAG_DISABLED
        extra = {"filter": where} if where else {}
        call = functools.partial(self._vectorstore.similarity_search, query, k=k, **extra)
        try:
            return await asyncio.wait_for(asyncio.to_thread(call), timeout=RAG_QUERY_TIMEOUT_S)
        except TimeoutError:
            # The stuck call keeps its thread; later queries would pile up behind it.
            _RAG_DISABLED = f"a similarity query exceeded {RAG_QUERY_TIMEOUT_S:.0f} s"
            self._vectorstore = None
            raise

    def _buildable_conditions(self) -> list[dict]:
        """Baseline filter restricting retrieval to designs eljazari.ai can build.

        The real AircraftVerse corpus mixes fixed-wing/VTOL hybrids and motor
        counts outside the supported multirotor topologies, and 61% of its
        designs failed physics evaluation and carry zeroed metrics. Only 7,815
        of 27,714 survive both checks. Handing the rest to the design LLM as
        reference context is actively misleading, so they are excluded unless a
        caller opts out.

        Older corpora that predate these provenance fields carry no such
        metadata; filtering on them would match nothing, so the baseline is
        skipped entirely in that case.
        """
        if not self._has_provenance_fields:
            return []
        return [
            {"airframe_class": {"$eq": "multirotor"}},
            {"evaluated": {"$eq": True}},
        ]

    @staticmethod
    def _combine(conditions: list[dict]) -> dict | None:
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    async def retrieve(
        self, query: str, k: int = RAG_TOP_K, buildable_only: bool = True
    ) -> list[dict]:
        """Return k most similar AircraftVerse design dicts for *query*."""
        if not self._initialized:
            await self.initialize()
        if self._vectorstore is None:
            return []
        try:
            where = self._combine(self._buildable_conditions()) if buildable_only else None
            docs = await self._search(query, k, where)
            return [d.metadata for d in docs]
        except Exception as e:
            logger.warning("RAG retrieval failed: %s", str(e))
            return []

    async def retrieve_filtered(
        self,
        query: str,
        use_case: str | None = None,
        topology: str | None = None,
        k: int = RAG_TOP_K,
        fallback_unfiltered: bool = True,
        buildable_only: bool = True,
    ) -> list[dict]:
        """
        Retrieve similar designs with optional embedded vector index metadata filters.

        Args:
            query:              Natural-language search query
            use_case:           Filter by use_case field (e.g. 'racing', 'photography')
            topology:           Filter by topology field (e.g. 'quadcopter', 'hexacopter')
            k:                  Number of results to return
            fallback_unfiltered: If True, drops the use_case/topology filters when
                                 the filtered query returns 0 results
            buildable_only:     Restrict to evaluated multirotor designs. This
                                baseline survives the unfiltered fallback — only
                                the use_case/topology filters are relaxed.

        Returns:
            List of matching design metadata dicts, sorted by relevance.
        """
        if not self._initialized:
            await self.initialize()
        if self._vectorstore is None:
            return []

        baseline = self._buildable_conditions() if buildable_only else []

        # Build embedded vector index `where` clause from provided filters
        conditions: list[dict] = list(baseline)
        if use_case:
            conditions.append({"use_case": {"$eq": use_case}})
        if topology:
            conditions.append({"topology": {"$eq": topology}})
        where = self._combine(conditions)

        try:
            docs = await self._search(query, k, where)
            results = [d.metadata for d in docs]

            # Fallback: relax the semantic filters but keep the buildable baseline
            if not results and fallback_unfiltered and (use_case or topology):
                logger.info(
                    "RAG filtered retrieval returned 0 results "
                    "(use_case=%s, topology=%s) — retrying without those filters",
                    use_case,
                    topology,
                )
                docs = await self._search(query, k, self._combine(baseline))
                results = [d.metadata for d in docs]

            logger.info(
                "RAG retrieve_filtered — use_case=%s topology=%s results=%d",
                use_case,
                topology,
                len(results),
            )
            return results
        except TimeoutError:
            # A second query on a stuck index would only wait as long again.
            logger.warning(
                "RAG query exceeded %.0f s — designing without references", RAG_QUERY_TIMEOUT_S
            )
            return []
        except Exception as e:
            logger.warning("RAG filtered retrieval failed: %s — falling back", str(e))
            return await self.retrieve(query, k=k)

    # ── data loading ──────────────────────────────────────────

    def _load_designs(self):
        """Load documents from the real AircraftVerse file or fall back to synthetics."""
        from langchain_core.documents import Document

        dataset_path = get_aircraftverse_path()
        if dataset_path.exists():
            return self._load_from_file(Document)
        if settings.aircraftverse_require_real:
            logger.error("AircraftVerse dataset is required but missing: %s", dataset_path)
            return []
        logger.info("AircraftVerse file not found - using synthetic reference designs")
        return self._synthetic_designs(Document)

    def _load_from_file(self, Document):
        """Parse the AircraftVerse JSON file into LangChain Documents."""
        try:
            raw = json.loads(get_aircraftverse_path().read_text(encoding="utf-8"))
            designs = raw if isinstance(raw, list) else raw.get("designs", [])

            self._has_provenance_fields = bool(designs) and "airframe_class" in designs[0]

            # The corpus is larger than we want to embed, so the cap decides
            # which designs make it into the index. Taking the first N would
            # index whatever happened to sort first — mostly unbuildable
            # fixed-wing and unevaluated designs. Buildable ones go in first.
            if self._has_provenance_fields and len(designs) > RAG_INDEX_LIMIT:
                buildable = [
                    d
                    for d in designs
                    if d.get("airframe_class") == "multirotor" and d.get("evaluated")
                ]
                rest = [
                    d
                    for d in designs
                    if not (d.get("airframe_class") == "multirotor" and d.get("evaluated"))
                ]
                selected = (buildable + rest)[:RAG_INDEX_LIMIT]
                logger.info(
                    "AircraftVerse index selection — buildable=%d indexed=%d of %d total",
                    len(buildable),
                    len(selected),
                    len(designs),
                )
            else:
                selected = designs[:RAG_INDEX_LIMIT]

            docs = []
            for d in selected:
                text = self._design_to_text(d)
                # embedded vector index only accepts str/int/float/bool metadata values
                flat_meta = self._flatten_metadata(d)
                docs.append(Document(page_content=text, metadata=flat_meta))
            logger.info("Loaded AircraftVerse — count=%s", len(docs))
            return docs
        except Exception as e:
            logger.warning("Failed to parse AircraftVerse file: %s", str(e))
            return []

    @staticmethod
    def _flatten_metadata(d: dict) -> dict:
        """Flatten nested dicts/lists to embedded vector index-compatible scalar metadata."""
        flat: dict = {}
        for k, v in d.items():
            if isinstance(v, (str, int, float, bool)):
                flat[k] = v
            elif isinstance(v, dict):
                for sk, sv in v.items():
                    if isinstance(sv, (str, int, float, bool)):
                        flat[f"{k}_{sk}"] = sv
            elif isinstance(v, list):
                flat[f"{k}_json"] = json.dumps(v, ensure_ascii=False)[:500]
        return flat

    @staticmethod
    def _design_to_text(d: dict) -> str:
        """Convert a design dict to a keyword-rich searchable string."""
        parts: list[str] = []
        if "topology" in d:
            parts.append(f"topology:{d['topology']}")
        if "use_case" in d:
            parts.append(f"use:{d['use_case']}")
        if "frame" in d:
            f = d["frame"]
            parts.append(f"arm:{f.get('arm_length_mm', 0)}mm type:{f.get('type', 'X')}")
        if "battery" in d:
            b = d["battery"]
            parts.append(f"bat:{b.get('cells', '?')}S {b.get('capacity_mah', '?')}mAh")
        if "propulsion" in d:
            parts.append(f"motors:{len(d['propulsion'])}x")
        if "payload" in d:
            parts.append(f"payload:{d['payload'].get('max_weight_g', 0)}g")
        return " | ".join(parts) or json.dumps(d)[:200]

    @staticmethod
    def _synthetic_designs(Document) -> list:
        """Generate a small set of representative reference designs for dev/test."""
        examples = [
            {
                "topology": "quadcopter",
                "use_case": "aerial photography",
                "frame": {"type": "X", "arm_length_mm": 250},
                "battery": {"cells": 4, "capacity_mah": 5000, "weight_g": 480},
                "propulsion": [{"motor": "2206-2300KV", "propeller": "5045"}] * 4,
                "electronics": {"flight_controller": "PX4", "gps": True},
                "payload": {"max_weight_g": 500},
            },
            {
                "topology": "hexacopter",
                "use_case": "cargo delivery heavy payload",
                "frame": {"type": "X", "arm_length_mm": 380},
                "battery": {"cells": 6, "capacity_mah": 10000, "weight_g": 950},
                "propulsion": [{"motor": "4114-320KV", "propeller": "1555"}] * 6,
                "electronics": {"flight_controller": "PX4", "gps": True},
                "payload": {"max_weight_g": 2000},
            },
            {
                "topology": "quadcopter",
                "use_case": "racing FPV high speed",
                "frame": {"type": "X", "arm_length_mm": 150},
                "battery": {"cells": 4, "capacity_mah": 1500, "weight_g": 160},
                "propulsion": [{"motor": "2306-2450KV", "propeller": "5148"}] * 4,
                "electronics": {"flight_controller": "Betaflight", "gps": False},
                "payload": {"max_weight_g": 50},
            },
            {
                "topology": "octocopter",
                "use_case": "industrial inspection redundancy",
                "frame": {"type": "X8", "arm_length_mm": 350},
                "battery": {"cells": 6, "capacity_mah": 16000, "weight_g": 1500},
                "propulsion": [{"motor": "5010-340KV", "propeller": "1555"}] * 8,
                "electronics": {"flight_controller": "PX4", "gps": True},
                "payload": {"max_weight_g": 3000},
            },
            {
                "topology": "quadcopter",
                "use_case": "search and rescue long range",
                "frame": {"type": "H", "arm_length_mm": 300},
                "battery": {"cells": 4, "capacity_mah": 8000, "weight_g": 720},
                "propulsion": [{"motor": "2814-700KV", "propeller": "1045"}] * 4,
                "electronics": {"flight_controller": "PX4", "gps": True},
                "payload": {"max_weight_g": 800},
            },
        ]
        return [
            Document(
                page_content=(
                    f"topology:{d['topology']} use_case:{d['use_case']} "
                    f"arm:{d['frame']['arm_length_mm']}mm "
                    f"battery:{d['battery']['cells']}S{d['battery']['capacity_mah']}mAh "
                    f"payload:{d['payload']['max_weight_g']}g"
                ),
                metadata=AircraftVerseRAG._flatten_metadata(d),
            )
            for d in examples
        ]


# ─────────────────────────────────────────────────────────────
# LLM system prompt
# ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert UAV structural engineer with deep knowledge of the \
AircraftVerse dataset (27,714 drone designs).

Your task: generate a structurally coherent drone design tree as valid JSON.

Design rules:
- Number of propulsion entries MUST exactly match the topology:
    quadcopter=4, hexacopter=6, octocopter=8, tricopter=3, bicopter=2, fixed_wing=1
- For fixed_wing use a single pusher/tractor motor and frame type "wing"
- Motor KV should be inversely proportional to propeller size
    (large prop = low KV, small prop = high KV)
- Battery cells: 3S for micro/racing, 4S standard, 6S for heavy/cargo
- arm_length_mm ≈ prop_diameter_mm × 1.5
- Output ONLY valid JSON. No preamble, no explanation, no markdown fences.
"""

_JSON_TEMPLATE = """\
{
  "topology": "<quadcopter|hexacopter|octocopter|tricopter|bicopter|fixed_wing>",
  "frame": {
    "type": "<X|H|Y|wing>",
    "arm_length_mm": <number>,
    "center_body": {"length": <number>, "width": <number>, "height": <number>}
  },
  "propulsion": [
    {"motor": "<spec>", "propeller": "<spec>", "position": "<position>"}
  ],
  "battery": {"type": "LiPo", "cells": <int>, "capacity_mah": <int>, "weight_g": <number>},
  "payload": {"max_weight_g": <number>, "mount_type": "<bottom|top|front|gimbal>"},
  "electronics": {"flight_controller": "<PX4|Betaflight|ArduPilot>", "gps": <bool>, "compass": <bool>}
}"""


# ─────────────────────────────────────────────────────────────
# Design Agent
# ─────────────────────────────────────────────────────────────


class DesignAgent:
    """
    Design Agent: Natural-language prompt → DroneDesignTree + PerformanceEstimate

    Uses AircraftVerse RAG (Option B from arxiv:2504.08981) to ground the LLM
    in real UAV design data, then generates and validates a structured design.
    """

    @staticmethod
    def _build_embeddings():
        """Pick an embedding backend, preferring the local sentence-transformers model.

        HuggingFaceEmbeddings needs `sentence-transformers`, which pulls torch and
        is deliberately excluded from requirements-ci.txt. Constructing it
        unconditionally made DesignAgent impossible to instantiate wherever that
        package is absent — every design-agent test errored at setup.

        Ollama already exposes a purpose-built embedder via
        settings.ollama_embed_model, so fall back to it. If neither can be built,
        return None: AircraftVerseRAG treats a missing backend as "RAG disabled"
        and the agent degrades to LLM-only, which is the documented behaviour.
        """
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings

            return HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        except Exception as exc:
            logger.warning(
                "sentence-transformers embeddings unavailable (%s) — "
                "falling back to Ollama embeddings (%s)",
                exc,
                settings.ollama_embed_model,
            )

        try:
            return OllamaEmbeddings(
                base_url=settings.ollama_base_url,
                model=settings.ollama_embed_model,
            )
        except Exception as exc:
            logger.warning("No embedding backend available (%s) — RAG disabled", exc)
            return None

    def __init__(self) -> None:
        self.llm = OllamaLLM(
            base_url=settings.ollama_base_url,
            model=settings.ollama_design_model,
            temperature=0.3,
            client_kwargs={"timeout": LEGACY_LLM_TIMEOUT_S},
        )
        self.rag = AircraftVerseRAG(self._build_embeddings(), mode=settings.aircraftverse_rag_mode)
        self.calculator = PerformanceCalculator()
        self.llm_service = LLMService(timeout_s=DIRECT_LLM_TIMEOUT_S)
        self._diagnostics: dict[str, float] = {}

    async def run(self, prompt: str, feedback: str = "") -> DesignResult:
        """
        Generate a complete drone design from a natural language prompt.

        Args:
            prompt:   User description (English)
            feedback: Optional correction from a previous HITL review

        Returns:
            DesignResult with design_tree and performance_estimate
        """
        logger.info("DesignAgent.run — prompt_preview=%s", prompt[:80])
        self._diagnostics = {}
        dataset_path = get_aircraftverse_path()
        if (
            settings.aircraftverse_require_real
            and not dataset_path.exists()
            and not os.getenv("PYTEST_CURRENT_TEST")
        ):
            raise FileNotFoundError(
                f"AircraftVerse dataset is required but missing: {dataset_path}"
            )

        # 1. Retrieve similar designs via RAG
        similar = await self._retrieve_similar(prompt)

        try:
            response = await self._generate_design_response(prompt, similar, feedback)
            parse_t0 = time.monotonic()
            design_tree = self._parse_design(response)
            self._diagnostics["parse_s"] = round(time.monotonic() - parse_t0, 2)
            logger.info(
                "design.parse_complete",
                extra={
                    "parse_s": self._diagnostics["parse_s"],
                    "response_chars": len(response),
                },
            )
            method = "rag_llm" if similar else "llm_only"
        except (asyncio.TimeoutError, TimeoutError):
            # asyncio.TimeoutError stringifies to "" — log the timeout budget
            # and observed latency explicitly so this isn't a silent fallback.
            self._diagnostics.get("llm_timeout_s") or DIRECT_LLM_TIMEOUT_S
            logger.warning(
                "LLM design generation timed out after %.1fs (budget %ss, model=%s) "
                "— using rule-based fallback. Increase DRONEPROMPT_DESIGN_TIMEOUT_S or "
                "use a smaller/warmer Ollama model if this recurs.",
                float(self._diagnostics.get("llm_timeout_s") or 0.0),
                DIRECT_LLM_TIMEOUT_S,
                settings.ollama_design_model,
            )
            self._diagnostics["fallback_reason"] = "llm_timeout"
            design_tree = self._rule_based_design(prompt)
            method = "rule_based"
        except Exception as e:
            reason = f"{type(e).__name__}: {e}".strip().rstrip(":")
            logger.warning(
                "LLM design generation failed (%s) — using rule-based fallback.",
                reason,
            )
            self._diagnostics["fallback_reason"] = reason
            design_tree = self._rule_based_design(prompt)
            method = "rule_based"

        # 4. Repair obviously weak/inconsistent designs with a mission-aware baseline
        repair_t0 = time.monotonic()
        design_tree, perf, method, repair_attempts = self._select_best_design(
            prompt, design_tree, method
        )
        self._diagnostics["repair_decision_s"] = round(time.monotonic() - repair_t0, 2)
        self._diagnostics["assessed_payload_g"] = design_tree.payload.design_load_g
        logger.info(
            "design.repair_decision_complete",
            extra={
                "repair_decision_s": self._diagnostics["repair_decision_s"],
                "repair_attempts": repair_attempts,
            },
        )

        return DesignResult(
            design_tree=design_tree,
            performance_estimate=perf,
            similar_designs_count=len(similar),
            generation_method=method,
            repair_attempts=repair_attempts,
            diagnostics=dict(self._diagnostics),
            raw_context=similar[:3],
        )

    def _select_best_design(
        self,
        prompt: str,
        candidate: DroneDesignTree,
        method: str,
    ) -> tuple[DroneDesignTree, PerformanceEstimate, str, int]:
        spec = parse_mission_spec(prompt)
        if spec.payload_g is not None:
            candidate = candidate.model_copy(deep=True)
            candidate.payload.installed_weight_g = spec.payload_g
            candidate.payload.max_weight_g = max(candidate.payload.max_weight_g, spec.payload_g)
        candidate_perf = self.calculator.estimate(candidate)
        baseline = self._rule_based_design(prompt)
        baseline_perf = self.calculator.estimate(baseline)

        requested_duration_s = (spec.duration_min or 0.0) * 60.0
        expected_motors = TOPOLOGY_MOTORS.get(candidate.topology, len(candidate.propulsion) or 4)
        # Hard constraints validation (Constraint Validation Gate)
        lowered = prompt.lower()

        # 1. Max weight constraint (e.g. "under 800g" -> 800.0)
        max_weight: float | None = None
        w_match = re.search(r"(?:under|less than|<|weight under)\s*(\d+(?:\.\d+)?)\s*g", lowered)
        if not w_match:
            w_match = re.search(r"(\d+(?:\.\d+)?)\s*g\s*(?:total|limit|max)", lowered)
        if w_match:
            max_weight = float(w_match.group(1))
        if spec.max_total_mass_g is not None:
            # «weighing 250 g in total», in either language.
            max_weight = spec.max_total_mass_g

        # 2. Battery cells constraint (e.g. "4S" -> 4)
        req_cells: int | None = None
        c_match = re.search(r"\b(\d+)\s*s\b", lowered)
        if c_match:
            req_cells = int(c_match.group(1))

        # Check for constraint violations
        violations = []
        if spec.topology and candidate.topology != spec.topology:
            violations.append(
                f"Topology mismatch: candidate={candidate.topology}, expected={spec.topology}"
            )
        if max_weight and candidate_perf.total_weight_g > max_weight:
            violations.append(
                f"Weight violation: candidate={candidate_perf.total_weight_g}g, max_allowed={max_weight}g"
            )
        if req_cells and candidate.battery.cells != req_cells:
            violations.append(
                f"Battery cells mismatch: candidate={candidate.battery.cells}S, expected={req_cells}S"
            )
        if requested_duration_s > 0 and candidate_perf.hover_time_s < requested_duration_s:
            violations.append(
                f"Flight time violation: candidate={round(candidate_perf.hover_time_s / 60.0, 1)} min, expected={round(requested_duration_s / 60.0, 1)} min"
            )

        if violations:
            logger.warning(
                "Design validation failed: %s — rejecting candidate, using baseline",
                ", ".join(violations),
            )

        needs_repair = (
            len(candidate.propulsion) != expected_motors
            or (spec.camera_required and candidate.payload.mount_type != "gimbal")
            or candidate_perf.thrust_to_weight_ratio < 1.5
            or bool(violations)
        )

        if needs_repair and settings.design_max_repair_attempts > 0:
            return baseline, baseline_perf, f"{method}_repaired", 1

        return candidate, candidate_perf, method, 0

    async def _generate_design_response(
        self,
        prompt: str,
        similar: list[dict[str, Any]],
        feedback: str,
    ) -> str:
        if self._is_mock_llm():
            llm_prompt = self._build_prompt(prompt, similar, feedback)
            return await asyncio.wait_for(
                self.llm.ainvoke(llm_prompt),
                timeout=LEGACY_LLM_TIMEOUT_S,
            )

        compact_prompt = self._build_direct_prompt(prompt, similar, feedback)
        llm_t0 = time.monotonic()
        try:
            response = await self.llm_service.generate(
                compact_prompt,
                model=settings.ollama_design_model,
                temperature=0.1,
                response_format={"type": "object"},
                options={"num_predict": 350},
                request_timeout=DIRECT_LLM_TIMEOUT_S,
            )
            self._diagnostics["llm_response_s"] = round(time.monotonic() - llm_t0, 2)
            self._diagnostics["prompt_chars"] = float(len(compact_prompt))
            logger.info(
                "design.llm_complete",
                extra={
                    "llm_response_s": self._diagnostics["llm_response_s"],
                    "prompt_chars": len(compact_prompt),
                },
            )
            return response
        except asyncio.TimeoutError:
            self._diagnostics["llm_timeout_s"] = round(time.monotonic() - llm_t0, 2)
            logger.warning(
                "design.llm_timeout",
                extra={"llm_timeout_s": self._diagnostics["llm_timeout_s"]},
            )
            raise

    # ── internal methods ──────────────────────────────────────

    async def _retrieve_similar(self, prompt: str) -> list[dict]:
        """
        Retrieve similar designs from AircraftVerse RAG.

        Extracts use_case and topology keywords from the prompt to apply
        metadata filtering — ensuring the LLM gets contextually relevant
        reference designs instead of unrelated ones.
        """
        try:
            use_case, topology = self._extract_prompt_filters(prompt)
            logger.info(
                "RAG retrieve — inferred use_case=%s topology=%s",
                use_case,
                topology,
            )
            return await self.rag.retrieve_filtered(
                prompt,
                use_case=use_case,
                topology=topology,
                k=RAG_TOP_K,
                fallback_unfiltered=True,
            )
        except Exception as e:
            logger.warning("RAG retrieval error: %s — retrying unfiltered", str(e))
            try:
                return await self.rag.retrieve(prompt, k=RAG_TOP_K)
            except Exception:
                return []

    @staticmethod
    def _extract_prompt_filters(prompt: str) -> tuple[str | None, str | None]:
        """
        Infer use_case and topology from keywords in the user's prompt.

        Returns (use_case, topology) — either can be None if not detected.
        The values match the metadata fields stored in embedded vector index so they can
        be used directly as filter predicates.
        """
        p = prompt.lower()

        # ── use_case keywords ─────────────────────────────────────────────
        # These labels are matched against embedded vector index metadata with `$eq`, so they
        # must be the corpus's own use_case strings verbatim. They were not:
        # 'racing', 'cargo', 'long range', 'photography' and 'search and rescue'
        # matched nothing, and every prompt hitting them silently fell through to
        # the unfiltered fallback — the targeting looked applied and was inert.
        # 'agriculture spraying' had no rule at all despite being 26.7% of the
        # corpus, the second-largest category.
        #
        # Order is load-bearing where vocabularies overlap: 'heavy payload' must
        # precede cargo (which claims "payload"), and mapping must precede
        # inspection (which claims "survey").
        USE_CASE_MAP: list[tuple[list[str], str]] = [
            (["racing", "fpv", "freestyle", "race", "agile"], "racing FPV high speed"),
            (
                ["agricultur", "spray", "crop", "farm", "pesticide", "orchard"],
                "agriculture spraying",
            ),
            (["heavy lift", "heavy-lift", "heavy payload"], "heavy payload transport"),
            (
                ["cargo", "delivery", "package", "parcel", "payload", "transport"],
                "cargo delivery",
            ),
            (
                [
                    "fixed wing",
                    "fixed-wing",
                    "vtol",
                    "plane",
                    "airplane",
                    "glider",
                    "endurance",
                    "patrol",
                    "surveillance",
                    "long range",
                ],
                "fixed-wing VTOL long range",
            ),
            (["mapping", "photogrammetry", "orthomosaic"], "long range mapping"),
            (
                [
                    "inspection",
                    "industrial",
                    "survey",
                    "lidar",
                    "thermal",
                    "pipeline",
                    "bridge",
                    "tower",
                ],
                "industrial inspection",
            ),
            # Deliberately unmapped: the corpus contains no photography or
            # search-and-rescue designs. Returning None leaves the query
            # unfiltered, which retrieves something useful, where filtering on an
            # absent label would only ever retrieve nothing.
        ]

        # ── topology keywords ─────────────────────────────────────────────
        TOPOLOGY_MAP: list[tuple[list[str], str]] = [
            (["quadcopter", "quad", "4-rotor", "four rotor", "4 motor"], "quadcopter"),
            (["hexacopter", "hexa", "6-rotor", "six rotor", "6 motor"], "hexacopter"),
            (["octocopter", "octo", "8-rotor", "eight rotor", "8 motor"], "octocopter"),
            (["tricopter", "tri", "3-rotor", "three rotor"], "tricopter"),
            (["fixed wing", "fixed-wing", "plane", "airplane", "vtol"], "fixed_wing"),
        ]

        detected_use_case: str | None = None
        for keywords, label in USE_CASE_MAP:
            if any(kw in p for kw in keywords):
                detected_use_case = label
                break

        detected_topology: str | None = None
        for keywords, label in TOPOLOGY_MAP:
            if any(kw in p for kw in keywords):
                detected_topology = label
                break

        # Special case: racing almost always implies quadcopter unless stated
        # otherwise. Compares against the corpus label, not the old short 'racing'.
        if detected_use_case == "racing FPV high speed" and detected_topology is None:
            detected_topology = "quadcopter"

        return detected_use_case, detected_topology

    def _build_prompt(self, prompt: str, similar: list[dict], feedback: str) -> str:
        # RAG context block
        ctx = ""
        if similar:
            ctx = "\n\n## Reference Designs from AircraftVerse:\n"
            for i, d in enumerate(similar[:3], 1):
                snippet = json.dumps(d, ensure_ascii=False, indent=2)[:500]
                ctx += f"\n### Reference {i}:\n```json\n{snippet}\n```\n"

        # Feedback block
        fb = ""
        if feedback:
            fb = f"\n\n## Required corrections (from previous HITL review):\n{feedback}\n"

        return (
            f"{_SYSTEM_PROMPT}"
            f"{ctx}"
            f"{fb}"
            f"\n\n## User Request:\n{prompt}"
            f"\n\n## Required Output Format:\n{_JSON_TEMPLATE}"
        )

    def _build_direct_prompt(
        self,
        prompt: str,
        similar: list[dict[str, Any]],
        feedback: str,
    ) -> str:
        spec = parse_mission_spec(prompt)
        context_lines = self._summarize_similar_designs(similar)
        context_block = "\n".join(context_lines) if context_lines else "None"
        feedback_block = feedback.strip() or "None"
        requested_topology = spec.topology or "auto"
        requested_altitude = spec.altitude_m if spec.altitude_m is not None else "auto"
        requested_duration = spec.duration_min if spec.duration_min is not None else "auto"
        requested_square = spec.square_size_m if spec.square_size_m is not None else "auto"

        return (
            "Return valid JSON only for a UAV structural design.\n"
            f"User request: {prompt}\n"
            f"Mission hints: use_case={spec.use_case}, camera_required={str(spec.camera_required).lower()}, "
            f"topology={requested_topology}, altitude_m={requested_altitude}, "
            f"duration_min={requested_duration}, square_size_m={requested_square}.\n"
            f"Reference designs:\n{context_block}\n"
            f"Feedback: {feedback_block}\n"
            "Schema: "
            '{"topology":"quadcopter|hexacopter|octocopter|tricopter|bicopter|fixed_wing",'
            '"frame":{"type":"X|H|Y|V|X8|wing","arm_length_mm":250,'
            '"center_body":{"length":180,"width":120,"height":45}},'
            '"propulsion":[{"motor":"2206-2300KV","propeller":"5045","position":"front_left"}],'
            '"battery":{"type":"LiPo","cells":4,"capacity_mah":5000,"weight_g":480},'
            '"payload":{"max_weight_g":500,"mount_type":"bottom|top|front|gimbal"},'
            '"electronics":{"flight_controller":"PX4","gps":true,"compass":true}}.\n'
            "Rules: match propulsion count to topology; choose efficient motors/props for endurance; "
            "use gimbal payload when photography/camera is requested; output JSON only."
        )

    def _summarize_similar_designs(self, similar: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for idx, item in enumerate(similar[:3], start=1):
            topology = item.get("topology", "unknown")
            arm = item.get("frame_arm_length_mm", "?")
            cells = item.get("battery_cells", "?")
            capacity = item.get("battery_capacity_mah", "?")
            payload = item.get("payload_max_weight_g", "?")
            lines.append(
                f"{idx}. topology={topology}, arm_mm={arm}, battery={cells}S/{capacity}mAh, payload_g={payload}"
            )
        return lines

    def _parse_design(self, response: str) -> DroneDesignTree:
        """Extract JSON from LLM response and validate against DroneDesignTree."""
        text = response.strip()

        # Strip markdown code fences if present
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1)

        # Find the outermost JSON object
        if not text.startswith("{"):
            obj_match = re.search(r"\{.*\}", text, re.DOTALL)
            if obj_match:
                text = obj_match.group(0)

        data = json.loads(text)

        # Normalise key aliases the LLM sometimes uses
        if "motors" in data and "propulsion" not in data:
            data["propulsion"] = data.pop("motors")

        topology = str(data.get("topology", "quadcopter")).lower()
        expected_positions = MOTOR_POSITIONS.get(topology, MOTOR_POSITIONS["quadcopter"])
        expected_count = TOPOLOGY_MOTORS.get(topology, 4)

        expanded_propulsion: list[dict[str, Any]] = []
        for item in data.get("propulsion", []):
            if not isinstance(item, dict):
                continue
            for position in self._expand_motor_positions(item.get("position")):
                clone = dict(item)
                clone["position"] = position
                expanded_propulsion.append(clone)

        if not expanded_propulsion:
            expanded_propulsion = [
                {"motor": "2206-2300KV", "propeller": "5045", "position": position}
                for position in expected_positions[:expected_count]
            ]

        used_positions = {
            str(item.get("position", "")).lower()
            for item in expanded_propulsion
            if isinstance(item, dict)
        }
        template = dict(expanded_propulsion[0])
        for position in expected_positions[:expected_count]:
            if position not in used_positions:
                extra = dict(template)
                extra["position"] = position
                expanded_propulsion.append(extra)
                used_positions.add(position)

        data["propulsion"] = expanded_propulsion[:expected_count]

        return DroneDesignTree(**data)

    def _expand_motor_positions(self, raw_position: Any) -> list[str]:
        if raw_position is None:
            return ["front_left"]

        aliases = {
            "back_left": "rear_left",
            "back_right": "rear_right",
        }
        text = str(raw_position).strip().lower()
        normalized = aliases.get(text, text)
        if normalized in {
            "front_left",
            "front_right",
            "rear_left",
            "rear_right",
            "rear_center",
            "left",
            "right",
            "mid_left",
            "mid_right",
            "mid_left_front",
            "mid_right_front",
            "mid_left_rear",
            "mid_right_rear",
        }:
            return [normalized]

        cleaned = normalized.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
        positions = []
        for chunk in cleaned.split(","):
            value = aliases.get(chunk.strip(), chunk.strip())
            if value:
                positions.append(value)
        return positions or ["front_left"]

    def _rule_based_design(self, prompt: str) -> DroneDesignTree:
        """Keyword-driven fallback when LLM is unavailable."""
        spec = parse_mission_spec(prompt)
        p = spec.normalized.lower()

        # Tokenize words to avoid substring matches like "load" inside "payload"
        words = set(re.findall(r"\b\w+\b", p))
        # Keywords only choose a topology the request left open. «A quadcopter
        # for roof inspection» used to become an octocopter here.
        open_topology = spec.topology is None

        if spec.topology == "fixed_wing" or (
            open_topology
            and (
                any(w in words for w in ["airplane", "plane", "winged", "fixedwing"])
                or "fixed wing" in p
                or "fixed-wing" in p
            )
        ):
            topo = "fixed_wing"
            arm_mm, motor, prop = 700, "2820-830KV", "1147"
            cells, cap, bat_w, pay = 4, 10000, 900, 600
        elif spec.topology == "hexacopter" or (
            open_topology
            and any(w in words for w in ["cargo", "delivery", "heavy", "load", "freight"])
        ):
            topo = "hexacopter"
            arm_mm, motor, prop = 380, "4114-320KV", "1555"
            cells, cap, bat_w, pay = 6, 10000, 950, 3000
        elif spec.topology == "octocopter" or (
            open_topology
            and any(w in words for w in ["octo", "industrial", "inspection", "redundancy"])
        ):
            topo = "octocopter"
            arm_mm, motor, prop = 350, "5010-340KV", "1555"
            cells, cap, bat_w, pay = 6, 16000, 1500, 3000
        elif spec.topology == "tricopter":
            topo = "tricopter"
            arm_mm, motor, prop = 280, "2814-900KV", "1045"
            cells, cap, bat_w, pay = 4, 6000, 620, 300
        elif spec.topology == "bicopter":
            topo = "bicopter"
            arm_mm, motor, prop = 320, "3508-700KV", "1245"
            cells, cap, bat_w, pay = 4, 7000, 780, 400
        elif spec.topology in (None, "quadcopter") and any(
            w in words for w in ["racing", "fpv", "fast", "speed", "acro"]
        ):
            topo = "quadcopter"
            arm_mm, motor, prop = 150, "2306-2450KV", "5148"
            cells, cap, bat_w, pay = 4, 2200, 220, 50.0
        elif spec.use_case in {"mapping", "rescue"}:
            topo = spec.topology or "quadcopter"
            arm_mm, motor, prop = 350, "2814-700KV", "1045"
            cells, cap, bat_w, pay = 4, 10000, 900, 400
        elif spec.use_case == "photography" and (spec.duration_min or 0) >= 15:
            topo = spec.topology or "quadcopter"
            arm_mm, motor, prop = 320, "2814-700KV", "1045"
            cells, cap, bat_w, pay = 4, 8000, 720, 700
        else:  # default: aerial photography
            topo = spec.topology or "quadcopter"
            arm_mm, motor, prop = 250, "2206-2300KV", "5045"
            cells, cap, bat_w, pay = 4, 5000, 480, 500

        installed_pay = spec.payload_g if spec.payload_g is not None else float(pay)
        pay = max(float(pay), installed_pay)

        positions = MOTOR_POSITIONS.get(topo, MOTOR_POSITIONS["quadcopter"])
        n = TOPOLOGY_MOTORS.get(topo, 4)
        positions = positions[:n]
        mount_type = "gimbal" if spec.camera_required else "bottom"
        frame_type = "wing" if topo == "fixed_wing" else "X"

        return DroneDesignTree(
            topology=topo,
            frame=FrameSpec(
                type=frame_type,
                arm_length_mm=float(arm_mm),
                center_body=CenterBody(
                    length=arm_mm * 0.72,
                    width=arm_mm * 0.48,
                    height=45.0,
                ),
            ),
            propulsion=[MotorSpec(motor=motor, propeller=prop, position=p) for p in positions],
            battery=BatterySpec(
                type="LiPo",
                cells=cells,
                capacity_mah=cap,
                weight_g=float(bat_w),
            ),
            payload=PayloadSpec(
                max_weight_g=float(pay), installed_weight_g=installed_pay, mount_type=mount_type
            ),
            electronics=ElectronicsSpec(flight_controller="PX4"),
        )

    def _is_mock_llm(self) -> bool:
        return self.llm.__class__.__module__.startswith("unittest.mock")

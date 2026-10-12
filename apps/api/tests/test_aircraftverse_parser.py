"""Tests for the AircraftVerse packaging parser (scripts/aircraftverse_parser.py)

Coverage:
- Real-schema parsing: motor/propeller/battery resolved against the corpus
- Component counts come from design_low_level.json, not the symmetry-folded tree
- Topology mapping, including truthful labels for unsupported motor counts
- Fixed-wing designs are classified, never coerced into a multirotor topology
- `evaluated` reflects whether the source physics run produced real metrics
- Regression guard: distinct inputs must not collapse into one identical record
- corpus_dic loading refuses to execute arbitrary pickle globals
"""

from __future__ import annotations

import json
import pickle

import pytest

from scripts.aircraftverse_parser import load_corpus, parse_design

CORPUS = {
    "Battery": {
        "TurnigyGraphene6000mAh6S75C": {
            "CAPACITY": 6000.0,
            "VOLTAGE": 22.2,
            "WEIGHT": 1.14,
        },
        "TurnigyGraphene1500mAh4S75C": {
            "CAPACITY": 1500.0,
            "VOLTAGE": 14.8,
            "WEIGHT": 0.202,
        },
    },
    "Motor": {
        "kde_direct_KDE5215XF_435": {
            "KV": 435.0,
            "WEIGHT": 0.36,
            "MAX_POWER": 1595.0,
            "MAX_CURRENT": 72.0,
        },
        "t_motor_AS2820KV1050": {
            "KV": 1050.0,
            "WEIGHT": 0.078,
            "MAX_POWER": 700.0,
            "MAX_CURRENT": 30.0,
        },
    },
    "Propeller": {
        "apc_propellers_15x8E": {"DIAMETER": 381.0, "PITCH": 203.2, "WEIGHT": 0.06},
        "apc_propellers_9x6E": {"DIAMETER": 228.6, "PITCH": 152.4, "WEIGHT": 0.02},
    },
    "Wing": {},
}


def _low_level(motor: str, prop: str, battery: str, motor_count: int, wing: bool = False):
    components = [{"component_instance": "Fuselage_1", "component_type": "UAV_Fuselage"}]
    for index in range(motor_count):
        components.append(
            {
                "component_instance": f"Motor_{index}",
                "component_type": "Motor",
                "component_choice": motor,
            }
        )
        components.append(
            {
                "component_instance": f"Prop_{index}",
                "component_type": "Propeller",
                "component_choice": prop,
            }
        )
    components.append(
        {
            "component_instance": "Battery_1",
            "component_type": "Battery",
            "component_choice": battery,
        }
    )
    components.append(
        {"component_instance": "GPS_1", "component_type": "Sensor", "component_choice": "GPS_1"}
    )
    components.append(
        {"component_instance": "Cargo_1", "component_type": "Cargo", "component_choice": "Cargo"}
    )
    if wing:
        components.append(
            {
                "component_instance": "Wing_1",
                "component_type": "Wing",
                "component_choice": "NACA_0012",
            }
        )
    return {"components": components, "connections": [], "parameters": []}


def _tree(arm_length: float = 400.0):
    """A design tree that folds four arms into one via symmetry."""
    return {
        "name": "design_x",
        "hub": {
            "node_type": "ConnectedHub4_Sym",
            "mainSegment": {
                "node_type": "PropArm",
                "armLength": arm_length,
                "motor": {"motorType": "kde_direct_KDE5215XF_435"},
                "prop": {"propType": "apc_propellers_15x8E"},
            },
        },
        "fuselageWithComponents": {
            "fuselage": {"length": 170.0, "vertDiameter": 103.5, "horzDiameter": 357.9}
        },
    }


EVALUATED_OUTPUT = {
    "Mass": 6.77,
    "Max_Distance": 12000.0,
    "Hover_Time": 900.0,
    "Max_Speed": 22.0,
    "Interferences": 0,
}
FAILED_OUTPUT = {"Mass": 6.77, "Max_Distance": 0, "Hover_Time": 0, "Max_Speed": 0}


_UNSET = object()  # distinguishes "not passed" from an explicit None


def _parse(low_level, tree=None, output=_UNSET, design_id="design_1"):
    return parse_design(
        design_id=design_id,
        tree=tree if tree is not None else _tree(),
        low_level=low_level,
        output=EVALUATED_OUTPUT if output is _UNSET else output,
        corpus=CORPUS,
    )


class TestComponentResolution:
    def test_battery_resolved_from_corpus(self):
        record = _parse(
            _low_level(
                "kde_direct_KDE5215XF_435", "apc_propellers_15x8E", "TurnigyGraphene6000mAh6S75C", 4
            )
        )
        assert record["battery"]["capacity_mah"] == 6000
        assert record["battery"]["cells"] == 6  # 22.2V / 3.7V
        assert record["battery"]["weight_g"] == 1140
        assert record["battery"]["part"] == "TurnigyGraphene6000mAh6S75C"

    def test_motor_and_propeller_specs_resolved(self):
        record = _parse(
            _low_level(
                "kde_direct_KDE5215XF_435", "apc_propellers_15x8E", "TurnigyGraphene6000mAh6S75C", 4
            )
        )
        motor = record["propulsion"][0]
        assert motor["kv"] == 435.0
        assert motor["max_power_w"] == 1595.0
        assert motor["prop_diameter_mm"] == 381.0

    def test_parallel_packs_sum_capacity(self):
        low_level = _low_level(
            "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", 4
        )
        low_level["components"].append(
            {
                "component_instance": "Battery_2",
                "component_type": "Battery",
                "component_choice": "TurnigyGraphene1500mAh4S75C",
            }
        )
        record = _parse(low_level)
        assert record["battery"]["pack_count"] == 2
        assert record["battery"]["capacity_mah"] == 3000
        assert record["battery"]["weight_g"] == 404

    def test_unknown_component_does_not_crash(self):
        record = _parse(_low_level("no_such_motor", "no_such_prop", "no_such_battery", 4))
        assert record is not None
        assert record["propulsion"][0]["motor"] == "no_such_motor"
        # Falls back to schema defaults rather than inventing specs.
        assert record["battery"]["capacity_mah"] == 5000


class TestTopology:
    @pytest.mark.parametrize(
        "count,expected",
        [
            (2, "bicopter"),
            (3, "tricopter"),
            (4, "quadcopter"),
            (6, "hexacopter"),
            (8, "octocopter"),
        ],
    )
    def test_supported_motor_counts_map_to_topology(self, count, expected):
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", count
            )
        )
        assert record["topology"] == expected
        assert record["airframe_class"] == "multirotor"
        assert record["frame"]["motor_count"] == count

    @pytest.mark.parametrize("count", [5, 7, 10, 12, 16])
    def test_unsupported_counts_are_labelled_not_coerced(self, count):
        """Regression: silently defaulting these is what collapsed the corpus."""
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", count
            )
        )
        assert record["topology"] == f"multirotor_{count}_motor"
        assert record["airframe_class"] == "multirotor_unsupported"
        assert record["frame"]["motor_count"] == count

    def test_fixed_wing_is_classified_separately(self):
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050",
                "apc_propellers_9x6E",
                "TurnigyGraphene1500mAh4S75C",
                4,
                wing=True,
            )
        )
        assert record["airframe_class"] == "fixed_wing_hybrid"
        assert record["topology"] == "fixed_wing_4_motor"
        assert record["frame"]["has_wing"] is True

    def test_motor_count_comes_from_low_level_not_hub_name(self):
        """The tree says ConnectedHub4_Sym; the low-level list says 6 motors."""
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", 6
            ),
            tree=_tree(),
        )
        assert record["frame"]["motor_count"] == 6
        assert record["topology"] == "hexacopter"

    def test_design_without_motors_is_rejected(self):
        assert _parse({"components": [], "connections": []}) is None


class TestProvenance:
    def test_evaluated_flag_tracks_source_metrics(self):
        low_level = _low_level(
            "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", 4
        )
        assert _parse(low_level, output=EVALUATED_OUTPUT)["evaluated"] is True
        assert _parse(low_level, output=FAILED_OUTPUT)["evaluated"] is False
        assert _parse(low_level, output=None)["evaluated"] is False

    def test_performance_metrics_preserved(self):
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", 4
            )
        )
        assert record["performance_simulated"]["Max_Distance"] == 12000.0
        assert record["mass_kg"] == 6.77

    def test_arm_length_read_from_tree(self):
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", 4
            ),
            tree=_tree(arm_length=436.5),
        )
        assert record["frame"]["arm_length_mm"] == 436

    def test_design_id_preserved_for_traceability(self):
        record = _parse(
            _low_level(
                "t_motor_AS2820KV1050", "apc_propellers_9x6E", "TurnigyGraphene1500mAh4S75C", 4
            ),
            design_id="design_14953",
        )
        assert record["design_id"] == "design_14953"


class TestCorpusDiversityRegression:
    """The original parser produced 27,714 byte-identical records."""

    def test_distinct_inputs_produce_distinct_records(self):
        variants = [
            (
                "kde_direct_KDE5215XF_435",
                "apc_propellers_15x8E",
                "TurnigyGraphene6000mAh6S75C",
                4,
                400.0,
            ),
            (
                "t_motor_AS2820KV1050",
                "apc_propellers_9x6E",
                "TurnigyGraphene1500mAh4S75C",
                6,
                250.0,
            ),
            (
                "kde_direct_KDE5215XF_435",
                "apc_propellers_9x6E",
                "TurnigyGraphene1500mAh4S75C",
                8,
                310.0,
            ),
            (
                "t_motor_AS2820KV1050",
                "apc_propellers_15x8E",
                "TurnigyGraphene6000mAh6S75C",
                3,
                180.0,
            ),
        ]
        records = [
            _parse(_low_level(m, p, b, n), tree=_tree(arm), design_id=f"design_{i}")
            for i, (m, p, b, n, arm) in enumerate(variants)
        ]
        identities = {
            json.dumps(
                {k: v for k, v in r.items() if k not in {"performance_simulated", "design_id"}},
                sort_keys=True,
            )
            for r in records
        }
        assert len(identities) == len(variants), "distinct designs collapsed into one record"

    def test_no_placeholder_generic_parts(self):
        """The broken parser emitted 'generic_2206-2300KV' for every motor."""
        record = _parse(
            _low_level(
                "kde_direct_KDE5215XF_435", "apc_propellers_15x8E", "TurnigyGraphene6000mAh6S75C", 4
            )
        )
        for motor in record["propulsion"]:
            assert not motor["motor"].startswith("generic_")
            assert not motor["propeller"].startswith("generic_")


class TestCorpusLoading:
    def test_loads_plain_data(self):
        payload = pickle.dumps({"Battery": {"b1": {"CAPACITY": 1000.0}}})
        assert load_corpus(payload)["Battery"]["b1"]["CAPACITY"] == 1000.0

    def test_refuses_arbitrary_globals(self):
        """corpus_dic is third-party data — unpickling must not import code."""
        malicious = b"c__builtin__\neval\n(S'1+1'\ntR."
        with pytest.raises(pickle.UnpicklingError):
            load_corpus(malicious)

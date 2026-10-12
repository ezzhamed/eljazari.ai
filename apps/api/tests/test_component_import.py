"""A clean manufacturer-data import must give the documented default curves.

The public catalog carries no sourced declarations, so a fresh clone (and CI)
imports with nothing to keep. The importer then fell back to "highest voltage,
then part number" and declared the MN3508 KV380 with the 15*5 propeller instead
of the documented 13*4.4, so tests passed only on machines with an older import.
"""

from scripts import extract_propulsion_tests as extractor
from scripts.extract_propulsion_tests import DEFAULT_DECLARATIONS, default_test


def _test(propeller: str, voltage: float) -> dict:
    return {
        "motor_part_number": "tmotor_mn3508_380kv",
        "propeller_part_number": propeller,
        "test_voltage_v": voltage,
    }


MN3508_380 = [
    _test("tmotor_p13_44", 22.2),
    _test("tmotor_p15_5", 22.2),
    _test("tmotor_p14_48", 14.8),
]


def test_clean_import_uses_the_documented_default():
    chosen = default_test("tmotor_mn3508_380kv", MN3508_380, current={})
    assert (chosen["propeller_part_number"], chosen["test_voltage_v"]) == ("tmotor_p13_44", 22.2)


def test_an_existing_declaration_that_is_still_a_test_is_kept():
    current = {"propeller_part_number": "tmotor_p14_48", "test_voltage_v": 14.8}
    chosen = default_test("tmotor_mn3508_380kv", MN3508_380, current)
    assert chosen["propeller_part_number"] == "tmotor_p14_48"


def test_without_a_documented_default_the_highest_voltage_test_is_used():
    chosen = default_test("unknown_motor", MN3508_380, current={})
    assert chosen["test_voltage_v"] == 22.2


def test_every_documented_default_names_known_parts():
    assert set(DEFAULT_DECLARATIONS) == set(extractor.MOTORS.values())
    for propeller, voltage in DEFAULT_DECLARATIONS.values():
        assert propeller in extractor.PROPELLERS.values()
        assert voltage > 0

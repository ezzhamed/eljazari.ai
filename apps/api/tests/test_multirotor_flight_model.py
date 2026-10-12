"""Hexa-X and Octo-X: structure, rotor allocation and PX4 parameters.

Built from documented propulsion only (data/components/propulsion_tests.json):
the same pipeline path as the Quad-X, with six or eight rotors.
"""

from __future__ import annotations

import math

from types import SimpleNamespace

import pytest

from agents.component_models import SelectedComponents
from agents.component_selector_agent import ComponentSelectorAgent
from agents.design_models import BatterySpec, DroneDesignTree, FrameSpec, PayloadSpec
from pipelines.flight_model import (
    _normalized_mix,
    attest_spawned_model,
    derive_flight_model,
    parse_gz_model_links,
    verify_flight_bundle,
    write_flight_model,
)
from pipelines.resolved_drone import motor_positions, resolve_drone
from pipelines.simulation_acceptance import assess_simulation
from services.component_catalog import get_catalog
from tests.test_simulation_acceptance import good_state

AIRFRAMES = {
    # topology: (motor, propeller, battery, frame, payload_g)
    "hexacopter": (
        "tmotor_mn3508_380kv",
        "tmotor_p13_44",
        "tattu_plus_10000_6s_25c",
        "tarot_680_pro_hex",
        1000.0,
    ),
    "octocopter": (
        "tmotor_mn4014_400kv",
        "tmotor_p15_5",
        "tattu_plus_10000_6s_25c",
        "tarot_x8_tl8x000",
        2000.0,
    ),
}
COUNT = {"hexacopter": 6, "octocopter": 8}


def _design(topology: str, payload_g: float) -> DroneDesignTree:
    design = DroneDesignTree(
        topology=topology,
        frame=FrameSpec(type="X", arm_length_mm=400.0),
        propulsion=[],
        battery=BatterySpec(type="LiPo", cells=6, capacity_mah=10000, weight_g=1300.0),
        payload=PayloadSpec(max_weight_g=payload_g, mount_type="bottom"),
    )
    design.payload.installed_weight_g = payload_g
    return design


def _parts(topology: str) -> SelectedComponents:
    catalog = get_catalog()
    motor, prop, battery, frame, _ = AIRFRAMES[topology]
    esc = next(e for e in catalog.escs if e.channels == 1)
    return catalog.with_propulsion_test(
        SelectedComponents(
            motor=catalog.motor(motor),
            propeller=catalog.propeller(prop),
            battery=catalog.battery(battery),
            esc=esc,
            flight_controller=catalog.flight_controller("holybro_durandal_h7"),
            frame=catalog.frame(frame),
            motor_count=COUNT[topology],
        )
    )


def spec(topology: str):
    return resolve_drone(_design(topology, AIRFRAMES[topology][4]), _parts(topology))


def test_quad_positions_keep_their_historical_arithmetic():
    arm = 0.25
    assert motor_positions(4, arm) == [
        (sx * arm / math.sqrt(2), sy * arm / math.sqrt(2))
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1))
    ]


@pytest.mark.parametrize("count", [6, 8])
def test_larger_frames_are_regular_and_start_off_axis(count):
    positions = motor_positions(count, 0.5)
    angles = [math.degrees(math.atan2(y, x)) % 360 for x, y in positions]
    assert angles == pytest.approx([180 / count + 360 * k / count for k in range(count)])
    assert all(math.hypot(x, y) == pytest.approx(0.5) for x, y in positions)


@pytest.mark.parametrize("topology", ["hexacopter", "octocopter"])
def test_structure_has_one_arm_motor_and_propeller_per_rotor(topology):
    resolved = spec(topology)
    names = [link.name for link in resolved.links]
    for kind in ("arm", "motor", "prop", "esc"):
        assert len([n for n in names if n.startswith(f"{kind}_")]) == COUNT[topology]
    assert resolved.topology == topology
    expected = resolved.components.total_bom_weight_g + AIRFRAMES[topology][4]
    assert sum(link.mass_kg for link in resolved.links) * 1000 == pytest.approx(expected)


def test_a_frame_with_another_arm_count_or_overlapping_discs_is_refused():
    design = _design("hexacopter", 0)
    quad_frame = _parts("hexacopter").model_copy(
        update={"frame": get_catalog().frame("holybro_x500_v2")}
    )
    with pytest.raises(ValueError, match="flat X multirotors"):
        resolve_drone(design, quad_frame)
    big = _parts("hexacopter").model_copy(
        update={"propeller": get_catalog().propeller("tmotor_p14_48")}
    )
    with pytest.raises(ValueError, match="overlap"):
        resolve_drone(design, big)


@pytest.mark.parametrize("topology", ["hexacopter", "octocopter"])
def test_flight_model_allocates_every_rotor_and_balances_yaw(topology):
    count = COUNT[topology]
    model = derive_flight_model(spec(topology))
    assert model.model_name.startswith({6: "hexafm_", 8: "octofm_"}[count])
    assert [r.px4_index for r in model.rotors] == list(range(count))
    # Neighbours counter-rotate, so the yaw moments cancel at equal thrust.
    assert [r.turning_direction for r in model.rotors] == ["ccw", "cw"] * (count // 2)
    params = model.px4_params
    assert params["CA_ROTOR_COUNT"] == count
    assert sum(params[f"CA_ROTOR{i}_KM"] for i in range(count)) == pytest.approx(0, abs=1e-9)
    for i in range(4, count):
        assert params[f"SIM_GZ_EC_FUNC{i + 1}"] == 101 + i
    assert "SIM_GZ_EC_FUNC1" not in params  # the x500 airframe already maps 1-4
    assert model.hover_thrust_fraction == pytest.approx(
        model.total_mass_kg * 9.80665 / count / model.motor.max_thrust_n
    )
    assert model.thrust_to_weight > 2


def test_pseudo_inverse_allocation_produces_pure_axis_torques():
    positions = motor_positions(6, 0.35)
    rotors = [(x, -y, 0.05 * (1 if i % 2 == 0 else -1)) for i, (x, y) in enumerate(positions)]
    mix = _normalized_mix(rotors)
    for axis in range(3):
        torque = [
            sum(mix[i][axis] * lever for i, lever in enumerate(levers))
            for levers in (
                [-py for _, py, _ in rotors],
                [px for px, _, _ in rotors],
                [km for _, _, km in rotors],
            )
        ]
        assert torque[axis] > 0
        assert all(abs(t) < 1e-9 for j, t in enumerate(torque) if j != axis)


@pytest.mark.parametrize("topology", ["hexacopter", "octocopter"])
def test_bundle_attestation_expects_every_rotor(tmp_path, topology):
    count = COUNT[topology]
    resolved = spec(topology)
    manifest = write_flight_model(resolved, tmp_path)
    bundle = verify_flight_bundle(manifest, resolved.sha256)
    assert bundle.motor_count == count
    instance = f"{bundle.model_name}_0"
    lines = []
    for name in ["base_link", *(f"rotor_{i}" for i in range(count))]:
        mass = bundle.total_mass_kg - 0.01 * count if name == "base_link" else 0.01
        lines += [f"  - Name: {name}", f"  - Parent: {instance}", f"  - Mass (kg): {mass}"]
    for sensor in ("air_pressure_sensor", "magnetometer_sensor", "imu_sensor", "navsat_sensor"):
        lines.append(f"    - Name: {sensor}")
    report = parse_gz_model_links("\n".join(lines))
    attestation = attest_spawned_model(bundle, instance, report)
    assert attestation["rotor_link_count"] == count
    missing = parse_gz_model_links("\n".join(line for line in lines if "rotor_5" not in line))
    with pytest.raises(ValueError, match="not the generated"):
        attest_spawned_model(bundle, instance, missing)


def test_acceptance_counts_the_rotors_of_the_resolved_design():
    resolved = spec("hexacopter").model_dump(mode="json")
    attestation = {
        "status": "matched",
        "resolved_spec_sha256": "s" * 64,
        "rotor_link_count": 6,
        "px4_params_checked": 40,
    }
    state = good_state(
        resolved_drone_sha256="s" * 64,
        resolved_drone_spec=resolved,
        simulation_model_attestation=attestation,
    )
    assert "Generated aircraft identity" not in " ".join(assess_simulation(state)["reasons"])
    attestation["rotor_link_count"] = 4
    assert "Generated aircraft identity" in " ".join(assess_simulation(state)["reasons"])


@pytest.mark.parametrize("topology", ["hexacopter", "octocopter"])
def test_selection_picks_a_matching_frame_with_clear_discs_and_sourced_data(topology):
    design = _design(topology, AIRFRAMES[topology][4])
    result = ComponentSelectorAgent().select(design, payload_g=AIRFRAMES[topology][4])
    parts = result.chosen.components
    assert parts.frame.arm_count == parts.motor_count == COUNT[topology]
    clearance = 2 * parts.frame.arm_length_mm / 1000 * math.sin(math.pi / parts.motor_count)
    assert parts.propeller.diameter_inch * 0.0254 < clearance
    assert result.chosen.performance.propulsion_evidence.status == "matched_declaration"
    derive_flight_model(resolve_drone(design, parts))  # the selection flies


def test_each_run_gets_its_own_gazebo_partition_and_cleanup_waits():
    """A leftover Gazebo server once received a new run's spawn (docs: batch 28)."""
    from services import gazebo_service as g

    script = g._SITL_CLEANUP_SCRIPT
    assert "pkill -9" in script and "exit 1" in script and "sleep 0.2" in script
    # [x] patterns keep pgrep/pkill from matching the cleanup command itself.
    assert all(p.startswith("[") for p in g._SITL_PATTERNS)
    sim = g.SimProcess(sim_id="abc12345", mavlink_port=14550, sitl_port=14540)
    assert g.GazeboService._gz_partition(sim) == "eljazari_abc12345"


@pytest.mark.asyncio
async def test_fault_injection_runs_only_allowed_px4_commands_over_stdin(monkeypatch):
    """scripts/check_sitl_faults.py reaches PX4 only through these two hooks."""
    from unittest.mock import AsyncMock, MagicMock

    from services import gazebo_service as g

    service = g.GazeboService.__new__(g.GazeboService)
    service._runtime = SimpleNamespace(
        mode="docker", px4_dir="/opt/px4/PX4-Autopilot", wsl_prefix=lambda: ["wsl.exe"]
    )
    sim = g.SimProcess(sim_id="abc12345", mavlink_port=14550, sitl_port=14540)
    sim.container = "eljazari-sim-abc12345"
    service._sims = {"abc12345": sim}
    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"ok", None))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(ValueError, match="not allowed"):
        await service.px4_command("abc12345", "shutdown")
    with pytest.raises(KeyError):
        await service.px4_command("other", "param", "show")

    assert await service.px4_command("abc12345", "failure", "motor", "off", "-i", "1") == (0, "ok")
    assert spawn.await_args.args == ("docker", "exec", "-i", "eljazari-sim-abc12345", "bash", "-s")
    script = proc.communicate.await_args.args[0].decode()
    assert script == "/opt/px4/PX4-Autopilot/build/px4_sitl_default/bin/px4-failure motor off -i 1"
    await service.kill_px4("abc12345")
    # Matches the daemon (arguments follow), not px4-<command> clients.
    assert proc.communicate.await_args.args[0] == b"pkill -9 -f '[b]in/px4 '"

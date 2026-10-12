"""Generated PX4/Gazebo flight bundles: physics, identity and fail-closed gates."""

import hashlib
import json
import math
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET

import pytest

from graph.nodes import _code_sha256

from pipelines.flight_model import (
    COMMAND_MAX,
    GRAVITY,
    VerifiedFlightBundle,
    attest_spawned_model,
    compare_px4_params,
    derive_flight_model,
    parse_gz_model_links,
    parse_px4_param_show,
    verify_flight_bundle,
    write_flight_model,
)
from pipelines.resolved_drone import resolve_drone
from pipelines.simulation_acceptance import assess_simulation
from pipelines.simulation_readiness import generated_model_block_reason
from services.component_catalog import get_catalog
from tests.flight_fixtures import flight
from tests.test_resolved_drone import inputs

FIXTURE = Path(__file__).parent / "fixtures" / "gz_model_quadfm_links.txt"


# A documented combination: T-MOTOR publishes this motor with this propeller at
# 14.8 V (data/components/propulsion_tests.json). SITL flies nothing else.
SOURCED = {
    "motor": "tmotor_mn3110_700kv",
    "propeller": "tmotor_p12_4",
    "battery": "tattu_1500_4s_75c",
    "frame": "holybro_x500_v2",
}


def sourced_inputs(payload=125):
    design, parts = inputs(payload)
    catalog = get_catalog()
    design.frame.arm_length_mm = 250.0
    parts = parts.model_copy(update={k: getattr(catalog, k)(v) for k, v in SOURCED.items()})
    return design, catalog.with_propulsion_test(parts)


def spec(payload=125):
    return resolve_drone(*sourced_inputs(payload))


def bundle(tmp_path, payload=125):
    resolved = spec(payload)
    manifest = write_flight_model(resolved, tmp_path)
    return resolved, manifest, verify_flight_bundle(manifest, resolved.sha256)


def test_mass_inertia_and_rotor_layout_follow_the_resolved_spec():
    resolved = spec()
    model = derive_flight_model(resolved)
    assert model.total_mass_kg * 1000 == pytest.approx(resolved.total_mass_g, abs=1e-6)
    assert model.body_mass_kg + 4 * model.rotor_mass_kg == pytest.approx(model.total_mass_kg)
    fixed = [link for link in resolved.links if not link.name.startswith("prop_")]
    com_z = sum(link.mass_kg * link.pose[2] for link in fixed) / model.body_mass_kg
    assert model.body_com_m[2] == pytest.approx(com_z)
    ixx, iyy, izz, ixy, ixz, iyz = model.body_inertia
    assert min(ixx, iyy, izz) > 0
    # Physical inertia tensors satisfy the triangle inequality.
    assert ixx + iyy >= izz and iyy + izz >= ixx and ixx + izz >= iyy
    assert abs(ixy) < 1e-12 and abs(ixz) < 1e-12 and abs(iyz) < 1e-12
    # PX4 Quad-X order: front-right CCW, rear-left CCW, front-left CW, rear-right CW.
    layout = [
        (
            math.copysign(1, r.position_flu_m[0]),
            math.copysign(1, r.position_flu_m[1]),
            r.turning_direction,
        )
        for r in model.rotors
    ]
    assert layout == [(1, -1, "ccw"), (-1, 1, "ccw"), (1, 1, "cw"), (-1, -1, "cw")]
    params = model.px4_params
    for rotor in model.rotors:
        i = rotor.px4_index
        x, y, _ = rotor.position_flu_m
        # PX4 parameters are FRD: PY is the negated Gazebo (left-positive) y.
        assert params[f"CA_ROTOR{i}_PX"] == pytest.approx(x, abs=1e-6)
        assert params[f"CA_ROTOR{i}_PY"] == pytest.approx(-y, abs=1e-6)
        assert (params[f"CA_ROTOR{i}_KM"] > 0) == (rotor.turning_direction == "ccw")


def test_thrust_fit_and_hover_estimate_come_from_the_catalog_curve():
    resolved = spec()
    model = derive_flight_model(resolved)
    curve = resolved.components.motor.thrust_curve
    top = max(curve, key=lambda p: p.rpm)
    omega = top.rpm * 2 * math.pi / 60
    assert model.motor.max_rpm == top.rpm
    assert model.motor.max_thrust_n == pytest.approx(
        model.motor.thrust_coefficient_n_per_rad2_s2 * omega**2
    )
    # Command units: the plugin's thrust at command 1000 equals the fitted maximum.
    assert model.motor_constant_command * COMMAND_MAX**2 == pytest.approx(model.motor.max_thrust_n)
    assert model.hover_thrust_fraction == pytest.approx(
        model.total_mass_kg * GRAVITY / 4 / model.motor.max_thrust_n
    )
    assert model.thrust_to_weight == pytest.approx(1 / model.hover_thrust_fraction)
    assert 0.1 <= model.px4_params["MPC_THR_HOVER"] <= 0.8
    assert all(
        0.01 <= model.px4_params[k] <= 5
        for k in ("MC_ROLLRATE_K", "MC_PITCHRATE_K", "MC_YAWRATE_K")
    )
    # The low end of the curve is not quadratic; this must be surfaced, not hidden.
    assert any("extrapolated" in w for w in model.warnings)
    assert model.motor.max_relative_fit_error > 0.1


def test_heavier_payload_changes_identity_mass_and_hover_thrust(tmp_path):
    light, heavy = derive_flight_model(spec(0)), derive_flight_model(spec(400))
    assert heavy.resolved_spec_sha256 != light.resolved_spec_sha256
    assert heavy.model_name != light.model_name
    assert heavy.total_mass_kg - light.total_mass_kg == pytest.approx(0.4)
    assert heavy.hover_thrust_fraction > light.hover_thrust_fraction
    assert heavy.px4_params["MPC_THR_HOVER"] > light.px4_params["MPC_THR_HOVER"]
    # The gain scale holds PX4's loop gain at the x500's, so it follows inertia over
    # the thrust slope at hover (proportional to sqrt(hover fraction)). A payload
    # raises both; which wins depends on the frame, so the relation is the invariant.
    expected = (heavy.body_inertia[0] / light.body_inertia[0]) * math.sqrt(
        light.hover_thrust_fraction / heavy.hover_thrust_fraction
    )
    ratio = heavy.rate_gain_scale["roll"] / light.rate_gain_scale["roll"]
    assert ratio == pytest.approx(expected, rel=0.02)  # rotor inertia is the remainder


def test_curve_at_another_voltage_is_refused():
    design, parts = sourced_inputs(0)
    parts.battery = get_catalog().battery("cnhl_blackseries_1500_6s_100c")
    with pytest.raises(ValueError, match="battery nominal voltage"):
        derive_flight_model(resolve_drone(design, parts))


def test_an_unsourced_combination_never_becomes_a_flight_model():
    design, parts = inputs(0)  # F80 + HQ 5-inch: no published test of the pairing
    with pytest.raises(ValueError, match="flies documented combinations only"):
        derive_flight_model(resolve_drone(design, parts))


def test_bundle_records_its_propulsion_source(tmp_path):
    _, manifest_path, _ = bundle(tmp_path)
    source = json.loads(manifest_path.read_text())["propulsion_source"]
    assert "MN3110 KV700 / 14.8 V / T-MOTOR" in source["reference"]
    assert len(source["source_sha256"]) == len(source["curve_sha256"]) == 64


def test_unhoverable_mass_is_refused():
    with pytest.raises(ValueError, match="cannot hover"):
        derive_flight_model(spec(8000))


def test_bundle_files_hashes_and_sdf_contents(tmp_path):
    resolved, manifest_path, verified = bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["validation_scope"] == "sitl_catalog_estimate"
    assert manifest["flight_ready"] is False
    assert manifest_path.parent == tmp_path / resolved.sha256 / "flight"
    for name, digest in manifest["files"].items():
        data = (manifest_path.parent / name).read_bytes()
        assert b"\r\n" not in data
        assert hashlib.sha256(data).hexdigest() == digest
    root = ET.parse(verified.sdf_path).getroot().find("model")
    assert root.get("name") == verified.model_name
    masses = [float(e.text) for e in root.findall("link/inertial/mass")]
    assert sum(masses) == pytest.approx(verified.total_mass_kg, abs=1e-9)
    sensors = {s.get("name") for s in root.findall("link[@name='base_link']/sensor")}
    assert sensors == {"air_pressure_sensor", "magnetometer_sensor", "imu_sensor", "navsat_sensor"}
    plugins = root.findall("plugin[@name='gz::sim::systems::MulticopterMotorModel']")
    assert [p.findtext("motorNumber") for p in plugins] == ["0", "1", "2", "3"]
    assert [p.findtext("turningDirection") for p in plugins] == ["ccw", "ccw", "cw", "cw"]
    assert {p.findtext("maxRotVelocity") for p in plugins} == {"1000.0"}
    assert float(root.findtext("pose").split()[2]) > 0
    # Regeneration is deterministic.
    again = write_flight_model(resolved, tmp_path)
    assert json.loads(again.read_text())["bundle_sha256"] == verified.bundle_sha256


def test_tampering_or_a_foreign_spec_fails_verification(tmp_path):
    resolved, manifest_path, _ = bundle(tmp_path)
    with pytest.raises(ValueError, match="different resolved specification"):
        verify_flight_bundle(manifest_path, spec(0).sha256)
    sdf = manifest_path.parent / "model.sdf"
    sdf.write_text(sdf.read_text().replace("<mass>", "<mass>1", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="model.sdf changed"):
        verify_flight_bundle(manifest_path, resolved.sha256)
    resolved, manifest_path, _ = bundle(tmp_path / "b")
    manifest = json.loads(manifest_path.read_text())
    manifest["px4_params"]["MC_ROLLRATE_K"] = 1.0
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="fingerprint"):
        verify_flight_bundle(manifest_path, resolved.sha256)


def _fixture_bundle(total=0.6263):
    return VerifiedFlightBundle(
        manifest_path="m",
        bundle_dir="d",
        sdf_path="s",
        model_name="quadfm_77aa9fc9ee789aa5",
        resolved_spec_sha256="a" * 64,
        bundle_sha256="b" * 64,
        total_mass_kg=total,
        motor_count=4,
        px4_base_autostart=4001,
        px4_params={},
    )


def test_gazebo_report_parsing_and_attestation():
    report = parse_gz_model_links(FIXTURE.read_text())
    assert report.links["base_link"] == pytest.approx(0.6095)
    assert {k for k in report.links if k.startswith("rotor_")} == {f"rotor_{i}" for i in range(4)}
    assert set(report.link_parents.values()) == {"quadfm_77aa9fc9ee789aa5_0"}
    assert {"imu_sensor", "navsat_sensor"} <= set(report.sensors)
    attestation = attest_spawned_model(_fixture_bundle(), "quadfm_77aa9fc9ee789aa5_0", report)
    assert attestation["status"] == "matched"
    assert attestation["rotor_link_count"] == 4
    assert attestation["simulated_total_mass_kg"] == pytest.approx(0.6263)
    with pytest.raises(ValueError, match="mass"):
        attest_spawned_model(_fixture_bundle(0.7), "quadfm_77aa9fc9ee789aa5_0", report)
    with pytest.raises(ValueError, match="instance"):
        attest_spawned_model(_fixture_bundle(), "x500_0", report)
    with pytest.raises(ValueError, match="not the generated"):
        attest_spawned_model(_fixture_bundle(), "x", parse_gz_model_links("No model named <x>"))
    no_imu = FIXTURE.read_text().replace("- Name: imu_sensor", "- Name: other_sensor")
    with pytest.raises(ValueError, match="imu_sensor"):
        attest_spawned_model(
            _fixture_bundle(), "quadfm_77aa9fc9ee789aa5_0", parse_gz_model_links(no_imu)
        )


def test_px4_param_readback_parsing():
    text = "x   MC_ROLLRATE_K [546,1031] : 0.1658\nx + SYS_AUTOSTART [1019,1643] : 4001\n"
    observed = parse_px4_param_show(text)
    assert observed == {"MC_ROLLRATE_K": 0.1658, "SYS_AUTOSTART": 4001.0}
    assert compare_px4_params({"MC_ROLLRATE_K": 0.16583, "SYS_AUTOSTART": 4001}, observed) == []
    assert compare_px4_params({"MC_ROLLRATE_K": 1.0}, observed)
    assert compare_px4_params({"CA_ROTOR0_PX": 0.08}, observed) == ["CA_ROTOR0_PX: not reported"]


def test_readiness_gate_accepts_only_a_verified_bundle(tmp_path):
    resolved, manifest_path, _ = bundle(tmp_path)
    config = {"urdf_path": "m.urdf", "resolved_drone_sha256": resolved.sha256}
    reason = generated_model_block_reason({**config, "flight_model_block_reason": "curve voltage"})
    assert "no verified flight bundle" in reason and "curve voltage" in reason
    assert (
        generated_model_block_reason({**config, "flight_model_manifest_path": str(manifest_path)})
        is None
    )
    foreign = {
        **config,
        "resolved_drone_sha256": "0" * 64,
        "flight_model_manifest_path": str(manifest_path),
    }
    assert "failed verification" in generated_model_block_reason(foreign)
    (manifest_path.parent / "model.sdf").write_text("<sdf/>", encoding="utf-8")
    assert "failed verification" in generated_model_block_reason(
        {**config, "flight_model_manifest_path": str(manifest_path)}
    )


def _flown(**extra):
    state = {
        "simulation_status": "complete",
        "runtime_mode": "wsl",
        "telemetry_source": "real_wsl_sitl",
        "mission_isolation": "linux_namespaces",
        "mission_started": True,
        "mission_completed": True,
        "mission_error": "",
        "crash_detected": False,
        "geofence_violation": False,
        **flight(),
    }
    state.update(extra)
    return state


def test_acceptance_requires_attestation_of_the_exact_bundle():
    reference = assess_simulation(_flown())
    assert reference["passed"] and reference["aircraft"] == "reference_x500"
    identity = {"resolved_drone_sha256": "a" * 64, "flight_model_bundle_sha256": "b" * 64}
    missing = assess_simulation(_flown(**identity))
    assert not missing["passed"]
    assert missing["aircraft"] == "generated" and missing["generated_identity_attested"] is False
    assert missing["retryable_with_code_change"] is False
    attestation = {
        "status": "matched",
        "resolved_spec_sha256": "a" * 64,
        "bundle_sha256": "b" * 64,
        "rotor_link_count": 4,
        "px4_params_checked": 30,
    }
    ok = assess_simulation(_flown(**identity, simulation_model_attestation=attestation))
    assert ok["passed"] and ok["generated_identity_attested"]
    assert ok["model_validation_scope"] == "sitl_catalog_estimate"
    assert ok["generated_aircraft_validated"] is False
    stale = {**attestation, "bundle_sha256": "c" * 64}
    assert not assess_simulation(_flown(**identity, simulation_model_attestation=stale))["passed"]


@pytest.mark.asyncio
async def test_generated_bundle_is_refused_by_runtimes_that_cannot_attest(tmp_path, monkeypatch):
    from services.gazebo_service import GazeboService
    from utils.sitl_runtime import SITLRuntime

    resolved, manifest_path, _ = bundle(tmp_path)
    service = GazeboService()
    service._runtime = SITLRuntime(mode="mock", host_platform="Windows", is_real=False)
    config = {
        "resolved_drone_sha256": resolved.sha256,
        "flight_model_manifest_path": str(manifest_path),
    }
    with pytest.raises(ValueError, match="real PX4/Gazebo runtime"):
        await service.launch_world(config)
    assert service.active_sim_count() == 0


@pytest.mark.asyncio
async def test_spawn_attests_and_failures_tear_the_run_down(tmp_path):
    from services.gazebo_service import GazeboService, SimProcess
    from utils.sitl_runtime import SITLRuntime

    service = GazeboService()
    service._runtime = SITLRuntime(
        mode="native", host_platform="Linux", is_real=True, px4_dir="/px4", worlds_path="/w"
    )
    sim = SimProcess(sim_id="s1", flight_bundle=_fixture_bundle())
    outputs = iter(
        [
            "Service providers [Address, Request Message, Response Message]:",
            "data: true",
            FIXTURE.read_text(),
        ]
    )
    with patch.object(
        service, "_gz_command", new=AsyncMock(side_effect=lambda *a, **k: (0, next(outputs)))
    ):
        await service._spawn_generated_model(sim)
    assert sim.model_instance == "quadfm_77aa9fc9ee789aa5_0"
    assert sim.model_attestation["status"] == "matched"
    env = service._generated_px4_env(
        SimProcess(
            sim_id="s2",
            flight_bundle=_fixture_bundle().model_copy(
                update={"px4_params": {"MC_ROLLRATE_K": 0.17}}
            ),
            model_instance="m_0",
        )
    )
    assert env == {
        "PX4_SYS_AUTOSTART": "4001",
        "PX4_GZ_MODEL_NAME": "m_0",
        "PX4_PARAM_MC_ROLLRATE_K": "0.17",
    }

    refused = SimProcess(sim_id="s3", flight_bundle=_fixture_bundle())
    replies = iter(["Service providers", "data: false"])
    with patch.object(
        service, "_gz_command", new=AsyncMock(side_effect=lambda *a, **k: (0, next(replies)))
    ):
        with pytest.raises(RuntimeError, match="refused"):
            await service._spawn_generated_model(refused)

    resolved, manifest_path, _ = bundle(tmp_path)
    config = {
        "resolved_drone_sha256": resolved.sha256,
        "flight_model_manifest_path": str(manifest_path),
    }
    with (
        patch.object(service, "_start_gazebo", new=AsyncMock()),
        patch.object(
            service, "_spawn_generated_model", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        patch.object(service, "_start_px4_sitl", new=AsyncMock()) as px4,
        patch.object(service, "stop_world", new=AsyncMock()) as stop,
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await service.launch_world(config)
    px4.assert_not_awaited()
    stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversion_emits_a_verified_flight_bundle(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import convert_to_urdf

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    design, parts = sourced_inputs(125)
    state = {
        "project_id": "flight",
        "bom_status": "selected",
        "design_tree": design.model_dump(),
        "selected_components": parts.model_dump(),
    }
    result = await convert_to_urdf(state)
    assert result["conversion_status"] == "complete"
    assert result["flight_model_status"] == "generated"
    verified = verify_flight_bundle(
        result["flight_model_manifest_path"], result["resolved_drone_sha256"]
    )
    assert verified.bundle_sha256 == result["flight_model_bundle_sha256"]
    assert (
        Path(result["flight_model_manifest_path"]).parent.parent
        == Path(result["model_manifest_path"]).parent
    )
    assert generated_model_block_reason({**state, **result}) is None


@pytest.mark.asyncio
async def test_unsupported_propulsion_keeps_structure_but_blocks_flight(tmp_path, monkeypatch):
    from config.settings import settings
    from graph.nodes import convert_to_urdf

    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    design, parts = sourced_inputs(0)
    parts.battery = get_catalog().battery("cnhl_blackseries_1500_6s_100c")
    state = {
        "project_id": "flight",
        "bom_status": "selected",
        "design_tree": design.model_dump(),
        "selected_components": parts.model_dump(),
    }
    result = await convert_to_urdf(state)
    assert result["conversion_status"] == "complete"
    assert result["model_validation_scope"] == "structural_only"
    assert result["flight_model_status"] == "failed"
    assert result["flight_model_manifest_path"] is None
    reason = generated_model_block_reason({**state, **result})
    assert "no verified flight bundle" in reason and "battery nominal voltage" in reason


@pytest.mark.asyncio
async def test_run_simulation_passes_identity_and_keeps_the_attestation():
    from graph.nodes import run_simulation

    attestation = {"status": "matched", "bundle_sha256": "b" * 64}
    state = {
        "project_id": "test",
        "generated_code": "code",
        "approved_code_sha256": _code_sha256("code"),
        "resolved_drone_sha256": "a" * 64,
        "flight_model_manifest_path": "/bundle/flight_manifest.json",
        "urdf_path": "/m.urdf",
        "selected_components": {"frame": "x"},
    }
    with (
        patch("pipelines.simulation_readiness.generated_model_block_reason", return_value=None),
        patch(
            "agents.test_agent.SimulationTestAgent.run_single",
            new=AsyncMock(return_value={"telemetry": [], "model_attestation": attestation}),
        ) as run,
        patch("graph.nodes._persist_simulation_record", new=AsyncMock(return_value=None)) as save,
    ):
        result = await run_simulation(state)
    config = run.await_args.kwargs["drone_config"]
    assert config["flight_model_manifest_path"] == "/bundle/flight_manifest.json"
    assert config["resolved_drone_sha256"] == "a" * 64
    assert "selected_components" not in config
    assert result["simulation_model_attestation"] == attestation
    assert save.await_args.kwargs["model_attestation"] == attestation

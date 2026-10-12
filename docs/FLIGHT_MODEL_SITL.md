# Generated-aircraft SITL flight model

A design with a selected BOM now produces a PX4/Gazebo flight bundle, and SITL flies **that** aircraft instead of the reference x500. The simulator reports which model it loaded, and the acceptance gate requires that report. Scope is `sitl_catalog_estimate`: parameters come from unverified catalog curves and declared assumptions. This is simulation evidence, not calibration or airworthiness.

## What is generated

`pipelines/flight_model.py` derives the model from `ResolvedDroneSpec` and writes `<storage>/<project>/urdf/<spec sha256>/flight/`:

| File | Content |
| --- | --- |
| `model.sdf` | One rigid `base_link` (every non-rotating structural link merged: composite mass, centre of mass and inertia by the parallel-axis theorem), one `rotor_N` link per motor (4, 6 or 8) on revolute joints, PX4's IMU/baro/magnetometer/GNSS sensor set and one `MulticopterMotorModel` plugin per rotor |
| `model.config` | Gazebo model metadata |
| `flight_model.json` | Every derived quantity, warnings and assumptions |
| `flight_manifest.json` | Scope, spec fingerprint, model name, total mass, PX4 base airframe and parameters, SHA-256 of each file, and a fingerprint over the manifest itself |

Derivation:

- **Thrust:** `T = k ω²` least-squares fitted to the catalog RPM/thrust points. The curve must be recorded at the selected battery's nominal voltage (±0.05 V), or no model is produced. PX4 caps `SIM_GZ_EC_MAX` at 1000, so the plugin uses command units: 1000 equals the catalog maximum RPM, and `motorConstant = T_max / 1000²`.
- **Yaw moment ratio:** mean of `0.8 · P_electrical / (ω · T)` over the curve points (0.8 is an assumed motor efficiency).
- **Airframes:** flat X quadcopters, hexacopters and octocopters on a frame with that many arms. Motors sit at the centre-to-motor distance, `wheelbase / 2`. Tricopters, bicopters and fixed wings need tilting rotors or control surfaces; they are refused, not approximated.
- **Rotor order:**
  - Quad-X keeps PX4's order: 0 front-right CCW, 1 rear-left CCW, 2 front-left CW, 3 rear-right CW.
  - Hexa-X and Octo-X number the arms round the circle from 180°/N (30° and 22.5°), alternating CCW and CW, so every neighbouring pair counter-rotates.
  - `CA_ROTOR_COUNT` and `CA_ROTORn_PX/PY/KM` give PX4 the same positions in FRD. `SIM_GZ_EC_FUNC5…8` map the extra outputs, because the x500 airframe maps only 1–4.
- **Allocation:** the rate-gain scaling mirrors PX4's allocation, which is a pseudo-inverse for more than four rotors.
- **Propeller clearance:** adjacent rotors are `2·r·sin(π/N)` apart. Selection and the structural model refuse propeller discs that would overlap, whatever the frame's stated maximum.
- **Hover:** hover thrust fraction, and `MPC_THR_HOVER` from the linear idle-to-maximum command mapping (clamped to PX4 bounds, with a warning).
- **Rate-loop gains:** PX4 default gains stay; `MC_ROLLRATE_K/PITCHRATE_K/YAWRATE_K` scale them so the loop gain at hover equals the x500 reference. This mirrors PX4 v1.17 pseudo-inverse allocation and its roll/pitch/yaw normalisation. They are not tuned for the airframe.
- **Refusals:** no bundle is produced for a design that cannot hover, a motor/propeller/voltage without a published test (see [propulsion evidence](PROPULSION_EVIDENCE.md)), or an unsupported layout. `convert_to_urdf` still completes the structural model; `flight_model_status=failed` and the reason blocks flight.

## How a run is bound to the bundle

1. **Readiness gate** (`pipelines/simulation_readiness.py`): a generated design may fly only if `flight_model_manifest_path` re-hashes against its manifest and names the current `resolved_drone_sha256`. Structural artifacts, or a manifest that only claims readiness, stay blocked. Reference x500 runs are unchanged.
2. **Launch** (`services/gazebo_service.py`, native, WSL and Docker runtimes): the bundle is verified again, then Gazebo spawns its SDF. `gz model -m <instance> -l` must report exactly `base_link` plus one rotor per motor, all owned by that instance, with the four sensors, and a total mass within 0.1 g of the manifest. PX4 starts in a fresh working directory with airframe 4001, `PX4_GZ_MODEL_NAME` and one `PX4_PARAM_*` per design parameter. Every parameter, plus `SYS_AUTOSTART`, is read back with `px4-param show` before any mission starts. Any mismatch stops the run. The mock runtime refuses generated bundles.

   Under Docker (`services/docker_sim.py`) every run gets its own container from the simulation image, started with only Gazebo (`SIM_MODE=gazebo`) and the bundle mounted read-only at `/bundle`. Spawning, PX4, the identity check and the mission runner then go through `docker exec`, exactly as under WSL. The container is removed when the run stops, and any labelled container a crashed run left behind is removed before the next one starts.
3. **Acceptance** (`pipelines/simulation_acceptance.py`): for a generated design, the attestation must be `matched`, name the current spec and bundle fingerprints, and confirm the resolved design's rotor count and PX4 parameters. Otherwise the run fails with `generated_identity_attested=false`, and a code change cannot fix it. `generated_aircraft_validated` stays `false` at every scope.
4. **Record:** the attestation goes into graph state, the simulation row's `config.model_attestation`, the report's `aircraft_under_test`, and `selected_components.json`.

A fresh PX4 working directory per generated run matters: `PX4_PARAM_*` values persist in `parameters.bson`, and a shared directory would carry one design's geometry and gains into the next run, including reference runs. The run's ULog is written there too (`simulation_artifacts.px4_working_dir`).

## Verification script

From `apps/api`:

```bash
python scripts/check_generated_model_sitl.py --payload-g 125 --payload-g 400
```

It builds a documented catalog set or loads `--resolved-spec`. `--topology` picks the set:

| Topology | Motor | Propeller | Battery | Frame |
| --- | --- | --- | --- | --- |
| `quadcopter` (default) | T-MOTOR MN3110 KV700 | P12×4 | Tattu 1500 mAh 4S | Holybro X500 V2 |
| `hexacopter` | T-MOTOR MN3508 KV380 | P13×4.4 | Tattu Plus 10000 mAh 6S | Tarot 680 Pro |
| `octocopter` | T-MOTOR MN4014 KV400 | P15×5 | Tattu Plus 10000 mAh 6S | Tarot X8 |

T-MOTOR publishes each combination at the battery's nominal voltage. Hexa and octo use one single-channel ESC per arm.

It writes the bundle and calls `SimulationTestAgent.run_single` exactly as the graph does. The mission is the code agent's own deterministic output, with no LLM: a stationary hover (5 m, 15 s) by default, or a square with `--square-m 20`. It then evaluates the acceptance gate, which compares the flight with that code's mission ([SIMULATION_ACCEPTANCE.md](SIMULATION_ACCEPTANCE.md)), and reads PX4's hover-thrust estimate from the ULog (`pyulog` must be installed for the ULog step). Run it on Windows for the WSL runtime, or inside WSL with the API venv for the native runtime.

## Verified (WSL runtime, 2026-09-28)

Back-to-back runs of the script, one airframe after another. PX4 v1.17.0-alpha1-1535-g14529d6af3, Gazebo Harmonic, 5 m hover for 15 s. Every run passed acceptance with the identity attested, and none left a Gazebo or PX4 process behind.

| Airframe | Payload | Rotors / params read back | Hold | Altitude RMSE | Peak | Max drift | Model hover command | PX4 hover estimate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Quad, MN3110 + P12×4, 4S | 125 g | 4 / 28 | 16.1 s | 0.053 m | 5.01 m | 0.07 m | 0.4481 | 0.4487 |
| Hexa, MN3508 + P13×4.4, 6S (3 runs) | 1000 g | 6 / 40 | 16.0–16.1 s | 0.050–0.054 m | 5.00–5.02 m | 0.03–0.07 m | 0.6041 | 0.6051–0.6054 |
| Octo, MN4014 + P15×5, 6S | 2000 g | 8 / 52 | 16.0 s | 0.051 m | 5.02 m | 0.06 m | 0.5277 | 0.5284 |

The hold is the span from the first to the last sample within 0.2 m of the target. The model hover command is `MPC_THR_HOVER`, and the PX4 estimate is the median of `hover_thrust_estimate` in the run's ULog.

### Docker runtime (same day)

The same three airframes, each in its own container from the simulation image (`infrastructure/Dockerfile.sim`, same PX4 build). Environment: `ELJAZARI_FORCE_SITL=docker` on Docker Engine 28.3.2 (Docker Desktop for Windows). Every run passed with `real_docker_sitl` telemetry, `mission_isolation=linux_namespaces` under the container's seccomp profile ([MISSION_SANDBOX.md](MISSION_SANDBOX.md#docker)) and a matched attestation. No container was left behind.

| Airframe | Rotors / params read back | Simulated mass | Hold | Altitude RMSE | Peak | Max drift | Wall time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Quad | 4 / 28 | 1.462 kg | 15.9 s | 0.042 m | 5.02 m | 0.04 m | 57 s |
| Hexa | 6 / 40 | 3.713 kg | 16.2 s | 0.051 m | 5.01 m | 0.10 m | 62 s |
| Octo | 8 / 52 | 7.540 kg | 16.1 s | 0.045 m | 5.03 m | 0.06 m | 57 s |

There is no PX4 hover estimate for these runs: the ULog stays inside the container, which is removed when the run stops.

Getting there fixed three faults:
- the runner refused `--runtime-mode docker`;
- the image had been built from a working tree whose entrypoint had CRLF line endings, so the container exited at once;
- `--rm` deleted each failed container with its logs, so nothing said why. Containers are now removed explicitly, after their logs have been read.

Two faults showed up along the way and are fixed:
- **Recorded altitude.** It was the highest of four streams, one of them the raw barometer ([SIMULATION_ACCEPTANCE.md](SIMULATION_ACCEPTANCE.md)). A hexacopter holding 5.3–5.5 m was recorded at 6.1 m and failed. Before the fix the same airframe showed 0.07–0.44 m RMSE across runs; after it, 0.05 m every time.
- **Leftover simulators.** A Gazebo server outlived its run: the cleanup command was mangled on its way through `wsl.exe`, and runs that followed it sometimes stayed on the ground. The cleanup script now goes over stdin. Each run also gets its own Gazebo partition (`GZ_PARTITION=eljazari_<sim id>`), so a stray server can no longer capture another run's vehicle.

## Limits

- The thrust curves are manufacturer tables, not calibrations (see [PROPULSION_EVIDENCE.md](PROPULSION_EVIDENCE.md)). The quadratic fit deviates from the published points (up to 23% for the MN3110 KV700 + P12×4), and hover may lie below the lowest published point, where thrust is extrapolated. Both appear as bundle warnings. The comparison with real hardware is in [CALIBRATION.md](CALIBRATION.md).
- Motor time constants, rotor drag, rolling moment and sensor noise reuse PX4 x500 values. Wiring, fasteners and external sensors are not in the mass. Geometry and inertia use uniform-density primitives with placeholder placements.
- Agreement between the predicted hover command and PX4's estimate shows the simulated plant is the generated model. It does not show that the model matches the real aircraft.
- Flat X quadcopters, hexacopters and octocopters with identical propellers only. One simulator per host at a time, as before.

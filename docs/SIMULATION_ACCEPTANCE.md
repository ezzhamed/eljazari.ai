# Basic simulation acceptance

Simulation collection completion is not a pass verdict. Before semantic evaluation, the graph, the direct simulation loop and the `TestAgent` wrapper use the same deterministic checks. The final graph report rechecks its current state, including resumed runs and failures sent directly to reporting.

A run needs all of the following:

- Collection status `complete`.
- An exact backend source/runtime pair: `wsl` / `real_wsl_sitl`, `native` / `real_native_sitl`, or `docker` / `real_docker_sitl`.
- Mission start and completion reported as true, without a mission error.
- Crash and geofence violation flags reported as false.
- At least two samples containing finite numeric `timestamp_s`, `north_m`, `east_m` and `alt_m` values. Timestamps must be non-negative and strictly increasing. Non-finite numeric values, including values in nested motor samples, are rejected.
- `mission_isolation=linux_namespaces`: the mission ran in the sandbox, so it could not alter the telemetry being judged ([MISSION_SANDBOX.md](MISSION_SANDBOX.md)).
- For a generated design (resolved spec, flight bundle or attestation present): the simulator's attestation must be `matched` and name the current spec and flight-bundle fingerprints, with the resolved design's rotor count (4, 6 or 8) and PX4 parameters confirmed. See [FLIGHT_MODEL_SITL.md](FLIGHT_MODEL_SITL.md).

A run that passes all of the above must also have flown the mission its approved code commanded (`pipelines/flight_conformance.py`, recorded as `flight_conformance` with its metrics and limits):

| Check | Limit |
| --- | --- |
| Sampling | Median interval ≤ 0.2 s, no gap over 1 s |
| Mission | PX4 reported `MISSION` mode |
| Horizontal tracking | Every `MISSION` sample within 1 m of the commanded path, which runs from home at takeoff altitude through each waypoint |
| Altitude | Within 0.5 m of the commanded altitude no later than 10 s into the mission, then an RMSE ≤ 0.5 m |
| Hold | Each waypoint with a loiter time held that long, 1 s margin, within 1 m horizontally |
| Landing | Last sample ≤ 0.5 m high, within 1 m of home after return-to-launch (else of the last waypoint) |
| Boundary | Never farther from home than the code's geofence radius |

The limits are the engineering review's initial SITL targets (section 6.3), not airworthiness criteria.

The commanded path is read from the approved code: its `MissionItem` list, anchored on the vehicle's home through the template's `PLANNED_HOME_*` constants. It is not read from the code agent's plan, because a reviewer may edit the code and the code is what flew. Code whose mission cannot be read fails closed, and a code change can fix that. Tracking, altitude and hold failures cannot be fixed by a code change, so they do not trigger regeneration. The check runs only on a run that passed the checks above.

Before it existed, `flight_quality_solver` graded tracking only for legacy skill-API code and reported «not evaluated» for every real MAVSDK mission. The anchoring on the vehicle's home has since made the code's coordinates reliable.

How much margin real flights leave:

| Flights | Result |
| --- | --- |
| All 55 recorded SITL flights | Sampled every 0.1 s, no gap over 0.14 s |
| Published squares (20 m and 25 m) | Within 0.52–0.60 m of the path |
| Hovers of 2026-09-28, WSL and Docker | Within 0.08 m of the path |
| Every recorded hover | Held its commanded time |

The two published runs recorded the barometer-biased altitude described below. Their altitude RMSE against the command (0.13–0.33 m) is therefore an upper bound.

**Live flights through the gate (2026-09-28).** Generated aircraft flew the code agent's own 20 m square at 10 m and its 5 m hover (`scripts/check_generated_model_sitl.py --square-m 20`). Every flight passed:

| Flight | Max off path | Altitude RMSE | Landed from home |
| --- | --- | --- | --- |
| Quad square, WSL | 0.50 m | 0.078 m | 0.06 m |
| Hexa square, WSL | 0.58 m | 0.083 m | 0.03 m |
| Octo square, WSL | 0.63 m | 0.069 m | 0.04 m |
| Quad square, Docker | 0.55 m | 0.100 m | 0.11 m |
| Quad hover (15 s commanded, 15.9 s held), WSL | 0.06 m | 0.052 m | 0.08 m |

Each square's largest deviation is mid-leg, 5.5–6.6 m after the vehicle leaves a corner, and at least 0.37 m inside the limit. The same code with a 25 m square's telemetry fails at 6.9 m.

`alt_m` is PX4's estimated height above home (`telemetry.position().relative_altitude_m`), the frame the mission itself commands. The runner's other altitude streams stand in only while that one stays flat below 0.5 m; they are never combined with it. Until 2026-09-28 the runner recorded the highest of all four streams, one of them the raw barometer (`altitude_monotonic_m`, about 0.25 m of noise in PX4's simulated barometer). Single-sample spikes then became the flight's peak altitude: a hexacopter holding 5.3–5.5 m was recorded at 6.1 m and failed the requested-altitude check. Altitude RMSE and peak figures from earlier runs, the published benchmark included, carry that bias.

Synthetic fallback, partial or unknown sources do not pass. Missing runtime metadata is not upgraded to real telemetry. Native and Docker runs use the same single-connection mission runner as WSL and are labelled `real_native_sitl` and `real_docker_sitl` by it (under Docker the runner runs inside the run's container). The older direct collector opened a second MAVSDK client on the mission's UDP port and received only part of the stream: flight modes advanced while altitude stayed at ground level. It remains unlabelled and rejected, and no real runtime reaches it any more.

Rejected graph runs have no score and expose structured reasons in `simulation_acceptance`. Direct agent results use a zero score for compatibility with their existing non-null score schema. Infrastructure or malformed-telemetry failures stop code regeneration; mission failures may retry within the existing iteration limit. Accepted basic checks still require semantic evaluation, whose graph verdict must be boolean and whose score must be finite and within 0–100.

Current-attempt observations replace previous observations in graph state. Each attempt clears prior success, scores, telemetry and acceptance. Historical simulation records remain separate. Completion events do not announce a pass before evaluation. The acceptance result is included in evaluation events, graph reports and the persisted telemetry envelope.

## Faults injected in SITL

The review's negative protocol (section 6.4) asks for one outcome under a fault: a rejection or an understood failure, never a recovery at any cost. `scripts/check_sitl_faults.py` flies the code agent's own 20 m square at 10 m through `SimulationTestAgent.run_single`. A few seconds into the mission it injects one fault through PX4's shell (`GazeboService.px4_command`, limited to `failure`, `param` and `mavlink`), then records:
- what PX4 did;
- the gate's verdict;
- whether anything was left running.

A scenario passes only if the run is rejected with a stated reason and no process or container survives.

Results on 2026-09-28, PX4 v1.17 + Gazebo Harmonic. Every scenario was rejected and none left anything running:

| Fault | Injected as | What happened | Reason given |
| --- | --- | --- | --- |
| Battery empty | `failure battery off` | PX4 failsafe: HOLD, then LAND; landed | `mission.left_mode_incomplete mode=LAND px4=Failsafe activated` |
| GPS lost | `param set EKF2_GPS_CTRL 0` (the `failure gps` injection has no handler under Gazebo) | PX4 failsafe: LAND; landed | `mode=LAND px4=Failsafe activated` |
| Geofence smaller than the mission | The code's geofence set to 10 m for a 20 m square | PX4 refused the mission; the code returned the vehicle and landed | `mission.not_started mode=HOLD px4=No valid mission available, loitering` |
| MAVLink link cut | `mavlink stop-all` | No telemetry after 18.4 s | `Telemetry link lost after 18.4 s; the mission was stopped` |
| PX4 killed | SIGKILL of the daemon | No telemetry after 18.7 s | `Telemetry link lost after 18.7 s` |
| Motor 1 off, quadcopter | `failure motor off -i 1` with `CA_FAILURE_MODE=1` | Rolled past 90° and fell | `Loss of control at 19.3 s: tilted 92 degrees, descending 2.5 m/s at 9.5 m`, and `crash_detected` |
| Motor 1 off, hexacopter | the same | Stayed within 19° of level, but fell at up to 8 m/s | `Loss of control …: tilted 5 degrees, descending 4.4 m/s at 7.7 m` |
| Battery and motor, Docker runtime | as above | as under WSL | as under WSL |

Before these runs, three of the faults showed defects, now fixed:
- **The mission code overrode PX4's failsafe.** On an empty battery PX4 was landing. The code saw the mission end early and commanded return-to-launch, and the vehicle climbed from 10 m to 26.5 m. Now:
  - when PX4 leaves the mission on its own, the code leaves it in charge and waits for the landing;
  - the code acts only when PX4 never started the mission.
- **A refused mission hung.** PX4 never entered MISSION mode and the code waited out the 330 s timeout. It now gives up after 15 s and reports PX4's own text.
- **Frozen telemetry.** After the link was cut, or PX4 died, the runner kept recording the last values every 0.1 s. The record showed a vehicle holding 9.95 m for five minutes. Now:
  - a sample is recorded only while the position stream is live;
  - after 15 s of silence the run ends as `link lost`.
- **A fall was not a crash.** `crash_detected` only looked for altitude below −0.5 m. A quadcopter upside down on the ground, and a hexacopter that hit it at 8 m/s, both went unflagged while PX4 kept reporting MISSION. An airborne vehicle now counts as out of control if either holds:
  - it tilts past 70° (PX4 caps tilt at 45° by default, which generated designs keep);
  - it descends faster than 4 m/s (PX4 limits commanded descent to 1.5 m/s by default and never allows more than 4 m/s).

  Neither threshold was reached in 60 recorded flights: the largest were 19.1° and 1.55 m/s. The run then ends 5 s later.
- A failed mission's last error line, which quotes PX4 where it can, is now reported with its exit code. It is the mission's own text, so it explains and decides nothing.

A motor failure needs `CA_FAILURE_MODE > 0`. At the default 0, PX4 accepts the injection and ignores it, and the quadcopter flew its whole mission.

## Limits

`scope` is `reference_sitl_basic_checks` or `generated_sitl_basic_checks`, with `aircraft` and `generated_identity_attested` reported; `generated_aircraft_validated=false` stays explicit at every scope. Backend source labels are declarations, not authenticated provenance. Existing detectors and collector defaults determine crash/geofence flags; the gate does not independently establish that every hazard was detected. Trajectory, altitude, hold time, sampling and landing are now checked against the approved code's mission, as described above. Missions that use anything other than plain waypoints fail closed, not unchecked: a LAND or TAKEOFF item, or code that does not follow the template's form.

Position freshness is now enforced by the runner; the freshness of other streams is not. Also still not enforced: wind performance, battery reserves, disarming after landing (the runner records every sample as armed). The gate does not repair all provenance producers, authenticate the mission subprocess or bind database updates to a unique run ID. The fault scenarios above run as a script, not in CI, because they need real SITL. These remain required work before release acceptance.

The low-level `evaluate` and `evaluate_mission` methods assess semantic observations; they are not standalone flight acceptance APIs. Use the graph or `evaluate_simulation_result` with actual telemetry for a gated verdict.

Generated aircraft fly only through a verified `sitl_catalog_estimate` flight bundle whose spawned identity and PX4 parameters are attested ([FLIGHT_MODEL_SITL.md](FLIGHT_MODEL_SITL.md)). Designs without a bundle remain blocked. Catalog-derived physics is not calibration, and no real flight is certified by SITL or unit-test fixtures.

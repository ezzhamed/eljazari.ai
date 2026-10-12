# Calibration against real hardware

Every flight model in this project is an estimate. The generated aircraft flies in PX4 SITL on published motor/propeller tests ([propulsion evidence](PROPULSION_EVIDENCE.md)) and estimated inertia, drag and motor dynamics. This document is the protocol, and the tooling, for comparing that model with a real motor on a thrust stand and a real aircraft in the air.

**No calibration result exists yet.** The tools produce a result only from real logs, and none have been supplied. Nothing in the pipeline changes a design's scope from `sitl_catalog_estimate` on its own.

## What is compared

| Step | Measurement | Compared with | Tool |
| --- | --- | --- | --- |
| Bench | Thrust-stand log of one motor + propeller at one voltage | The catalog's published curve for that combination | `scripts/calibrate.py bench` |
| Flight | PX4 ULog of the real aircraft hovering | The flight model's predicted hover command | `scripts/calibrate.py flight` |

The code is in [`apps/api/pipelines/calibration.py`](../apps/api/pipelines/calibration.py). Its tests use synthetic logs and say so; they check parsing and arithmetic, not hardware.

## Bench protocol

**Equipment.** A thrust stand logging thrust, torque, optical RPM, voltage and current, for example a Tyto Robotics / RCbenchmark Series 1585. Also:
- a bench power supply able to hold the battery's nominal voltage (14.8 V for 4S, 22.2 V for 6S) under the full current;
- the exact motor, propeller and ESC part numbers the catalog names.

**Procedure.**
1. Mount the propeller in its flying orientation. Record the room temperature and the air pressure.
2. Hold the supply at the battery's **nominal** voltage. The tool refuses a run whose voltage strays more than 0.3 V from nominal. A curve at another voltage is never reused (the same rule as for manufacturer data).
3. Step the ESC command from idle to full, for example 1100 µs to 2000 µs in 100 µs steps.
   - Hold each step for at least 3 s. The first 30% of every step is discarded as settling.
   - Log at 10 Hz or faster.
4. Repeat three times, and keep every log. Each log becomes its own test, keyed by the SHA-256 of the CSV.
5. Export CSV with units in the headers, for example `Thrust (kgf)`, `Torque (N·m)`, `Motor Optical Speed (RPM)`, `Voltage (V)`, `Current (A)` and `ESC signal (µs)`. A column without a recognised unit is rejected, never guessed.

**Run the comparison.**

```bash
cd apps/api
python scripts/calibrate.py bench --csv run1.csv --motor tmotor_mn3110_700kv \
    --propeller tmotor_p12_4 --nominal-voltage 14.8 --date 2026-10-01 \
    --reference "RCbenchmark 1585, <site>, run 1" --out run1_test.json
```

The report gives:
- the fitted thrust coefficient (T = kω²) of the bench against the catalog curve;
- the thrust difference at each measured RPM;
- the **measured** yaw moment ratio (torque ÷ thrust). The flight model today *estimates* that ratio from electrical power at an assumed 80% efficiency.

The written `bench_test` entry follows the same contract as a manufacturer test. After review it can be appended to `data/components/propulsion_tests.json`. Selection then prefers it like any other sourced curve.

**How to read it.**

| Thrust coefficient difference | Meaning |
| --- | --- |
| Within ±5% | The published curve describes this motor and propeller at this voltage |
| Beyond ±5% | Use the bench curve for this combination and record why they differ: propeller batch, ESC, rig or air density |

A measured moment ratio far from the estimate means the yaw model, and with it the yaw rate gain, needs the measured value.

## Flight protocol

**Aircraft.** The aircraft must be built exactly to the design's resolved specification: the same parts, payload and mass. Weigh it; the bundle's mass is in `flight_manifest.json`.

**Procedure.**
1. Flash the same PX4 version as the SITL runs (`v1.17.0-alpha1-1535-g14529d6af3`). Load the design's parameters from the bundle (`px4_params`).
2. Keep full logging (`SDLOG_PROFILE` default, which includes `hover_thrust_estimate` and `actuator_motors`).
3. Hover in position mode, 2–5 m, in calm air, for at least 60 s, then land. Use a fully charged battery, and note its voltage before and after.
4. Keep the `.ulg` file.

**Run the comparison.**

```bash
python scripts/calibrate.py flight --ulog flight.ulg \
    --manifest <storage>/<spec sha>/flight/flight_manifest.json
```

The report compares PX4's own hover-thrust estimate (valid samples only) with the model's predicted hover command, with a default tolerance of ±0.05. It also records the median motor command, the battery voltage, the bundle's fingerprints and the log's SHA-256.

**How to read it.** A difference beyond the tolerance says the model does not describe the aircraft. Mass, thrust curve or motor response are the candidates; the bench step separates the curve from the rest. Several flights on the same airframe, at different payloads, show whether the error scales with mass (thrust curve) or stays constant (a fixed offset such as mass or ESC calibration).

## Sanity check of the tooling on SITL

Run on a SITL log, `calibrate.py flight` must report almost no difference, because the model *is* the simulator. It checks the tool, not the aircraft. It is the only result these tools have produced so far (2026-09-28, WSL runtime, the ULogs of the runs in [FLIGHT_MODEL_SITL.md](FLIGHT_MODEL_SITL.md#verified-wsl-runtime-2026-09-28)):

| Airframe | PX4 hover thrust (valid samples) | Model hover command | Difference |
| --- | --- | --- | --- |
| Quad, MN3110 KV700 + P12×4 | 0.4487 (274) | 0.4481 | 0.0006 |
| Hexa, MN3508 KV380 + P13×4.4 | 0.6051 (275) | 0.6041 | 0.0010 |
| Octo, MN4014 KV400 + P15×5 | 0.5284 (274) | 0.5277 | 0.0008 |

A real aircraft is not expected to come this close. The table shows only that the tool reads the log and the bundle correctly.

## Beyond hover (not provided)

- Rate-loop tuning: step responses per axis against the model's rate gains.
- Motor time constants from a thrust-stand step response.
- Drag from a forward-flight log.
- HIL with the real flight controller in the loop.

Each needs its own logs and is listed here so that its absence is visible, not assumed.

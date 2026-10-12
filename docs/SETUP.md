# Setup and operation

The full guide behind the README's quick start. A clean-clone run of these steps, with timings and what it found, is in [REPRODUCTION.md](REPRODUCTION.md).

- [Requirements](#requirements)
- [Downloads](#downloads)
- [Local setup](#local-setup)
- [PX4 and Gazebo](#px4-and-gazebo)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Ground control station](#ground-control-station)
- [Production](#production)
- [Services and ports](#services-and-ports)
- [Tests and flight checks](#tests-and-flight-checks)
- [Timings](#timings)
- [Known limitations](#known-limitations)

## Requirements

- Docker + Docker Compose v2+
- Node.js 22.13+ (or Node 24) and npm 10+. The frontend test dependencies require a recent Node runtime.
- Python 3.11–3.13. The pinned packages install from wheels on these versions; others are untested, and a package without a wheel needs a C++ compiler (on Windows, Microsoft C++ Build Tools)
- PostgreSQL 16+ (or a managed instance)
- [Ollama](https://ollama.com/)
- About 13 GB of downloads: Python packages 1.7 GB installed, `node_modules` 0.6 GB, the three Ollama models 10 GB
- For simulation: PX4 v1.17 and Gazebo Harmonic, either in WSL2 or Linux ([steps](#px4-and-gazebo)) or in the Docker simulation image

See [Local component data](LOCAL_COMPONENT_DATA.md) for optional manufacturer imports, their storage location and redistribution limits.

## Downloads

Everything a run needs beyond the repository, with sizes measured on 2026-09-30.

| What | Size | Needed for | How |
|---|---|---|---|
| Python packages | 1.7 GB installed | everything | `pip install -r apps/api/requirements.txt` |
| Web packages | 0.6 GB | the web app | `npm ci --legacy-peer-deps` at the repository root |
| Ollama models: `gemma3:4b`, `llama3.2:latest`, `qwen2.5-coder:7b` | 3.3 + 2.0 + 4.7 GB | design, code, analysis | `prepare_assets --models` |
| Ollama `nomic-embed-text` | 0.3 GB | retrieval, if sentence-transformers is missing | `prepare_assets --models` |
| Embedder `all-MiniLM-L6-v2` | 87 MB | retrieval | `prepare_assets --embedder`; otherwise fetched on the first design |
| Retrieval index | depends on corpus and embedding dimensions | retrieval | `prepare_assets --index`; otherwise built on the first design |
| AircraftVerse corpus | 65 MB | retrieval | in the repository; to rebuild from source, see step 2 (12.3 GB download) |
| Simulation image `eljazari-sim` | 7.4 GB | the Docker simulation runtime | `prepare_assets --sim-image` |
| PX4 v1.17 (source and build) | 3.8 GB | simulation in WSL or Linux | [by hand](#px4-and-gazebo) |
| Gazebo Harmonic | apt packages | simulation in WSL or Linux | [by hand](#px4-and-gazebo) |
| MAVSDK environment | 83 MB | running missions in WSL or Linux | [by hand](#px4-and-gazebo) |

One command checks all of it and fetches what is missing:

```bash
cd apps/api
python -m scripts.prepare_assets                      # status and sizes only
python -m scripts.prepare_assets --all                # models, embedder and index
python -m scripts.prepare_assets --all --sim-image    # and the simulation image
```

It asks before downloading (`--yes` skips the question). The API reuses the index with `AIRCRAFTVERSE_RAG_MODE=persistent`, as in `.env.example`; in `memory` mode each API process builds its own. PX4 and Gazebo need `sudo`, so the script only checks them.

## Local setup

Run each block from the repository root unless it says otherwise. Keep the backend virtual environment active for Python commands; use a separate terminal for each long-running service.

### 1. Clone, configure and install backend dependencies

```bash
git clone https://github.com/ezzhamed/eljazari.ai.git
cd eljazari.ai
cp .env.example .env
```

Install dependencies before running any of the preparation scripts:

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m scripts.bootstrap_components  # local-only data for sourced flight checks
cd ../..
```

Set at least `DATABASE_URL`, `API_SECRET_KEY`, `OLLAMA_BASE_URL`, and JSON arrays for `CORS_ORIGINS` and `TRUSTED_HOSTS`. Relative paths in `.env` are taken from the repository root.

The backend reads the repository-root `.env` even when `uvicorn` starts from `apps/api`. **The frontend does not**: Next.js only reads env files from `apps/web/`. The defaults in `apps/web/lib/config.ts` point at `http://localhost:8000`, so a standard local setup needs no frontend configuration. If your backend runs elsewhere:

```bash
cp apps/web/.env.example apps/web/.env.local   # then edit
```

### 2. Verify the design corpus

`data/aircraftverse/designs.json` is a repackaged copy of the real AircraftVerse corpus (27,714 designs, NeurIPS 2023), so retrieval works with no download. The design data is **CC-BY-4.0, not MIT**; see [DATASET_LICENSE.md](../data/aircraftverse/DATASET_LICENSE.md).

```bash
cd apps/api
python -m scripts.bootstrap_aircraftverse   # checks structure and diversity
```

With `AIRCRAFTVERSE_RAG_MODE=persistent` (the example's setting) the retrieval index is built once, on the first design or ahead of time with `prepare_assets --index`, and kept on disk; in `memory` mode every API process builds its own, about 80 s. The embedder, `all-MiniLM-L6-v2` (87 MB), downloads from Hugging Face on first use unless `prepare_assets` fetched it.

<details>
<summary>Rebuilding the corpus from source</summary>

```bash
cd apps/api
# Part 1 only: ~9,200 designs, ~4.1 GB download
python -m scripts.download_aircraftverse --parts 1 --cleanup

# Full corpus: all three Zenodo parts, ~12.3 GB downloaded then deleted
python -m scripts.download_aircraftverse --parts 1,2,3 --cleanup
```

The downloader also fetches `corpus_dic`, the AircraftVerse component catalogue, and resolves every motor, propeller and battery against it. It produces `designs.json`, `corpus_report.json` and the retrieval index under `data/aircraftverse/`.

Motor counts in the corpus run from 1 to 16, about 28% of designs are fixed-wing or VTOL hybrids, and roughly two thirds failed physics evaluation at the source. Records are labelled with `airframe_class` and `evaluated`, and retrieval uses evaluated multirotors only: 7,815 designs. See [data/aircraftverse/README.md](../data/aircraftverse/README.md).

</details>

### 3. Prepare Ollama models

Start `ollama serve` in a separate terminal and leave it running. Then, in the terminal with the backend virtual environment active:

```bash
cd apps/api
python -m scripts.prepare_assets --models
```

Pulls the models named by `OLLAMA_DESIGN_MODEL`, `OLLAMA_DEFAULT_MODEL`, `OLLAMA_CODE_MODEL`, `OLLAMA_EVAL_MODEL` and `OLLAMA_EMBED_MODEL` that are missing. With `.env.example` that is `gemma3:4b`, `llama3.2:latest`, `qwen2.5-coder:7b` and `nomic-embed-text`, about 10 GB. (`python -m scripts.bootstrap_ollama --pull-missing` does the same without the embedding model.)

### 4. Start PostgreSQL and Redis

```bash
docker compose -f infrastructure/docker-compose.dev.yml up -d postgres redis
```

- Add `ollama` only if you did not start Ollama in step 3. Both listen on port 11434.
- If PostgreSQL or Redis already runs on this machine, ports 5432 and 6379 clash. Use those servers, or publish other ports and change `DATABASE_URL`, `REDIS_URL` and the two `CELERY_*` URLs in `.env`.

### 5. Start the backend

```bash
cd apps/api
# Use the virtual environment created in step 1.
alembic upgrade head
python -m scripts.seed
uvicorn main:socket_app --reload --port 8000
```

OpenAPI docs are at <http://localhost:8000/docs>. `GET /health` should report `"dataset": "real"`; `fallback` means the corpus was not found and designs are made without retrieval.

Without PX4 and Gazebo, runs end at simulation with demo telemetry (`simulated_fallback`) that never passes the gate. Set `ELJAZARI_REQUIRE_REAL_SITL=true` to forbid it.

### 6. Start the frontend

```bash
# New terminal, from the repository root
npm ci --legacy-peer-deps
npm run dev -w @eljazari/web
```

Open <http://localhost:3000>.

### 7. Authentication

If anonymous auth is disabled, request a JWT from `POST /api/auth/token` and send it as `Authorization: Bearer <token>`.

> [!WARNING]
> **That endpoint is not authentication.** It signs a token for whatever `user_id` you send, without checking any credential; the project ships no user store and no login flow. It is disabled when `ENVIRONMENT=production`, and the API refuses to start if you force it on there. For real use, front the API with an identity provider that issues the JWTs `middleware.auth` verifies. See [SECURITY.md](../SECURITY.md).

## PX4 and Gazebo

For simulation in WSL2 or on a Linux host (Ubuntu 24.04). These are the steps the simulation image runs ([Dockerfile.sim](../infrastructure/Dockerfile.sim)), adapted to a user account. The maintainer's WSL was prepared earlier by hand, and this sequence has not been re-run on a fresh Ubuntu, so report anything that differs.

```bash
# Gazebo Harmonic, from the OSRF repository
sudo apt-get update && sudo apt-get install -y curl gnupg lsb-release git python3-venv
sudo curl https://packages.osrfoundation.org/gazebo.gpg   --output /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg]   http://packages.osrfoundation.org/gazebo/ubuntu-stable $(lsb_release -cs) main"   | sudo tee /etc/apt/sources.list.d/gazebo-stable.list
sudo apt-get update && sudo apt-get install -y gz-harmonic

# PX4 at the commit every result in docs/ was produced with
git clone https://github.com/PX4/PX4-Autopilot.git ~/PX4-Autopilot
cd ~/PX4-Autopilot
git checkout 14529d6af3adad05b938fd6bc87cb92c3920147b
git submodule update --init --recursive
bash ./Tools/setup/ubuntu.sh --no-nuttx --no-sim-tools
make px4_sitl_default -j4      # -j4: all cores can exhaust WSL's memory

# The Python that runs mission code
python3 -m venv ~/droneprompt-mavsdk-venv
~/droneprompt-mavsdk-venv/bin/pip install mavsdk==3.15.3
```

Then point `.env` at them (these names are in `.env.example`), and check:

```bash
DRONEPROMPT_WSL_DISTRO=Ubuntu
DRONEPROMPT_WSL_USER=<your Linux user>
DRONEPROMPT_WSL_PX4_DIR=/home/<user>/PX4-Autopilot
DRONEPROMPT_WSL_WORLDS_PATH=/home/<user>/PX4-Autopilot/Tools/simulation/gz/worlds
DRONEPROMPT_WSL_PYTHON_BIN=/home/<user>/droneprompt-mavsdk-venv/bin/python
```

```bash
cd apps/api && python scripts/doctor.py      # PX4 build, Gazebo, MAVSDK, mission sandbox
```

For the Docker runtime instead, build the image (`prepare_assets --sim-image`) and set `ELJAZARI_FORCE_SITL=docker`.

## Command line

Runs the full pipeline without the web stack, with an in-memory checkpointer. Models and paths come from `.env`, as for the API.

```bash
# Interactive: asks for approval or feedback at each review gate
python cli.py

# Non-interactive
python cli.py --prompt "A small quadcopter carrying a 150 g camera. Take off, hover at 10 m for 20 seconds, then land." --auto-approve --output-dir my_drone_run
```

| Flag | Purpose |
|---|---|
| `-p`, `--prompt` | The drone and mission, in English or Arabic (asked for if omitted) |
| `-o`, `--output-dir` | Where to write `model.urdf`, `mission_safe.py`, `design_spec.json` and the deliverables. Default `cli_runs/latest` |
| `--auto-approve` | Pass the review gates without asking |
| `--design-model`, `--code-model`, `--eval-model` | Override the Ollama model per stage |
| `--max-speed`, `--max-weight` | Custom flight limits |
| `--max-sim-iter` | Maximum simulation attempts |

## Configuration

`.env.example` is the source of truth. The essentials:

```bash
# App and security
ENVIRONMENT=development
DEBUG=true
ALLOW_ANONYMOUS_AUTH=true
API_SECRET_KEY=change-me-in-production-32-chars!!
CORS_ORIGINS=["http://localhost:3000","http://localhost:8000"]
TRUSTED_HOSTS=["localhost","127.0.0.1"]

# Database and inference
DATABASE_URL=postgresql+asyncpg://drone:drone@localhost:5432/droneprompt
OLLAMA_BASE_URL=http://localhost:11434
```

**Variable names.** Project variables use the `ELJAZARI_` prefix. The older `DRONEPROMPT_` spelling still works, and `ELJAZARI_` wins when both are set:

```bash
ELJAZARI_SITL_RUNTIME=wsl     # canonical
DRONEPROMPT_SITL_RUNTIME=wsl  # legacy, still honoured
```

The compatibility layer is `apps/api/config/env_names.py`. Unprefixed variables (`DATABASE_URL`, `API_SECRET_KEY`, `NEXT_PUBLIC_*`, `GCS_*`, `AIRCRAFTVERSE_*`) are unaffected. Two identifiers keep the old spelling on purpose: the Postgres database name and WSL user and paths, which name resources on your machine; and the string `drone-prompt-dev-secret-key-2026`, a blocklist entry for a leaked development key that the production guard rejects.

## Ground control station

`/dashboard/gcs` streams MAVLink telemetry, sends flight commands, uploads waypoint missions, and reads and writes PX4 PID parameters over Socket.io. It attaches to the project the socket has joined, so open a project first.

| Setting | Default | Purpose |
|---|---|---|
| `GCS_MAVLINK_ADDRESS` | `udp://:14540` | PX4 SITL UDP port. `serial:///dev/ttyUSB0:57600` or `tcp://<ip>:5760` for hardware |
| `GCS_CONNECT_TIMEOUT_S` | `30` | Connection timeout |
| `GCS_TAKEOFF_ALTITUDE_M` | `10` | Takeoff altitude when the client sends none |

> [!NOTE]
> **`mavsdk` is needed for real flight.** It is in `requirements.txt` but not in `requirements-ci.txt`. Without it the bridge runs in **stub mode**: it sends a made-up orbit and acknowledges commands that never reach a vehicle. Responses carry `"stub": true`, telemetry samples `"synthetic": true`, and the page shows a warning while it receives them.

Flight commands go through an allow-list (`arm`, `disarm`, `takeoff`, `land`, `rtl`, `pause`, `estop`, `start_mission`), because on hardware they arm motors and cut throttle.

## Production

Minimum:

- `ENVIRONMENT=production` and `ALLOW_ANONYMOUS_AUTH=false`
- An external identity provider issuing JWTs signed with `API_SECRET_KEY`
- `AIRCRAFTVERSE_REQUIRE_REAL=true`
- A non-default `API_SECRET_KEY`, and real `CORS_ORIGINS` and `TRUSTED_HOSTS`

```bash
# Bootstrap helpers
docker compose -f infrastructure/docker-compose.yml --profile bootstrap run --rm dataset-bootstrap
docker compose -f infrastructure/docker-compose.yml --profile bootstrap run --rm ollama-bootstrap

# The stack
docker compose -f infrastructure/docker-compose.yml up -d

# Simulation (Linux hosts, host networking)
docker compose -f infrastructure/docker-compose.yml --profile simulation up -d simulation
```

Full checklist: [infrastructure/PRODUCTION_CHECKLIST.md](../infrastructure/PRODUCTION_CHECKLIST.md).

## Services and ports

| Service | Port | Description |
|---|---|---|
| Web (Next.js) | 3000 | Workspace, 3D viewer, ground control |
| API (FastAPI + Socket.io) | 8000 | REST, live events, review endpoints |
| PostgreSQL | 5432 | Projects, designs, simulations, logs, checkpoints |
| Redis | 6379 | Cache, Celery queue, Socket.IO message queue |
| Ollama | 11434 | Local LLM inference |
| Flower | 5555 | Celery monitoring |
| Prometheus / Grafana | 9090 / 3001 | Metrics and dashboards |
| PX4 SITL + Gazebo | 14540 / 14550 / 11345 | Simulation |

## Tests and flight checks

```bash
cd apps/api && pytest -q        # backend
cd apps/web && npm run test     # frontend
```

CI runs ruff, MyPy on the safety-critical modules, both suites (including the mission-sandbox containment test) and the image builds. Unit tests are not flight evidence. The flight checks need a machine with PX4:

```bash
cd apps/api
python scripts/doctor.py                     # services, models, PX4, Gazebo, sandbox, disk
python scripts/check_generated_model_sitl.py --topology quadcopter --payload-g 400
python scripts/check_sitl_faults.py          # injected faults must end in a rejection
python scripts/check_multiworker.py          # inside Linux/WSL, with PostgreSQL and Redis
python scripts/check_run_persistence.py <project-id>   # inside Linux/WSL: a pipeline worker can save its simulation record
python scripts/run_benchmark.py --supervise --out ../../benchmarks/results/<run-id>   # the prompt suite
```

`.github/workflows/sitl.yml` runs the flight checks on a self-hosted `px4-sitl` runner, and `release.yml` drafts a release only when CI, the secret scan and SITL passed on the same commit ([release gate](RELEASE_GATE.md)).

## Timings

Measured on the development machine (Windows 11, WSL2, local Ollama) on 2026-09-30, for a camera hover:

| Stage | Time |
|---|---|
| Design, including the first index build (~80 s) | 2.5–4.5 min |
| Mission code | 1–2 min |
| SITL flight and acceptance gate | 1.5–2 min |
| Whole run, CLI with auto-approve | 503 s |

They depend on the machine's CPU and GPU, and on the Ollama models.

## Known limitations

- Windows needs a prepared WSL2 Ubuntu with PX4, Gazebo and a MAVSDK virtualenv for real SITL; from WSL, the API needs the Ollama bridge (`infrastructure/start-wsl-ollama.ps1`) after each Windows restart.
- Hosts without a real SITL runtime fall back to demo telemetry (`simulated_fallback`), which the gate always rejects.
- Generated aircraft use catalogue-estimate physics: manufacturer thrust tables, not calibration, and PX4 x500 reference coefficients for motor dynamics and drag.
- Mission sandboxing needs Linux with unprivileged user namespaces (WSL2 has them); other hosts refuse to run generated code.
- The selected catalogue components and their sourced performance are not shown in the web app yet.
- With in-memory checkpoints, restarting the API loses a run waiting for review.
- The LLM monitor summary can time out on slow machines; a deterministic summary from the telemetry replaces it.
- Only about 28% of AircraftVerse designs are buildable, physics-evaluated multirotors (7,815), and they skew toward hexacopters.
- `AIRCRAFTVERSE_REQUIRE_REAL=true` checks existence, structure and diversity; it cannot verify provenance.
- `PX4_HOME_LAT` / `LON` / `ALT` have no effect under Gazebo Harmonic; the vehicle spawns at the world's `<spherical_coordinates>`.
- No hardware, HIL or real flight results exist.

The current retrieval backend is documented in [Embedded retrieval](EMBEDDED_RETRIEVAL.md). Historical Chroma timing and compatibility notes do not describe the new index.

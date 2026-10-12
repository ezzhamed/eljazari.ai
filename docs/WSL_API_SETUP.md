# Running the API in WSL

Historical local verification: 20 September 2026, Python 3.12 with PostgreSQL and Redis inside WSL. The API ran with one worker and durable workflow checkpoints. This demonstrated service and checkpoint operation, not aircraft airworthiness. See [SETUP.md](SETUP.md) for the current general setup.

## Install

From the repository root in WSL, with PostgreSQL, Redis and connection settings in `.env`:

```bash
python3 -m venv "$HOME/droneprompt-api-venv"
"$HOME/droneprompt-api-venv/bin/python" -m pip install --upgrade pip
"$HOME/droneprompt-api-venv/bin/python" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
"$HOME/droneprompt-api-venv/bin/python" -m pip install -r apps/api/requirements.txt
"$HOME/droneprompt-api-venv/bin/python" -m pip check
```

The API environment is separate from the MAVSDK mission environment. This example uses the CPU PyTorch distribution. Existing installations do not need to be recreated solely because this guide exists.

## Start

From the repository root, using a free port:

```bash
"$HOME/droneprompt-api-venv/bin/python" apps/api/scripts/run_wsl_api.py --port 8001
```

Health: <http://localhost:8001/health>. API reference: <http://localhost:8001/docs>. Stop a foreground process with `Ctrl+C`.

The launcher sets Linux data/storage paths, PostgreSQL checkpoints and native PX4 simulation. Its default PX4 location is `$HOME/PX4-Autopilot`. It disables automatic Docker-service startup because this configuration uses existing local services. It does not modify `.env`.

Override values through process environment variables or the ignored `.env.wsl.local` file at the repository root. Precedence is process environment, then that local file, then launcher defaults. Other connection settings and secrets still come from the application's `.env`. Keep `CHECKPOINT_BACKEND=postgres` for durable checkpoints.

Historical runtime records were `$HOME/droneprompt-api-runtime/server.log` and `server.json`. This was a local process, not an automatically installed boot service. Check its identity and listening port before launching another instance.

## Historical checkpoint verification

`/health` was reachable from WSL and Windows; `/docs` was reachable from WSL. Three separate PostgreSQL client processes saved, paused, resumed and read a test checkpoint, then removed the temporary schema. Forty-six focused checkpoint, production-settings and simulation-record tests passed. This did not establish restart recovery for a complete flight.

From `apps/api`, rerun the checkpoint check with:

```bash
"$HOME/droneprompt-api-venv/bin/python" scripts/check_checkpoint_restart.py
```

## Ollama access from WSL

The historical machine ran its primary Ollama server on Windows at port 11434 and a project-specific listener on the WSL interface at port 11435. Both used the same downloaded model files. The project listener unloads models between requests to reduce memory use, which can increase latency.

From PowerShell at the repository root:

```powershell
.\infrastructure\start-wsl-ollama.ps1
```

The script discovers the current WSL interface address, checks the port and reuses its own existing process. It writes `OLLAMA_BASE_URL` to `.env.wsl.local`, preserving other settings. Restart the API after an address change. The listener binds to the WSL interface; this setup did not add a firewall rule or boot service. Logs and process metadata are under `%LOCALAPPDATA%\DronePrompt`.

The historical check listed the six installed models, generated valid design JSON using `gemma3:4b` and generated a 768-value embedding with `nomic-embed-text`. Those checks established model connectivity, not a full design or flight.

## AircraftVerse index

An older WSL setup used a separate `data/storage/chroma-wsl-0.6.3` index to avoid opening an incompatible index. A fresh index of 5,000 designs supported retrieval and reached design review. Current index reuse and version behavior are described in [SETUP.md](SETUP.md); do not treat that historical path as a required modern Chroma version.

The project can remain in `designing` while paused at `human_review_design`. Reaching that checkpoint does not approve the design or complete component selection and simulation.

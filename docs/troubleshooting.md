# eljazari.ai — Troubleshooting Guide


### AircraftVerse dataset missing

```bash
cd apps/api
python -m scripts.bootstrap_aircraftverse
python -m scripts.bootstrap_aircraftverse --from-backup
```

If production is configured with `AIRCRAFTVERSE_REQUIRE_REAL=true`, API startup will fail until the dataset exists.

### Ollama models are missing

```bash
cd apps/api
python -m scripts.bootstrap_ollama --pull-missing
curl http://localhost:11434/api/tags
```

### WebSocket not connecting

- Verify `NEXT_PUBLIC_WS_URL` matches the API URL
- Ensure the API is started as `uvicorn main:socket_app`
- Check `CORS_ORIGINS` and `TRUSTED_HOSTS`

### Linux simulation stack issues

```bash
docker compose -f infrastructure/docker-compose.yml --profile simulation up -d simulation
```

The `simulation` profile is Linux-oriented and is not expected to provide full physics on Windows.

The container starts Gazebo Harmonic and PX4 v1.16 SITL itself; there is nothing to launch inside it. Startup is logged with a `[sim-entrypoint]` prefix, and it fails loudly with the missing path named rather than exiting silently:

```bash
docker compose -f infrastructure/docker-compose.yml logs -f simulation
```

**MAVSDK connects but never receives telemetry.** PX4's stock link targets localhost, and the `simulation` service uses `network_mode: host` while `api` sits on the bridge network — so the packets never cross. Give PX4 an addressed link:

```bash
PX4_MAVLINK_TARGET_HOST=<host reachable from the api container> \
  docker compose -f infrastructure/docker-compose.yml --profile simulation up -d simulation
```

**Choosing an airframe or world.** `SIM_MODEL` is the bare name (`x500`), from which the entrypoint derives `PX4_SIM_MODEL=gz_x500`. `SIM_WORLD` names a `.sdf` in `SIM_WORLDS_PATH`, which defaults to the worlds PX4 ships with; point it at `/simulation/gazebo/worlds` to use this repo's own.

**Expected on every boot, and harmless:**

```text
[Err] [SystemLoader.cc:92] Failed to load system plugin [libGstCameraSystem.so]
```

PX4's stock `server.config` lists a GStreamer camera-streaming plugin that the image does not build, because nothing here simulates video. Gazebo logs the miss and continues; PX4, the gz bridge and MAVLink are unaffected.

**`PX4_HOME_LAT` / `PX4_HOME_LON` / `PX4_HOME_ALT` have no effect under Gazebo Harmonic.** The vehicle spawns at the `<spherical_coordinates>` declared by the world file, so the stock world puts it at Zurich (47.398, 8.546) no matter what these are set to — verified by reading both the process environment and the resulting telemetry. To move the origin, edit the world's `<spherical_coordinates>` and point `SIM_WORLDS_PATH` at it.

### Windows WSL SITL diagnostics

```bash
cd apps/api
python -m scripts.check_sitl_runtime
```

If the output contains `WSL/Service/E_ACCESSDENIED`, run the real SITL smoke from a normal PowerShell session that has permission to invoke `wsl.exe`. The backend will use mock/headless simulation in restricted sessions.

### Running locally without Docker (Windows)

Docker's only job in the daily loop is Postgres and Redis. Ollama runs on the host, the API under uvicorn, the web app under `next dev`, and PX4 SITL inside WSL — which is already a requirement. So both services can move into that same WSL distro and Docker Desktop drops out of local development entirely:

```powershell
./infrastructure/dev-services.ps1          # start and verify
./infrastructure/dev-services.ps1 -Action Status
```

One-time setup inside the distro:

```bash
sudo apt-get install -y postgresql-16 redis-server
sudo pg_conftool 16 main set port 5432     # Ubuntu may pick 5433
sudo systemctl enable --now postgresql redis-server
sudo -u postgres psql -c "CREATE ROLE drone LOGIN PASSWORD '<POSTGRES_PASSWORD from .env>'"
sudo -u postgres createdb -O drone droneprompt
cd apps/api && python -m alembic upgrade head
```

The versions match what compose used — `postgresql-16` against `postgres:16-alpine`, `redis-server 7.0.15` against `redis:7-alpine` — and WSL2 forwards localhost, so `DATABASE_URL` and `REDIS_URL` in `.env` work unchanged.

To bring existing data across before switching, dump it while the container is still running:

```bash
docker exec -e PGPASSWORD=<pw> infrastructure-postgres-1 pg_dump -U drone -d droneprompt --clean --if-exists > dump.sql
wsl -e bash -lc "PGPASSWORD=<pw> psql -h 127.0.0.1 -p 5432 -U drone -d droneprompt -f /mnt/c/path/to/dump.sql"
```

Verify the restore against the WSL cluster explicitly — `psql` without `-p` will happily talk to a still-running container instead and appear to succeed.

**The keepalive is not optional.** A WSL distro sleeps once nothing is running inside it, and a sleeping distro refuses connections from Windows with `WinError 1225`. `alembic current` failed that way between two commands that each worked on their own. `dev-services.ps1` holds one process open to prevent it; systemd restarts the services whenever the distro wakes, so data is never at risk.

This changes local development only. CI still builds images on Linux runners, where none of this applies, and compose and k8s remain the deployment path.

### Docker Desktop will not start on Windows

If `docker version` hangs and the Docker Desktop window shows an error, check its log:

```powershell
Select-String -Path "$env:LOCALAPPDATA\Docker\log\host\com.docker.backend.exe.log" -Pattern 'GET /error \(' | Select-Object -Last 1
```

A message like this is the known failure:

```text
starting services: initializing Inference manager:
remove ...\Docker\run\dockerInference: The file cannot be accessed by the system.
```

Docker Desktop 4.44.3 initialises its Inference manager on every startup even when Model Runner is disabled, and that step calls `remove()` on an AF_UNIX socket reparse point in `%LOCALAPPDATA%\Docker\run`. Once stranded, Windows refuses every operation on that file — `Remove-Item`, `del`, `ren`, even `fsutil reparsepoint query` all return error 1920 — and startup aborts.

Measured on a Windows 11 machine running 4.44.3: the sockets survive **every** shutdown including `docker desktop stop`, and are unremovable in all cases, so Docker Desktop cannot restart itself once it has run. It is not a consequence of force-killing it or of `wsl --shutdown`.

Launch it through the helper instead, which renames the containing directory — that touches only the parent's directory entry and never opens the sockets, which is why it succeeds where deletion cannot:

```powershell
./infrastructure/docker-desktop.ps1 Start
```

Use `Stop` before `wsl --shutdown` (needed when compacting the WSL disk) so containers are not cut off mid-write. It does not stop the sockets being stranded; nothing outside Docker can.

The real fix is upstream — a release that skips the Inference manager when the feature is off, or tolerates a stale socket. `docker desktop disable model-runner` is **not** sufficient: the manager is still constructed before the setting is read.

### Database migration fails

```bash
cd apps/api
alembic downgrade base
alembic upgrade head
```

### Frontend build errors

```bash
cd apps/web
npm install
npm run lint
npm run build
```

---


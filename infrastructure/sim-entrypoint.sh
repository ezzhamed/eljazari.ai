#!/usr/bin/env bash
#
# Boot PX4 SITL + Gazebo Harmonic inside the sim container.
#
# This deliberately mirrors GazeboService._create_wsl_px4_process and
# _create_wsl_gazebo_process in apps/api/services/gazebo_service.py rather than
# inventing a second launch contract. The WSL path is the one that is known to
# fly in this project, so the container reproduces its environment exactly:
# Gazebo runs standalone as the server, PX4 attaches to it via PX4_GZ_STANDALONE=1,
# and both are supervised here.
#
# Lives in /usr/local/bin, NOT /simulation — docker-compose bind-mounts the host's
# simulation/ over /simulation, which would shadow anything shipped there.

set -euo pipefail

PX4_DIR="${PX4_DIR:-/opt/px4/PX4-Autopilot}"
BUILD_DIR="$PX4_DIR/build/px4_sitl_default"
ROOTFS_DIR="$BUILD_DIR/rootfs"

# Model naming follows GazeboService._model_name / _px4_sim_model: PX4_GZ_MODEL is
# the bare name, PX4_SIM_MODEL is the gz_-prefixed airframe that selects autostart.
SIM_MODEL="${SIM_MODEL:-x500}"
SIM_MODEL="${SIM_MODEL#gz_}"
SIM_WORLD="${SIM_WORLD:-default}"
SIM_WORLDS_PATH="${SIM_WORLDS_PATH:-$PX4_DIR/Tools/simulation/gz/worlds}"

# Defaults match config/settings.py: MAVLink GCS on 14550, MAVSDK on 14540.
MAVLINK_UDP_PORT="${MAVLINK_UDP_PORT:-14550}"
MAVSDK_PORT_REMOTE="${MAVSDK_PORT_REMOTE:-14540}"

GZ_READY_TIMEOUT="${GZ_READY_TIMEOUT:-60}"

log() { printf '[sim-entrypoint] %s\n' "$*" >&2; }
fail() { log "FATAL: $*"; exit 1; }

# ── preflight ────────────────────────────────────────────────────────────────
# Fail with the missing path named. A container that exits saying "no such file
# or directory" is what this script exists to replace.
[ -x "$BUILD_DIR/bin/px4" ] || fail "PX4 binary missing at $BUILD_DIR/bin/px4"
[ -d "$ROOTFS_DIR" ] || fail "PX4 rootfs missing at $ROOTFS_DIR"

WORLD_FILE="$SIM_WORLDS_PATH/${SIM_WORLD}.sdf"
[ -f "$WORLD_FILE" ] || fail "world not found: $WORLD_FILE (set SIM_WORLD / SIM_WORLDS_PATH)"

# ── environment ──────────────────────────────────────────────────────────────
# ROS's generated setup scripts read variables they do not always define, so -u
# has to come off for the duration or sourcing aborts the container at boot.
set +u
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
set -u

# Headless: mirrors the WSL path, which runs without any display attached.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-root}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR" || true
export QT_QPA_PLATFORM=offscreen
export LIBGL_ALWAYS_SOFTWARE=1

# Exactly GazeboService._gz_runtime_env().
export PX4_GZ_MODELS="$PX4_DIR/Tools/simulation/gz/models"
export PX4_GZ_WORLDS="$SIM_WORLDS_PATH"
export PX4_GZ_PLUGINS="$BUILD_DIR/src/modules/simulation/gz_plugins"
export PX4_GZ_SERVER_CONFIG="$PX4_DIR/src/modules/simulation/gz_bridge/server.config"
export GZ_SIM_SYSTEM_PLUGIN_PATH="$BUILD_DIR/src/modules/simulation/gz_plugins"
export GZ_SIM_RESOURCE_PATH="$PX4_GZ_MODELS:$SIM_WORLDS_PATH${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"

export PX4_SIM_HOSTNAME="${PX4_SIM_HOSTNAME:-localhost}"
export PX4_GZ_STANDALONE=1
export PX4_GZ_MODEL="$SIM_MODEL"
export PX4_SIM_MODEL="gz_$SIM_MODEL"
export PX4_GZ_WORLD="$SIM_WORLD"
export MAVLINK_UDP_PORT

# Home position — already plumbed through docker-compose.yml.
export PX4_HOME_LAT="${PX4_HOME_LAT:-24.7136}"
export PX4_HOME_LON="${PX4_HOME_LON:-46.6753}"
export PX4_HOME_ALT="${PX4_HOME_ALT:-620.0}"

# ── MAVSDK link ──────────────────────────────────────────────────────────────
# The same patch the WSL runtime applies (apps/api/utils/px4_mavlink_patch.py):
# keep PX4's single stock onboard link on MAVSDK's port, raise its telemetry
# rates, and address it with -t when MAVSDK runs in another container or host.
# A second `-m custom` link used to be appended here; two links answering one
# client broke the mission upload handshake, which the WSL path had to fix.
# PX4 is started with -d ROMFS, so rootfs/etc resolves to this file.
rc_mavlink="$PX4_DIR/ROMFS/px4fmu_common/init.d-posix/px4-rc.mavlink"
# PX4 parses -t with inet_aton, so a hostname such as `api` or
# host.docker.internal is rejected ("invalid partner ip") and the link never
# starts. Resolve it here, once, and fail loudly instead.
sdk_host="${PX4_MAVLINK_TARGET_HOST:-127.0.0.1}"
if ! [[ "$sdk_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$sdk_host" == localhost ]]; then
  resolved="$(getent ahostsv4 "$sdk_host" | awk 'NR==1 {print $1}')"
  [ -n "$resolved" ] || fail "PX4_MAVLINK_TARGET_HOST=$sdk_host does not resolve to an IPv4 address"
  log "resolved $sdk_host to $resolved"
  sdk_host="$resolved"
fi
log "configuring MAVSDK link (target=$sdk_host:$MAVSDK_PORT_REMOTE)"
python3 /usr/local/bin/px4_mavlink_patch.py --source "$rc_mavlink" --target "$rc_mavlink" \
  --sdk-host "$sdk_host" || fail "could not configure the MAVSDK link in $rc_mavlink"

# ── supervision ──────────────────────────────────────────────────────────────
GZ_PID=""
PX4_PID=""

cleanup() {
  if [ -n "$PX4_PID" ]; then kill -TERM "$PX4_PID" 2>/dev/null || true; fi
  if [ -n "$GZ_PID" ]; then kill -TERM "$GZ_PID" 2>/dev/null || true; fi
  wait 2>/dev/null || true
}

# Split from cleanup so a signal actually terminates the script. A single handler
# on TERM would return to `wait` once it finished and the container would hang
# until Docker escalated to SIGKILL.
on_signal() {
  log "signal received; shutting down"
  cleanup
  exit 143
}

trap on_signal TERM INT
trap cleanup EXIT

log "gazebo: world=$WORLD_FILE"
cd "$ROOTFS_DIR"
# gz_env.sh is emitted by the PX4 build; source it when present, as the WSL path does.
if [ -f ./gz_env.sh ]; then
  # shellcheck disable=SC1091
  . ./gz_env.sh
fi

gz sim -r -s --headless-rendering "$WORLD_FILE" &
GZ_PID=$!

# Wait for the server to publish, rather than sleeping a guessed interval. PX4
# attaching to a server that is not up yet fails in ways that look like a model
# problem, which is expensive to debug.
log "waiting up to ${GZ_READY_TIMEOUT}s for the Gazebo server"
ready=0
for _ in $(seq 1 "$GZ_READY_TIMEOUT"); do
  if ! kill -0 "$GZ_PID" 2>/dev/null; then
    fail "Gazebo server exited during startup"
  fi
  # Collected into a variable rather than piped into grep: `grep -q` exits on the
  # first match, which SIGPIPEs `gz topic`, and under `pipefail` that failure
  # would mask a successful match and stall startup until the timeout.
  topics="$(gz topic -l 2>/dev/null || true)"
  if printf '%s\n' "$topics" | grep -q "^/world/"; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" = 1 ] || fail "Gazebo server did not publish a world topic within ${GZ_READY_TIMEOUT}s"
log "gazebo ready"

# SIM_MODE=gazebo: the platform owns this container for one run and starts PX4,
# the aircraft and the mission itself through docker exec
# (apps/api/services/docker_sim.py). Supervise Gazebo alone.
if [ "${SIM_MODE:-full}" = "gazebo" ]; then
  log "gazebo-only mode: PX4 is started by the platform"
  status=0
  wait "$GZ_PID" || status=$?
  log "gazebo exited (status=$status)"
  [ "$status" -ne 0 ] || status=1
  exit "$status"
fi

log "px4: model=$PX4_SIM_MODEL world=$PX4_GZ_WORLD mavlink=$MAVLINK_UDP_PORT mavsdk=$MAVSDK_PORT_REMOTE"
"$BUILD_DIR/bin/px4" -d "$PX4_DIR/ROMFS/px4fmu_common" -s etc/init.d-posix/rcS &
PX4_PID=$!

# Exit as soon as either half dies; a container running Gazebo with a dead PX4 is
# not a working simulator and should be restarted by the orchestrator.
# `|| status=$?` is load-bearing: under `set -e` a non-zero `wait -n` would exit
# immediately and the status would never be captured or logged.
status=0
wait -n "$GZ_PID" "$PX4_PID" || status=$?
log "a simulation process exited (status=$status)"
# Either half stopping on its own is a failure even when it exited 0: PX4 ends
# cleanly on SIGTERM, and a 0 here read as a deliberate stop to Docker and to
# anything watching the exit code. Deliberate stops leave through on_signal.
[ "$status" -ne 0 ] || status=1
exit "$status"

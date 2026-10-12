# Generated mission execution (F08)

Mission code comes from a language model. It runs only if it is the exact code a person approved, and only inside an unprivileged Linux sandbox with no network, no service secrets and no view of the host filesystem. The only thing it can reach is the simulated vehicle.

## Chain of custody

1. **Approval binds bytes.** `human_review_code` stores `approved_code_sha256` (SHA-256 of `generated_code` as approved). Every `generate_code` result clears it.
2. **Graph gate.** `run_simulation` refuses code with no recorded approval or a different fingerprint (`simulation_status=blocked`, straight to the report, no retry). The manual `POST /simulate/start` endpoint applies the same check against the graph state and returns 409 otherwise. It also flies the design's own aircraft identity, and now stops its simulator.
3. **Service gate.** `PX4Service.execute_mission(expected_code_sha256=...)` re-hashes the code before anything starts.
4. **Runner.** The runner receives the SHA-256 of the exact file it will load (`--mission-sha256`). The sandbox bootstrap checks it again inside the sandbox before importing the mission (exit code 3 on mismatch).

## The sandbox

`utils/mission_sandbox.py` runs from the trusted mission runner (WSL or native Linux) and builds it with util-linux only; `bubblewrap` is not required.

| Boundary | Mechanism |
| --- | --- |
| Namespaces | `unshare --user --map-root-user --mount --net --pid --ipc --uts --fork --kill-child` |
| Filesystem | tmpfs root, `pivot_root`, old root detached. Read-only: `/usr` (every submount), `/bin`/`/lib*` links, a few `/etc` files (loader cache, passwd, group, hosts, nsswitch, localtime), the Python environment, and the staged mission. Private 64 MB `/tmp`. No `/home`, `/mnt`, repository, storage or `.env`. |
| Network | A new network namespace with no interfaces up. The only channel is a Unix socket bind-mounted at `/run/mavsdk`, which the runner relays to `mavsdk_server` (and so to the simulated vehicle). |
| Privileges | `setpriv --no-new-privs`, empty bounding, inheritable and ambient capability sets |
| Environment | `env -i` with `PATH`, `HOME=/tmp`, `TMPDIR`, and `LANG`/`LC_ALL`/`TZ` when set |
| Limits | Address space 4 GiB, file size 64 MiB, 256 open files, 128 tasks, no core files, CPU time equal to the mission timeout plus 60 s. The runner also enforces wall-clock timeouts. |
| Lifetime | The sandbox dies with the runner (`PR_SET_PDEATHSIG` → `unshare --kill-child` → PID namespace init) |

The bootstrap (`utils/mission_sandbox_entry.py`) replaces `mavsdk.System` with a subclass pinned to the socket (`unix:/run/mavsdk/grpc:<port>`). Missions written as `run_mission(drone)` or `main()` both work unchanged.

**Telemetry integrity.** The runner collects telemetry through its own MAVSDK client, outside the sandbox. The mission's output reaches the runner's stderr prefixed with `[mission]`, never stdout, so it cannot inject live-telemetry lines. Previously the mission ran inside the runner process and could have altered the record the acceptance gate judges.

## Policy

- `mission_isolation_mode()` is `sandbox`. The runner fails closed when user namespaces or util-linux tools are missing.
- `DRONEPROMPT_ALLOW_UNSANDBOXED_MISSIONS=true` restores in-process execution for local development only, and is ignored when `ENVIRONMENT=production`. Such runs report `mission_isolation=none`, and the acceptance gate rejects them.
- The Docker runtime used to execute the code in the API process with the API's environment. It now runs the same runner and sandbox inside the run's own simulation container (see [Docker](#docker)).
- The native runner receives only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`, `TMPDIR` and `XDG_RUNTIME_DIR` from the API process.
- Acceptance requires `mission_isolation=linux_namespaces`.

## Verified

- `tests/test_mission_sandbox.py` (13 tests). On Linux it runs a hostile mission inside the real sandbox. The mission sees no secret environment variable or planted file. TCP to a local listener, Postgres and the internet, and UDP to PX4, all fail with "network unreachable". Writing outside `/tmp` fails, `mount` is denied, it sees only its own PID, and the relayed socket works. A tampered mission file exits with code 3.
- Real PX4 SITL flights of the generated Quad-X through the sandbox, from Windows (WSL runtime) and from inside WSL (native runtime), completed and passed acceptance with `mission_isolation=linux_namespaces`.

## Docker

Docker's default seccomp profile allows `unshare`, `mount` and `umount2` only to a container holding `CAP_SYS_ADMIN`, and `pivot_root` not at all, so the sandbox cannot start there. The simulation container therefore runs with its own profile instead of more privileges:

| File | Content |
| --- | --- |
| `infrastructure/seccomp/moby-default-v28.3.2.json` | Docker's default profile, unmodified (moby v28.3.2, `profiles/seccomp/default.json`) |
| `infrastructure/seccomp/make_profile.py` | Appends one rule allowing exactly those four calls |
| `infrastructure/seccomp/sim-sandbox.json` | The result, passed as `--security-opt seccomp=...` |

`tests/test_multiworker.py` rebuilds the profile and checks it byte for byte. It also checks that everything except that one rule equals the base. The container gets no `--privileged` and no added capability. Inside it:
- the mission cannot see the host's files;
- a mount of the host root is refused;
- the process keeps Docker's default capability set.

A generated quadcopter flew through this path with `mission_isolation=linux_namespaces` and passed acceptance ([FLIGHT_MODEL_SITL.md](FLIGHT_MODEL_SITL.md)).

## Limits

- Needs Linux with unprivileged user namespaces (WSL2 has them). Hosts that block them, for example Ubuntu 24.04's AppArmor restriction for arbitrary binaries, fail closed. So does a container started with Docker's default seccomp profile instead of `sim-sandbox.json`.
- Through `mavsdk_server`, a mission has the full MAVSDK API to the simulated vehicle: parameters, shell and FTP included. The boundary is the simulator, not individual commands. A restricted mission DSL would narrow this further.
- `mavsdk_server` listens on all interfaces for gRPC (upstream default). Restricting it to loopback is separate hardening.
- CPU and memory are bounded by rlimits, not cgroups, so a mission can still compete for CPU until its time limits expire.

# Workers, run ownership and the simulator (F09)

The API runs one worker by default. Several are supported only with shared state, and each shared resource has exactly one owner at a time.

## One run per project

`services/run_lock.py` gives a project a single owner across threads, workers and hosts.

- **Durable checkpoints (PostgreSQL):** a session advisory lock on a dedicated connection. It is released when that connection closes, including when its process is killed, so a crash never leaves a project locked.
- **In-memory checkpoints:** a process-local lock. That mode is single-process by definition.
- **Never waits:** a second request is refused, not queued, so a duplicated or redelivered approval cannot start a second run.

Starting a pipeline and resuming it after an approval both take the lock first, then re-check the review gate inside it. Previously the gate was checked before acting, with no lock, so two simultaneous approvals could both find it open and both resume, which meant two simulations for one approval. An interrupted run stays at its last checkpoint and is never re-executed automatically.

## Events reach every client

- **Thread-safe emission:** pipelines run on worker threads with their own event loops, while the Socket.IO server's connections live on the main loop. `broadcast_to_project` now hands events from other loops to that loop (`bind_server_loop` at startup). Before, it wrote to its transports from another thread.
- **Cross-worker delivery:** with `ELJAZARI_SOCKETIO_MESSAGE_QUEUE=redis://…` the server uses `socketio.AsyncRedisManager`. An event raised in any process reaches clients connected to any worker.

## Worker count

`ELJAZARI_API_WORKERS` (default 1) drives `Dockerfile.api`, `docker-compose.yml` (`API_WORKERS`) and `scripts/run_wsl_api.py --workers N`. Startup refuses more than one worker without the message queue or without PostgreSQL checkpoints. Engine.IO long-polling additionally needs sticky sessions. The web client tries WebSocket first, which does not.

## One simulator per host

PX4/Gazebo ports, the PX4 root filesystem and the WSL process cleanup are host-wide, and each launch kills leftover simulators. So a second concurrent run, from another project or worker, used to kill the first mid-flight.

`GazeboService.launch_world` now takes a host file lock before touching any process, and `stop_world` or any launch failure releases it. Later runs wait up to `ELJAZARI_SITL_LOCK_TIMEOUT_S` (default 120 s), then fail with a clear message. The OS releases the lock if the process dies.

## Verified

- `tests/test_multiworker.py` (7 tests):
  - Two simultaneous approvals resume the graph once.
  - A new run is refused while the project is busy.
  - Events emitted from a worker thread are delivered on the server loop.
  - A second service instance waits for the simulator, gets it after `stop_world`, and a failed launch does not keep it.
  - The worker/queue/checkpoint startup guard.
- `scripts/check_multiworker.py`, with real PostgreSQL and Redis in WSL:
  - Of two processes racing for one project, exactly one owns it.
  - A SIGKILLed owner's lock is released.
  - An event raised by a separate process reached a Socket.IO client connected to another process through Redis.
- The API started under `run_wsl_api.py --workers 2`: two worker processes, each with the Redis queue and PostgreSQL checkpoints, and `/health` answering.

## Limits

- No durable job queue: a run interrupted by a restart waits at its checkpoint until someone acts on it.
- The simulator lock is per host. Separate hosts must not share one SITL instance.
- The lock file lives in each OS's temp directory, so an API on Windows (WSL runtime) and another inside WSL (native runtime) do not see each other's lock. Run one of them per machine.

# Reproduction from a clean clone

## What this is, and what it is not

On 2026-09-30 the README quick start was followed from a fresh `git clone` of `origin/main` (`037744bb`), step by step and timed. The clone went into a new folder, with a new virtualenv, a fresh `npm install` and separate database containers.

This is a **proxy, not a second person**. The run was made by the same assistant, on the owner's machine: Windows 11, Python 3.13.12, Node 22.20.0, Docker Desktop 4.44.3. That machine already had:
- the Ollama models;
- the Hugging Face embedder in its cache;
- PX4 and Gazebo in WSL;
- PostgreSQL and Redis running in WSL.

So it catches what the **repository** lacks, not what a **different machine** lacks. The [form at the end](#form-for-a-second-person) is for the real test.

## Steps as written

| Step (README) | Result | Time | Cause |
| --- | --- | --- | --- |
| `git clone` | OK | 25 s | 275 MB pack, 90 MB working tree |
| `cp .env.example .env` | OK | — | |
| 2. `python -m scripts.bootstrap_aircraftverse` | **Failed:** "dataset missing" | 2 s | `.env.example` sets `AIRCRAFTVERSE_PATH=./data/...`, and it was resolved from `apps/api`. The API then reported `"dataset": "fallback"` and would have designed without retrieval, silently |
| 3. `python -m scripts.bootstrap_ollama` | OK | 5 s | Models already present. On a clean machine: about 10 GB |
| 4. `docker compose … up -d postgres redis ollama` | **Not runnable as written** | 4 s once changed | This machine already serves 5432 and 6379 from WSL. The `ollama` service also takes 11434, which step 3's `ollama serve` already holds. Run with a separate project name and ports 55432 and 56379 |
| 5. `pip install -r requirements.txt` | **Failed** after 391 s | | `asyncpg==0.29.0`, and `chroma-hnswlib==0.7.6` (from `chromadb==0.6.3`), have no Python 3.13 wheel. Building them needs Microsoft C++ Build Tools |
| … with `asyncpg 0.31.0`, `chromadb 1.5.8` | OK | 833 s | virtualenv 1.7 GB. These are the versions the benchmark host runs |
| `alembic upgrade head` | **Failed** | 3 s | `greenlet` missing. SQLAlchemy 2.0.35 declares it only for Python < 3.13 |
| … after `pip install greenlet` | OK; `python -m scripts.seed` OK | 2 s + 2 s | |
| `uvicorn main:socket_app --port 8000` | OK: `/health` 200 with `"dataset": "real"` (after the step 2 workaround), `/docs` 200 | 5 s | |
| 6. `npm install` (in `apps/web`) | OK | 345 s | 641 MB. npm reports 21 advisories (1 critical, 14 high), including Next.js 14.2.14. It also rewrote two metadata lines in `package-lock.json` |
| `npm run test` | 36 of 36 | 99 s | |
| `npm run dev`, opened in a browser | Page loads; it obtains a token from the API | | |
| Testing: `pytest` | **1297 passed, 2 failed**, 1 skipped | 298 s | Both tests read the developer's `.env`. The one copied from `.env.example` turns on the dev token endpoint, which production refuses. Another test wrote deliverables into the real storage folder |
| `python scripts/doctor.py` | All checks OK | 9 s | Only because this machine has PX4 and Gazebo in WSL |
| `python cli.py --prompt "A small quadcopter carrying a 150 g camera. Take off, hover at 10 m for 20 seconds, then land." --auto-approve` | **Run FAILED** at simulation: "Spawned model links [] are not the generated Quad-X" | 650 s | See below |

### Why the clean clone did not fly

`.env.example` set `STORAGE_PATH=/data/storage`:
- On Windows that means `C:\data\storage`. The generated model was written there, and the path went to WSL as `/data/storage`, where nothing exists. Gazebo spawned nothing, attestation found no links, and the gate refused the run, as it should.
- The owner's checkout sets a full Windows path, which is why it flies.
- On Linux, a normal user cannot create `/data` at all.

The CLI also set its model names as environment variables, and those outrank `.env`. It asked for `qwen2.5:7b` and `llama3.2:3b`, which the bootstrap in step 3 never pulls. It worked here only because this machine happens to have both.

## Fixed after this run

| Finding | Change |
| --- | --- |
| Relative paths in `.env` resolved from wherever the process started | `AIRCRAFTVERSE_PATH`, `AIRCRAFTVERSE_CHROMA_PATH` and `STORAGE_PATH` are taken from the repository root, where `.env` lives |
| `/data/storage` default | Default and example are now the repository's `data/storage`, already git-ignored. The containers set their own absolute path, unchanged |
| No Python 3.13 wheels | `asyncpg 0.31.0`, `chromadb 1.5.8`, `sqlalchemy[asyncio]` (brings `greenlet`) |
| Tests depending on the developer's `.env` and storage folder | Settings tests ignore `.env`; every test gets a private storage folder |
| CLI overriding `.env` | It sets only its in-memory database URL and its default secret |
| CLI printed "Evaluation Score: 0/100. Passed: None" for every run | It read keys the evaluator does not return. It now prints `simulation_score` and `simulation_success` |
| README | Python 3.11–3.13; download sizes; `ollama` out of the compose command; the port clash; first-design index time; `/health` must say `real`; where simulation comes from |

**After the fixes**, the same clone with the changes applied and a new `.env` copied from the new `.env.example` (only the two ports changed):
- `pip install -r requirements.txt`: already satisfied; the new pins match what installed.
- Step 2 passes as written. Storage resolves to the clone's `data/storage`.
- The same CLI prompt flew in real WSL SITL: 548 telemetry samples, mission completed. The gate passed it with score 85, in 503 s end to end. The CLI now uses the models from `.env`.
- `pytest`: 1305 passed, 1 skipped, none failed (295 s). This is with the `.env` copied from the example, which failed two tests before.

## Still open

- **Environment drift.** The benchmark host's Python differs from the pinned versions in 26 packages. Examples: FastAPI 0.115.0 against 0.139.0, Starlette 0.38.6 against 1.3.1, LangGraph 1.1.8 against 1.2.7. The clean clone ran the pinned set, and its unit tests pass, but the benchmark and SITL evidence came from the host set. A lock file, or a benchmark run on the pinned set, would close this.
- **API/Celery container paths — fixed in the 2026-09-30 review.** Shared path resolution now supports both `apps/api` and the flattened `/app` layout, including safety rules, the component catalog and the simulation service. A regression test imports the API and worker from a copied container layout. A fresh Docker image startup remains unverified.
- **Starter prompts** in the web app include a fixed-wing VTOL and an 8 kg lift, which the system refuses by design.
- **CLI output.** `pipeline_results.json` leaves out the acceptance record, so the flight's path, altitude and hold figures are not in the CLI's files. The gate cannot pass without them (a test holds it to that), but a reader cannot see them.
- **CLI database URL.** The CLI's `sqlite+aiosqlite` URL needs `aiosqlite`, which is not a requirement, so the CLI logs `persist_skipped` and keeps nothing. Harmless for a one-off run.
- **npm advisories**, including the Next.js 14.2.14 security update.

## Form for a second person

Please follow the [setup guide](SETUP.md) on your own machine, without help from the maintainers, and send back this form.

```text
Machine / OS:                      CPU / RAM / GPU:
Python version:                    Node / npm version:
Docker version:                    Ollama version:
Already installed before starting (PX4? Gazebo? PostgreSQL? models?):

Step                                  Pass/Fail   Minutes   Notes (exact error text)
git clone
cp .env.example .env
2 bootstrap_aircraftverse
3 bootstrap_ollama --pull-missing
4 docker compose up -d postgres redis
5 pip install -r requirements.txt
5 alembic upgrade head
5 python -m scripts.seed
5 uvicorn; GET /health says "dataset": "real"
6 npm install; npm run dev; page opens
pytest (apps/api)                     passed / failed / skipped:
npm run test (apps/web)               passed / failed:
One prompt in the web app: what did the run end with?

Anything you had to do that the guide did not say:
```

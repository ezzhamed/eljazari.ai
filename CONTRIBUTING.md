# Contributing to eljazari.ai

Thank you for your interest in contributing to **eljazari.ai**, a reference implementation that turns a drone request into a design, mission code and a checked PX4 SITL flight. Bug fixes, tests, documentation and new features are all welcome. The project is alpha: a change that makes a claim about flight behaviour should come with the evidence for it, and simulation results must not be described as hardware validation.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Pull Request Process](#pull-request-process)
- [Reporting Issues](#reporting-issues)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior to the maintainers.

---

## Getting Started

1. **Fork** the repository on GitHub.
2. **Clone** your fork locally:
   ```bash
   git clone https://github.com/<your-username>/eljazari.ai.git
   cd eljazari.ai
   ```
3. **Create a branch** for your feature or fix:
   ```bash
   git checkout -b feat/my-feature
   # or
   git checkout -b fix/my-bugfix
   ```

---

## Development Setup

### Prerequisites

- **Node.js** 22.13+ (or Node 24), **npm** 10+
- **Python** 3.11–3.13 (see [docs/SETUP.md](docs/SETUP.md#requirements))
- **PostgreSQL** 16+
- **Redis** 7+
- **Ollama** (for local LLM inference)
- **Docker + Docker Compose v2+** (optional, for full stack)

### Backend (FastAPI)

```bash
cd apps/api
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m scripts.bootstrap_components  # optional; sourced-model tests need it

# Set up the database
cp ../../.env.example ../../.env  # update with your values
alembic upgrade head

# Run tests
pytest -v

# Start the dev server
uvicorn main:socket_app --reload --port 8000
```

### Frontend (Next.js)

```bash
# From the repository root
npm ci --legacy-peer-deps
npm run dev -w @eljazari/web
```

### Full Stack (Docker)

```bash
docker compose -f infrastructure/docker-compose.dev.yml up -d
```

---

## Project Structure

```
eljazari.ai/
├── apps/
│   ├── api/          # FastAPI backend (agents, graph, services, tests)
│   └── web/          # Next.js 15 frontend (dashboard, 3D viewer, GCS)
├── packages/
│   ├── shared-types/ # Shared TypeScript types
│   ├── gsce-framework/
│   └── urdf-converter/
├── infrastructure/   # Docker, K8s, monitoring, nginx
├── simulation/       # Gazebo + PX4 configs
└── data/             # Datasets, safety rules, meshes
```

---

## Coding Standards

### Python (Backend)

- **Formatter:** [Ruff](https://docs.astral.sh/ruff/) — `ruff format .`
- **Linter:** [Ruff](https://docs.astral.sh/ruff/) — `ruff check .`
- **Type Checker:** [MyPy](https://mypy-lang.org/) — `mypy . --ignore-missing-imports`
- **Test Framework:** [pytest](https://docs.pytest.org/) with `pytest-asyncio`
- **Style:**
  - Use `async def` for all database and LLM operations
  - Use Pydantic v2 for all data models
  - Keep agents stateless — shared state flows through `ElJazariState`
  - Every safety rule must have a test case

### TypeScript (Frontend)

- **Linter:** ESLint — `npm run lint`
- **Type Check:** `npx tsc --noEmit`
- **Style:**
  - Use TypeScript strict mode
  - Use Zustand + Immer for state management
  - Use TanStack Query for server state
  - Keep components focused and reusable
  - All telemetry data must pass through `normalizeTelemetryPoint()`

### Git Conventions

- **Branch names:** `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`
- **Commit messages:** Follow [Conventional Commits](https://www.conventionalcommits.org/):
  ```
  feat: add battery thermal analysis to monitor agent
  fix: prevent null telemetry crash in simulation page
  docs: update API reference for new endpoints
  chore: update dependencies
  ```

---

## Pull Request Process

1. **Ensure all tests pass** locally:
   ```bash
   # Backend
   cd apps/api && pytest -v

   # Frontend
   cd apps/web && npm run lint && npx tsc --noEmit && npm run build
   ```

2. **Write tests** for new features or bug fixes.

3. **Update documentation** if your changes affect:
   - API endpoints or WebSocket events
   - Environment variables
   - Agent behavior or pipeline flow

4. **Open a Pull Request** against the `main` branch:
   - Provide a clear title and description
   - Reference any related issues
   - Include screenshots for UI changes

5. **CI must pass** — the PR will be reviewed once all checks are green.

6. **One approval** is required before merging.

---

## Reporting Issues

When reporting bugs, please include:

- **OS and environment** (Windows/Linux/macOS, Node version, Python version)
- **Steps to reproduce** the issue
- **Expected vs. actual behavior**
- **Logs** (API console output, browser console errors)
- **Screenshots** if the issue is UI-related

Use the [GitHub Issues](https://github.com/ezzhamed/eljazari.ai/issues) page and apply the appropriate label:
- `bug` — Something is broken
- `feature` — New functionality request
- `docs` — Documentation improvement
- `question` — General questions

---

## License

By contributing to eljazari.ai, you agree that your contributions will be licensed under the [MIT License](LICENSE).

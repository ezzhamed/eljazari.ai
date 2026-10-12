# Release gate

A public release is built from a scanned source snapshot that contains no credentials, local environment files or locally imported manufacturer data.

## Clean source

From the repository root:

```bash
python tools/export_public.py --output ../eljazari-public
```

The exporter includes tracked source and non-ignored new source files, excludes local data, secrets and maintainer-only files, and writes a SHA-256 manifest. The destination must be new. It does not change the source repository or publish anything. Scan the exported files for secrets before committing them.

## Required validation

- Unit tests, type checks, lint and production builds must pass.
- Secret and dependency scans must pass. Dependency audit failures are blocking.
- Flight checks must use the generated aircraft, matching local propulsion evidence, the mission sandbox and deterministic acceptance gate.
- GitHub CI, security and SITL workflows must all succeed on the exact release commit before `release.yml` creates a draft.
- A self-hosted Linux runner labelled `px4-sitl` is required by `sitl.yml`. It must have PX4/Gazebo, its Python environment, PostgreSQL and Redis configured; public pull requests must never run untrusted jobs on this runner.

## Data and version

Manufacturer data is imported privately using [Local component data](LOCAL_COMPONENT_DATA.md). It is excluded from source exports and Docker image build contexts. A clean clone needs that import for sourced-model tests and accepted simulation flights.

The API and packages report `0.1.0-alpha.1`. Create a release tag only after the final candidate checks pass. Historical reports are not proof that a later commit passes.

# Local manufacturer component data

The public repository does not distribute T-MOTOR test extracts, sourced motor/propeller records or their derived propulsion tables. The publisher metadata in the historical extracts did not grant redistribution permission. Code remains MIT; AircraftVerse data retains its separate CC-BY-4.0 attribution.

The shipped component catalog contains unsourced reference records. They can support interface and design exploration, but the acceptance gate must refuse flight validation without matching propulsion evidence. No synthetic data is represented as manufacturer evidence.

## Import for local use

After installing backend requirements, from `apps/api`:

```bash
python -m scripts.bootstrap_components
```

The importer downloads the source product pages and derives a catalog under `.local/components` at the repository root. This directory is ignored by Git, excluded from Docker build contexts and excluded from public exports. Review the source publisher's terms for your intended use; importing does not grant redistribution rights.

The API prefers this local catalog when present. To choose another private directory:

```bash
python -m scripts.bootstrap_components --output-dir /absolute/private/catalog
# Set this in the backend environment:
COMPONENTS_CATALOG_PATH=/absolute/private/catalog
```

Use `--refresh` to re-fetch current pages. Changed publisher data can change the resulting fingerprints or make a pairing invalid. The importer rejects the tracked `data/components` directory as a destination.

## Tests, containers and CI

Tests that verify sourced flight dynamics require the local import. CI imports the pages before running those checks; failure to import fails the job rather than silently skipping engineering validation. Network access to the publisher is therefore a test prerequisite.

For a container, mount your locally imported directory read-only and set `COMPONENTS_CATALOG_PATH` to the mount location. Do not bake the directory into a public image. For example, mount `.local/components` as `/local-components` and use `COMPONENTS_CATALOG_PATH=/local-components`.

Historical benchmarks describe the data and environment used at their recorded dates. Their source fingerprints are not a promise that freshly downloaded pages are unchanged.

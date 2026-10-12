# AircraftVerse Dataset

`designs.json` holds the design corpus that backs the Design agent's RAG
retrieval. It is a **derived repackaging of the real AircraftVerse dataset**
(NeurIPS 2023) — see [DATASET_LICENSE.md](DATASET_LICENSE.md) for attribution,
the CC-BY-4.0 terms, and the full list of changes made to the source data.

`corpus_report.json`, written alongside it, records exactly what the packaged
corpus contains (design counts, distinct components, topology breakdown).

## Building the corpus

```bash
cd apps/api

# Full corpus — downloads all three Zenodo parts (~12.3 GB, deleted after use)
python -m scripts.download_aircraftverse --parts 1,2,3 --cleanup

# Part 1 only — ~9,000 designs, ~4.1 GB download
python -m scripts.download_aircraftverse --parts 1 --cleanup

# Repackage from zips you already have, without re-downloading
python -m scripts.download_aircraftverse --zip-dir data/aircraftverse/temp
```

The downloader also fetches `corpus_dic`, the AircraftVerse component
catalogue. It is required: without it, motor/propeller/battery identifiers
cannot be resolved to real KV, capacity, and geometry values.

## Validating what you have

```bash
cd apps/api
python -m scripts.bootstrap_aircraftverse
```

This checks structure **and diversity**. A file can be large and well-formed and
still be worthless for retrieval — an earlier packaging bug produced 27,714
entries that were identical apart from their performance block, and every
structural check passed. The validator now fails when a corpus of more than 50
designs contains fewer than two distinct motors.

## Record shape

```json
{
  "design_id": "design_1495",
  "topology": "tricopter",
  "airframe_class": "multirotor",
  "use_case": "industrial inspection",
  "frame": { "type": "X", "arm_length_mm": 436, "motor_count": 3, "has_wing": false },
  "battery": { "cells": 6, "capacity_mah": 6000, "weight_g": 1140,
               "part": "TurnigyGraphene6000mAh6S75C", "voltage_v": 22.2 },
  "propulsion": [
    { "motor": "kde_direct_KDE5215XF_435", "propeller": "apc_propellers_15x8E",
      "position": "front_left", "kv": 435.0, "max_power_w": 1595.0,
      "prop_diameter_mm": 381.0 }
  ],
  "electronics": { "flight_controller": "PX4", "gps": true, "compass": true },
  "payload": { "max_weight_g": 1693, "mount_type": "bottom" },
  "mass_kg": 6.7737,
  "evaluated": true,
  "performance_simulated": { "Max_Distance": 12000.0, "Hover_Time": 900.0, "...": 0 }
}
```

### `airframe_class` and `evaluated`

The real corpus is more varied than eljazari.ai's URDF pipeline can build:
motor counts run from 1 to 16, roughly a quarter of designs are fixed-wing or
VTOL hybrids, and about two thirds failed physics evaluation at the source and
carry zeroed metrics. 7,815 of the 27,714 designs (28%) survive both filters.
Rather than coercing the rest into a supported topology — which is what
silently destroyed the previous corpus — they are labelled:

| `airframe_class` | Meaning |
|---|---|
| `multirotor` | 2/3/4/6/8 motors, no wing — buildable by the URDF pipeline |
| `multirotor_unsupported` | A real multirotor whose motor count has no URDF mapping |
| `fixed_wing_hybrid` | Carries a wing; `topology` is `fixed_wing_<n>_motor` |

`evaluated` is `true` only when the source physics run produced a non-zero
`Max_Distance`.

RAG retrieval filters to `airframe_class == "multirotor"` **and**
`evaluated == true` by default, and that baseline survives the fallback path
that relaxes use-case/topology filters. Pass `buildable_only=False` to
`retrieve()` / `retrieve_filtered()` to search the whole corpus.

Corpora predating these fields carry no such metadata; the filter detects that
and disables itself rather than matching nothing.

## Configuration

- `AIRCRAFTVERSE_PATH` — path to `designs.json`
- `AIRCRAFTVERSE_REQUIRE_REAL=true` — fail startup when the file is missing
- `AIRCRAFTVERSE_RAG_MODE=memory|persistent`

> `AIRCRAFTVERSE_REQUIRE_REAL` is an **existence and validity** check, not a
> provenance check. It cannot tell a real corpus from a synthetic one that
> happens to be well-formed and diverse. Verify provenance yourself via
> `corpus_report.json`.

## ChromaDB index

With `AIRCRAFTVERSE_RAG_MODE=persistent`, the index lives in
`chroma_db/<corpus stamp>/` next to the dataset.
- The stamp covers the dataset file's size and modification time, the index cap and the embedding model. A changed corpus therefore gets a fresh directory, and an unchanged one is reused across restarts.
- The downloader clears `chroma_db/` after repackaging.
- Directories for old corpus stamps can be deleted by hand.

In memory mode the index is built once per process.

**Before 2026-09-29, every design appended the whole corpus again.** It went into one fixed-name collection, in memory and on disk alike. Persisted stores reached 2.6 GB and 6 GB, and a benchmark query on one of them took 17.5 hours. Those old stores are the loose files directly in `chroma_db/` (`chroma.sqlite3` and one directory per old collection). The current code never reads them, and they can be deleted.

Only the first `RAG_INDEX_LIMIT` (5,000) designs are embedded. When the corpus
carries provenance fields, buildable evaluated designs are selected first, so
the cap does not fill the index with unbuildable airframes.

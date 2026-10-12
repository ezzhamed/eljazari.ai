# Dataset License & Attribution

> **The data in this directory is _not_ covered by the repository's MIT licence.**
> The source code that downloads and repackages it is MIT; the design data
> itself is CC-BY-4.0 and carries the attribution requirements below.

## Source

**AircraftVerse: A Large-Scale Multimodal Dataset of Aerial Vehicle Designs**

- DOI: [10.5281/zenodo.6525446](https://doi.org/10.5281/zenodo.6525446)
- Version 1.0.0, published 2023-06-08
- Licence: [Creative Commons Attribution 4.0 International (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/)
- Published at NeurIPS 2023 (Datasets and Benchmarks Track)

## Authors

Adam D. Cobb, Anirban Roy, Daniel Elenius, F. Michael Heim, Brian Swenson,
Sydney Whittington, James D. Walker, Theodore Bapty, Joseph Hite,
Karthik Ramani, Christopher McComb, Susmit Jha.

SRI International · Southwest Research Institute · Vanderbilt University ·
Purdue University · Carnegie Mellon University.

## Citation

```bibtex
@inproceedings{cobb2023aircraftverse,
  title     = {AircraftVerse: A Large-Scale Multimodal Dataset of Aerial Vehicle Designs},
  author    = {Cobb, Adam D. and Roy, Anirban and Elenius, Daniel and
               Heim, F. Michael and Swenson, Brian and Whittington, Sydney and
               Walker, James D. and Bapty, Theodore and Hite, Joseph and
               Ramani, Karthik and McComb, Christopher and Jha, Susmit},
  booktitle = {Advances in Neural Information Processing Systems (NeurIPS)
               Datasets and Benchmarks Track},
  year      = {2023},
  doi       = {10.5281/zenodo.6525446}
}
```

## Changes made to the source data

CC-BY-4.0 requires that modifications be indicated. `designs.json` in this
directory is **a derived, repackaged subset** of AircraftVerse, not the original
distribution. Specifically:

1. Each design directory (`design_tree.json`, `design_low_level.json`,
   `output.json`) is condensed into a single flat JSON record.
2. Motor, propeller, and battery identifiers are resolved against the
   AircraftVerse `corpus_dic` catalogue, so records carry KV, capacity, voltage,
   weight, and propeller geometry inline.
3. Component counts are taken from the symmetry-expanded low-level
   representation rather than the folded design tree.
4. A `use_case` label is **derived** — AircraftVerse ships no such annotation.
   It is computed from the physics-evaluated metrics (mass, max speed, hover
   time, range) and resolved geometry. Treat it as a retrieval aid, not as
   source data.
5. `airframe_class` and `evaluated` flags are added to mark, respectively,
   whether a design is a buildable multirotor and whether its source physics run
   produced non-zero metrics.
6. CAD geometry (`Geom.stp`, `cadfile.stl`), point clouds (`pointCloud.npy`),
   and trim-state arrays (`trims.npy`) are **not** included.

The original dataset is unmodified at the source and remains available at the
DOI above. Regenerate this file at any time with:

```bash
cd apps/api
python -m scripts.download_aircraftverse --parts 1,2,3 --cleanup
```

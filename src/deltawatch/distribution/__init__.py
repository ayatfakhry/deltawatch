"""Distribution layer: packages frozen DeltaWatch outputs as COGs (and later a static STAC catalog).

Kept separate from the pipeline on purpose: nothing here is imported by `deltawatch.pipeline`,
and nothing here writes into `examples/`, `reference/` or a pipeline run directory.
"""

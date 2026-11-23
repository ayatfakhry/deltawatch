"""Explicit exception types so failures say what went wrong, not just KeyError."""


class DeltaWatchError(Exception):
    """Base class for all pipeline errors."""


class ConfigError(DeltaWatchError):
    """Missing or invalid configuration key."""


class AssetError(DeltaWatchError):
    """A required STAC asset is missing, non-public or requester-pays."""


class ScaleOffsetError(DeltaWatchError):
    """STAC and raster scale/offset disagree, or neither provides one."""


class GridError(DeltaWatchError):
    """CRS, transform, shape or coverage does not match the reference grid."""


class ManifestError(DeltaWatchError):
    """Manifest was edited after freezing or is inconsistent."""


class SelectionError(DeltaWatchError):
    """Not enough acquisition dates pass the selection policy."""

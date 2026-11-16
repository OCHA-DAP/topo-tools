"""Non-user-configurable constants for the clean pipeline."""

# gap_maximum_width for --maximum-gap-width=all: larger than any real
# geographic extent, so every detected gap qualifies without scanning.
GAP_MAXIMUM_WIDTH_ALL_DEG = 360.0

# Baseline area-loss tolerance with no detected overlaps to explain any loss:
# double the ~1% per-fid renoding drift confirmed on real defect-dense data.
AREA_NOISE_FACTOR = 0.02

# Multiplier on detected overlap area for how far ST_CoverageClean may
# legitimately redraw beyond the overlap's own footprint while resolving it.
OVERLAP_LOSS_HEADROOM = 3.0

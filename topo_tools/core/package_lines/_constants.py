"""Pure literals for the package-lines pipeline."""

# Grid cell size, in degrees, that boundaries are clipped to before noding.
TILE_DEG = 1.0
# Tiles overlap by this much so a line cut at a tile edge still meets its node.
TILE_MARGIN_DEG = 0.00001

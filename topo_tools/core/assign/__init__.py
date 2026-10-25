"""Shared input-to-overlay assignment helpers (edge tools, schema-join, code-update)."""

from ._column_selection import (
    resolve_column_selection,
    resolve_merge_columns,
    validate_merge_flags,
)
from ._gap_fill import fill_unmatched_overlays
from ._inputs import load_input, load_original, load_overlay
from ._many import assign_many
from ._one import (
    assign_one,
    bboxes_overlap,
    input_bbox_extent,
    load_nearby_overlay,
    overlay_fid_bboxes,
    prepare_overlay_tiles,
    union_bbox,
)

__all__ = [
    "assign_many",
    "assign_one",
    "bboxes_overlap",
    "fill_unmatched_overlays",
    "input_bbox_extent",
    "load_input",
    "load_nearby_overlay",
    "load_original",
    "load_overlay",
    "overlay_fid_bboxes",
    "prepare_overlay_tiles",
    "resolve_column_selection",
    "resolve_merge_columns",
    "union_bbox",
    "validate_merge_flags",
]

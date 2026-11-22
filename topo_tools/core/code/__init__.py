"""Shared hierarchical-code primitive: format, detection, cascade, rewrite."""

from topo_tools.core.code._cascade import assign_new_codes
from topo_tools.core.code._code_format import (
    CodeFormat,
    MinWidth,
    parse_min_width,
    resolve_code_format,
)
from topo_tools.core.code._codes import (
    build_code,
    last_component,
    parent_prefix,
    parse_code,
)
from topo_tools.core.code._columns import write_root_code
from topo_tools.core.code._detect_format import (
    detect_code_format,
    detect_undelimited_format,
    has_delimiter,
)
from topo_tools.core.code._next_available import next_available_integer
from topo_tools.core.code._rewrite import rewrite_child_code
from topo_tools.core.code._seed import check_unique_names, seed_code_from_names
from topo_tools.core.constants import TABLE_COPY_OPTS

__all__ = [
    "TABLE_COPY_OPTS",
    "CodeFormat",
    "MinWidth",
    "assign_new_codes",
    "build_code",
    "check_unique_names",
    "detect_code_format",
    "detect_undelimited_format",
    "has_delimiter",
    "last_component",
    "next_available_integer",
    "parent_prefix",
    "parse_code",
    "parse_min_width",
    "resolve_code_format",
    "rewrite_child_code",
    "seed_code_from_names",
    "write_root_code",
]

"""A hierarchical code's own delimiter/root/zero-pad configuration."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

MinWidth = int | tuple[int, ...] | Literal["auto"]


@dataclass(frozen=True)
class CodeFormat:
    """One target format for a dot-style hierarchical code. No default values."""

    root_code: str | None
    delimiter: str
    min_width: MinWidth

    def width(self, level: int) -> int | None:
        """Return level's zero-pad floor (levels count from 1), None under auto."""
        if self.min_width == "auto":
            return None
        if isinstance(self.min_width, tuple):
            return self.min_width[level - 1]
        return self.min_width

    def check_level_count(self, count: int) -> None:
        """Raise unless a per-level min_width has exactly one width per level."""
        if isinstance(self.min_width, tuple) and len(self.min_width) != count:
            msg = (
                f"min_width lists {len(self.min_width)} widths "
                f"{self.min_width} but {count} level(s) are numbered"
            )
            raise ValueError(msg)


def parse_min_width(value: int | str | Sequence[int]) -> MinWidth:
    """Parse a width (3), a coarsest-first list ('2,2,4'), or 'auto'."""
    if isinstance(value, str):
        if value.strip().lower() == "auto":
            return "auto"
        try:
            widths = tuple(int(part) for part in value.split(","))
        except ValueError:
            msg = f"min_width must be a number, a comma list, or auto, got {value!r}"
            raise ValueError(msg) from None
    elif isinstance(value, int):
        widths = (value,)
    else:
        widths = tuple(value)
    if not widths or any(w < 1 for w in widths):
        msg = f"min_width must be positive, got {value!r}"
        raise ValueError(msg)
    return widths[0] if len(widths) == 1 else widths


def resolve_code_format(
    root_code: str | None,
    delimiter: str,
    min_width: int | str | Sequence[int],
    *,
    allow_empty_delimiter: bool = False,
) -> CodeFormat:
    """Validate root_code (None: one root per row), delimiter and min_width."""
    if root_code is not None and not root_code:
        msg = "root_code must be a non-empty string"
        raise ValueError(msg)
    if not (allow_empty_delimiter and delimiter == "") and len(delimiter) != 1:
        msg = f"delimiter must be a single character, got {delimiter!r}"
        raise ValueError(msg)
    return CodeFormat(
        root_code=root_code,
        delimiter=delimiter,
        min_width=parse_min_width(min_width),
    )

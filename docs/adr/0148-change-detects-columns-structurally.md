# 0148: change detects its code and name columns structurally

## Status

Accepted.

## Context

`change` found each side's code and name columns with regex patterns
ported from cod-site's `columns.ts`, first match in column order, and
only when `--link-by-code`/`--link-by-name` was set. A run without a link
flag wrote empty `code_*`/`name_*` columns with no warning, even on a
standard COD-AB layer, and the first match could be a coarser level's
code (`adm0_pcode`, `iso3`) depending on column order.

## Decision

Take each side's code and primary name column from the finest level that
`core.schema_map`'s `resolve_levels` finds, the level detection
`code-detect`, `name-detect` and `schema-detect` use, with
`--name-field-*`/`--code-field-*` templates as `code-update` takes them.
Detection runs whether or not linking is on; an explicit
`--code-column-*`/`--name-column-*` overrides it.

## Consequences

- The changelog's `code_*`/`name_*` columns fill on any layer whose
  levels can be detected, and a side with none logs a warning, or raises
  under `--link-by-*`.
- A layer with codes but no names, or a single admin0 unit, has no
  detectable level and needs explicit columns.
- cod-site's Change page keeps the regex detector until it is ported.

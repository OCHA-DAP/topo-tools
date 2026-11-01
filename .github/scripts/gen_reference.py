"""Generate docs/pages/<step>/reference/<command>.md from the CLI's --help text.

Run `uv run python .github/scripts/gen_reference.py` to write the pages, or add
`--check` to exit non-zero when a page differs from the CLI.
"""

import inspect
import json
import re
import sys
import textwrap
from pathlib import Path

import click

from topo_tools.cli.main import cli

ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "docs" / "pages"

# command -> (docs step directory, sidebar order within that step)
LAYOUT = {
    "schema-detect": ("1-schema", 3),
    "schema-map": ("1-schema", 4),
    "schema-join": ("1-schema", 5),
    "schema-fill": ("1-schema", 6),
    "topo-detect": ("2-topology", 3),
    "topo-clean": ("2-topology", 4),
    "edge-extend": ("3-edge", 3),
    "edge-match": ("3-edge", 4),
    "edge-mosaic": ("3-edge", 5),
    "code-detect": ("4-codes", 3),
    "code-create": ("4-codes", 4),
    "code-update": ("4-codes", 5),
    "change": ("4-codes", 6),
    "name-detect": ("5-names", 3),
    "name-clean": ("5-names", 4),
    "package": ("6-packaging", 3),
    "package-polygons": ("6-packaging", 4),
    "package-points": ("6-packaging", 5),
    "package-lines": ("6-packaging", 6),
    "validate": ("6-packaging", 7),
}


def split_help(text: str) -> tuple[str, list[str], list[list[str]]]:
    """Split a command docstring into summary, description paragraphs, examples."""
    text = inspect.cleandoc(text).replace("\b\n", "").replace("\b", "")
    body, _, examples_text = text.partition("Examples:")
    paragraphs = [" ".join(p.split()) for p in body.split("\n\n") if p.strip()]
    examples = []
    for block in re.split(r"\n\s*\n", textwrap.dedent(examples_text).strip()):
        lines = [line for line in textwrap.dedent(block).splitlines() if line.strip()]
        if lines:
            examples.append(lines)
    return paragraphs[0], paragraphs[1:], examples


def code_flags(text: str) -> str:
    """Wrap --flags in prose as inline code, so typography doesn't turn -- into a dash."""
    return re.sub(r"(?<![\w`-])(--[a-z][\w-]*(?:=[^\s,;)]*[^\s,;).])?)", r"`\1`", text)


def synopsis(cmd: click.Command, ctx: click.Context) -> str:
    """Render the command's usage line, unwrapped."""
    return " ".join([ctx.command_path, *cmd.collect_usage_pieces(ctx)])


def option_lines(cmd: click.Command, ctx: click.Context) -> list[str]:
    """One markdown bullet per option: its flags and help text."""
    lines = []
    for param in cmd.params:
        record = param.get_help_record(ctx) if isinstance(param, click.Option) else None
        if record:
            names, text = record
            text = " ".join(text.split())
            lines.append(f"- `{names}`: {code_flags(text)}" if text else f"- `{names}`")
    return lines


def render(name: str, cmd: click.Command, order: int) -> str:
    """Build one reference page."""
    ctx = click.Context(cmd, info_name=f"topo-tools {name}")
    summary, description, examples = split_help(cmd.help or "")
    lines = [
        "---",
        f'title: "{name}"',
        f"description: {json.dumps(summary)}",
        "sidebar:",
        f"  order: {order}",
        "---",
        "",
        code_flags(summary),
        "",
        "## Synopsis",
        "",
        "```text",
        synopsis(cmd, ctx),
        "```",
        "",
    ]
    if description:
        lines += ["## Description", ""]
        for paragraph in description:
            lines += [code_flags(paragraph), ""]
    lines += ["## Options", "", *option_lines(cmd, ctx), ""]
    if examples:
        lines += ["## Examples", ""]
        for example in examples:
            comments = [line.lstrip("# ") for line in example if line.startswith("#")]
            commands = [line for line in example if not line.startswith("#")]
            if comments:
                lines += [code_flags(" ".join(comments)) + ":", ""]
            lines += ["```sh", *commands, "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    """Write every reference page, or report stale ones with --check."""
    check = "--check" in sys.argv[1:]
    missing = sorted(set(cli.commands) ^ set(LAYOUT))
    if missing:
        print(f"LAYOUT and CLI commands differ: {', '.join(missing)}")
        return 1
    stale = []
    for name, cmd in sorted(cli.commands.items()):
        step, order = LAYOUT[name]
        path = PAGES / step / "reference" / (name.replace("-", "_") + ".md")
        page = render(name, cmd, order)
        if path.exists() and path.read_text(encoding="utf-8") == page:
            continue
        stale.append(path.relative_to(ROOT))
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(page, encoding="utf-8")
    for path in stale:
        print(f"{'stale' if check else 'wrote'}: {path}")
    if check and stale:
        print("Run `uv run python .github/scripts/gen_reference.py` to regenerate.")
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main())

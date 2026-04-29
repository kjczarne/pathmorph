"""
pathmorph CLI — subcommands: init, pack, unpack, diff, verify.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from pathmorph.collision import CollisionAbort, CollisionStrategy
from pathmorph.core import diff, pack, unpack, verify
from pathmorph.schemas import Schema

app = typer.Typer(
    name="pathmorph",
    help=(
        "Invertible directory structure transformations.\n\n"
        "Apply a declarative schema to rewrite a directory layout, then "
        "restore the original at any time using the embedded manifest."
    ),
    pretty_exceptions_show_locals=False,
)

console = Console()
err_console = Console(stderr=True, style="bold red")


# ------------------------------------------------------------------ #
# Shared option types                                                  #
# ------------------------------------------------------------------ #

SchemaOpt = Annotated[
    Path,
    typer.Option(
        "-s", "--schema",
        help="Path to schema file (.toml, .yaml, .yml, .json).",
        show_default=False,
    ),
]

HandleExistingOpt = Annotated[
    Optional[CollisionStrategy],
    typer.Option(
        "--handle-existing",
        help=(
            "How to handle destination paths that already exist. "
            "If omitted, pathmorph prompts interactively per file."
        ),
        show_default=False,
    ),
]

MoveFlag = Annotated[
    bool,
    typer.Option(
        "--move",
        help="Move files instead of copying (destructive — use with caution).",
    ),
]

HashAlgoOpt = Annotated[
    str,
    typer.Option(
        "--hash",
        help=(
            "Hashing algorithm for file integrity. "
            "Builtins: sha256, sha1, md5, blake2b. "
            "With pathmorph[xxhash]: xxh64, xxh128, xxh3_64, xxh3_128."
        ),
    ),
]


# ------------------------------------------------------------------ #
# init                                                                  #
# ------------------------------------------------------------------ #

_TOML_TEMPLATE = """\
[schema]
name        = "{name}"
description = ""
fallback    = "passthrough"   # passthrough | omit

[[schema.rules]]
# pattern: full-match regex with named capture groups
# target:  format string that references those groups
pattern = 'runs/(?P<run>[^/]+)/(?P<rest>.+)'
target  = "experiments/{{run}}/{{rest}}"
"""

_YAML_TEMPLATE = """\
schema:
  name: {name}
  description: ""
  fallback: passthrough   # passthrough | omit

  rules:
    # pattern: full-match regex with named capture groups
    # target:  format string that references those groups
    - pattern: 'runs/(?P<run>[^/]+)/(?P<rest>.+)'
      target: "experiments/{{run}}/{{rest}}"
"""

_JSON_TEMPLATE = """\
{{
  "schema": {{
    "name": "{name}",
    "description": "",
    "fallback": "passthrough",
    "rules": [
      {{
        "pattern": "runs/(?P<run>[^/]+)/(?P<rest>.+)",
        "target": "experiments/{{run}}/{{rest}}"
      }}
    ]
  }}
}}
"""

_TEMPLATES: dict[str, str] = {
    ".toml": _TOML_TEMPLATE,
    ".yaml": _YAML_TEMPLATE,
    ".yml":  _YAML_TEMPLATE,
    ".json": _JSON_TEMPLATE,
}


@app.command("init")
def init_cmd(
    output: Annotated[Path, typer.Argument(help="Path for the new schema file (.toml, .yaml, .yml, .json).")],
    name: Annotated[
        str,
        typer.Option("--name", "-n", help="Schema name embedded in the template."),
    ] = "my_schema",
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite the file if it already exists."),
    ] = False,
) -> None:
    """
    Create a template schema file ready to customise.

    The format is determined by the file extension:
    .toml, .yaml / .yml, or .json.
    """
    suffix = output.suffix.lower()
    if suffix not in _TEMPLATES:
        err_console.print(
            f"Unsupported extension '{suffix}'. "
            "Use .toml, .yaml, .yml, or .json."
        )
        raise typer.Exit(code=1)

    if output.exists() and not force:
        err_console.print(
            f"'{output}' already exists. Pass --force to overwrite."
        )
        raise typer.Exit(code=1)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_TEMPLATES[suffix].format(name=name))
    console.print(f"\n[bold green]✓ Schema template written to[/bold green] [cyan]{output}[/cyan]\n")


# ------------------------------------------------------------------ #
# pack                                                                  #
# ------------------------------------------------------------------ #

@app.command("pack")
def pack_cmd(
    srcs: Annotated[list[Path], typer.Argument(help="One or more source directories to pack.")],
    dst: Annotated[
        Path,
        typer.Option("--dst", "-d", help="Destination directory for the packed layout.", show_default=False),
    ],
    schema: SchemaOpt = ...,  # required
    move: MoveFlag = False,
    handle_existing: HandleExistingOpt = None,
    hash_algo: HashAlgoOpt = "sha256",
) -> None:
    """
    Apply a schema's forward mapping from one or more SRCS to DST.

    Multiple source directories are supported.  Each file's schema-visible
    path is prefixed with the source label (the path as given) so that rules
    can distinguish between sources.  Single-source behaviour is unchanged.

    Writes a manifest to DST/.pathmorph_manifest.json that enables
    lossless inversion via the `unpack` command.
    """
    loaded_schema = _load_schema(schema)

    try:
        result = pack(
            srcs, dst,
            schema=loaded_schema,
            move=move,
            collision=handle_existing,
            hash_algorithm=hash_algo,
        )
    except CollisionAbort as e:
        err_console.print(f"[abort] {e}")
        raise typer.Exit(code=1)

    action = "moved" if move else "copied"
    non_omitted = [r for r in result.records if not r.omitted]
    passthrough = sum(1 for r in non_omitted if not r.matched and not r.crammed)
    remapped = sum(1 for r in non_omitted if r.matched)
    crammed = sum(1 for r in non_omitted if r.crammed)

    console.print(f"\n[bold green]✓ Pack complete[/bold green]")
    console.print(f"  Schema    : [cyan]{loaded_schema.name}[/cyan]")
    console.print(f"  Files {action}: [white]{len(non_omitted)}[/white]")
    console.print(f"    remapped  : {remapped}")
    console.print(f"    passthrough: {passthrough}")
    if crammed:
        console.print(f"    crammed   : [magenta]{crammed}[/magenta]")
    if result.omitted_count:
        console.print(f"    omitted   : [yellow]{result.omitted_count}[/yellow]")
    console.print(f"  Manifest  : [dim]{result.manifest_path}[/dim]\n")


# ------------------------------------------------------------------ #
# unpack                                                               #
# ------------------------------------------------------------------ #

@app.command("unpack")
def unpack_cmd(
    packed_dir: Annotated[Path, typer.Argument(help="Directory produced by `pack`.")],
    dst: Annotated[Path, typer.Argument(help="Destination for restored files.")],
    move: MoveFlag = False,
    handle_existing: HandleExistingOpt = None,
) -> None:
    """
    Restore the original directory layout using the embedded manifest.

    PACKED_DIR must contain a .pathmorph_manifest.json file.
    """
    try:
        result = unpack(packed_dir, dst, move=move, collision=handle_existing)
    except CollisionAbort as e:
        err_console.print(f"[abort] {e}")
        raise typer.Exit(code=1)
    except FileNotFoundError as e:
        err_console.print(str(e))
        raise typer.Exit(code=1)

    action = "moved" if move else "copied"
    console.print(f"\n[bold green]✓ Unpack complete[/bold green]")
    console.print(f"  Files {action}: [white]{len(result.restored)}[/white]")
    if result.skipped:
        console.print(f"  Skipped   : [yellow]{len(result.skipped)}[/yellow]")
        for p in result.skipped:
            console.print(f"    [dim]{p}[/dim]")
    console.print()


# ------------------------------------------------------------------ #
# diff                                                                  #
# ------------------------------------------------------------------ #

@app.command("diff")
def diff_cmd(
    srcs: Annotated[list[Path], typer.Argument(help="One or more source directories to inspect.")],
    schema: SchemaOpt = ...,
    show_passthrough: Annotated[
        bool,
        typer.Option("--show-passthrough/--hide-passthrough",
                     help="Show files that pass through unchanged."),
    ] = True,
) -> None:
    """
    Show what the forward mapping would produce — without touching the filesystem.
    """
    loaded_schema = _load_schema(schema)
    records = diff(srcs, loaded_schema)

    table = Table(
        "Original path", "→  Packed path", "Status",
        box=box.SIMPLE_HEAD,
        show_lines=False,
        header_style="bold cyan",
    )

    remapped = omitted = passthrough = crammed = 0
    for r in records:
        if r.omitted:
            table.add_row(str(r.original), "[dim]—[/dim]", "[yellow]omit[/yellow]")
            omitted += 1
        elif r.crammed:
            table.add_row(str(r.original), str(r.packed), "[magenta]cram[/magenta]")
            crammed += 1
        elif not r.matched:
            if show_passthrough:
                table.add_row(str(r.original), str(r.original), "[dim]pass[/dim]")
            passthrough += 1
        else:
            table.add_row(str(r.original), str(r.packed), "[green]remap[/green]")
            remapped += 1

    console.print(f"\n[bold]Schema:[/bold] [cyan]{loaded_schema.name}[/cyan]")
    if loaded_schema.description:
        console.print(f"[dim]{loaded_schema.description}[/dim]")
    console.print()
    console.print(table)
    summary = (
        f"  [green]{remapped}[/green] remapped  "
        f"[dim]{passthrough}[/dim] passthrough  "
        f"[yellow]{omitted}[/yellow] omitted"
    )
    if crammed:
        summary += f"  [magenta]{crammed}[/magenta] crammed"
    console.print(summary + "\n")


# ------------------------------------------------------------------ #
# verify                                                               #
# ------------------------------------------------------------------ #

@app.command("verify")
def verify_cmd(
    packed_dir: Annotated[Path, typer.Argument(help="Directory produced by `pack`.")],
) -> None:
    """
    Verify that all files in PACKED_DIR match their manifest hashes.

    Exits with code 0 on success, 1 if any file fails.
    """
    try:
        result = verify(packed_dir)
    except FileNotFoundError as e:
        err_console.print(str(e))
        raise typer.Exit(code=1)

    if result.passed:
        console.print(
            f"\n[bold green]✓ All {len(result.ok)} file(s) passed integrity check.[/bold green]\n"
        )
    else:
        console.print(
            f"\n[bold red]✗ {len(result.failed)} file(s) failed integrity check:[/bold red]"
        )
        for f in result.failed:
            console.print(f"  [red]{f}[/red]")
        console.print(f"  [green]{len(result.ok)} file(s) OK[/green]\n")
        raise typer.Exit(code=1)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _load_schema(path: Path) -> Schema:
    try:
        return Schema.from_file(path)
    except Exception as e:
        err_console.print(f"Failed to load schema '{path}': {e}")
        raise typer.Exit(code=1)


# Rename commands to match the public CLI surface
init_cmd.name = "init"          # type: ignore[attr-defined]
pack_cmd.name = "pack"          # type: ignore[attr-defined]
unpack_cmd.name = "unpack"      # type: ignore[attr-defined]
diff_cmd.name = "diff"          # type: ignore[attr-defined]
verify_cmd.name = "verify"      # type: ignore[attr-defined]


def main() -> None:
    app()

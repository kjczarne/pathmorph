# pathmorph

**Invertible directory structure transformations.**

`pathmorph` lets you remap a directory layout to a different schema and
restore the original at any time — with zero external state beyond a small
JSON manifest embedded in the packed directory.

It is designed to be:

- **Composable** — fits naturally into shell pipelines and Makefiles
- **Reproducible** — the manifest is a ground-truth record of every file move
- **Format-agnostic** — schemas can be written in YAML, TOML, or JSON
- **Dependency-light** — `confuk`, `omegaconf`, `rich`, `typer`; nothing heavier

---

## Motivation

Pipeline tools tend to emit outputs in machine-friendly directory structures:
short IDs, flat hierarchies, terse filenames. Collaborators often expect
something different: descriptive names, nested grouping, human-readable paths.

`pathmorph` lets you have both. Define the mapping as a schema, `pack` when
you need the human layout, `unpack` when you need the original back.

---

## Installation

```bash
pip install pathmorph

# Optional: faster hashing for large directories
pip install pathmorph[xxhash]
```

---

## Quickstart

**1. Write a schema** (`my_schema.yaml/toml/json`):

```yaml
schema:
  name: human_v1
  description: "Friendly layout for collaborators"
  fallback: passthrough   # or: omit/cram

  rules:
    - pattern: "runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\\.tsv"
      target:  "experiments/{exp}/candidates/{variant}/developability_scores.tsv"

    - pattern: "runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/(?P<rest>.+)"
      target:  "experiments/{exp}/candidates/{variant}/{rest}"
```

Each rule is a Python `re` **full-match** pattern with named capture groups.
The `target` is a format string that references those groups via `{name}`.

Rules are evaluated in order — the **first match wins**.

`fallback` controls what happens when no rule matches:
- `passthrough` — file is copied as-is (default)
- `omit` — file is skipped entirely
- `cram` – file is crammed into the subdirectory path specified in the `crampath` variable in the schema

You can also use symlinks that match against a pre-existing rule via its `id`:

```toml
[[schema.rules]]
id      = "scores"          # optional id — required if referenced by a symlink rule
pattern = 'runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\.tsv'
target  = "experiments/{exp}/candidates/{variant}/scores.tsv"

[[schema.rules]]
symlink = "scores"          # references the rule above by id
target  = "latest/{exp}/{variant}/scores.tsv"
```

> [!note]
> TOML/YAML/JSON schema files are supported

There are three implicit variables always available in `target`:

| Variable       | Value for `outputs/o01/o01_docked_result.txt` |
| -------------- | --------------------------------------------- |
| `{__name__}`   | `o01_docked_result.txt`                       |
| `{__stem__}`   | `o01_docked_result`                           |
| `{__suffix__}` | `.txt`                                        |

And you can leverage `confuk`/`omegaconf`'s variable interpolation to avoid repeating RegEx patterns:

```toml
exp_pattern = '(?P<exp>exp\d+[^/]+)'
variant_pattern = '(?P<variant>v\d+[^/]+)'

[[schema.rules]]
pattern = 'runs/${exp_pattern}/${variant_pattern}/scores\.tsv'
target  = "experiments/{exp}/candidates/{variant}/scores.tsv"

[[schema.rules]]
pattern = 'runs/${exp_pattern}/${variant_pattern}/logs'
target  = "experiments/{exp}/candidates/{variant}/Reports"
```

**2. Preview the mapping** (dry-run, no filesystem changes):

```bash
pathmorph diff ./pipeline_output -s my_schema.yaml
```

**3. Pack into the human layout**:

```bash
pathmorph pack ./pipeline_output -d ./for_collaborators -s my_schema.yaml
```

This copies files (non-destructive by default) and writes
`./for_collaborators/.pathmorph_manifest.json`.

**4. Restore the original layout**:

```bash
pathmorph unpack ./for_collaborators ./restored_original
```

**5. Verify integrity**:

```bash
pathmorph verify ./for_collaborators
```

---

## Command reference

### `pack`

```
pathmorph pack SRCS -d/--dst DST -s/--schema SCHEMA [OPTIONS]

Options:
  --move                   Move files instead of copying
  --handle-existing        abort | skip | overwrite
                           (prompts interactively if omitted)
  --hash ALGO              sha256 (default), sha1, md5, blake2b,
                           xxh64, xxh128, xxh3_64, xxh3_128
```

### `unpack`

```
pathmorph unpack PACKED_DIR DST [OPTIONS]

Options:
  --move                   Move files instead of copying
  --handle-existing        abort | skip | overwrite
```

### `diff`

```
pathmorph diff SRCS -s/--schema SCHEMA [OPTIONS]

Options:
  --show-passthrough / --hide-passthrough   (default: show)
```

### `verify`

```
pathmorph verify PACKED_DIR

Exit code 0 on success, 1 if any file fails.
```

---

## Schema formats

`pathmorph` accepts any format supported by
[confuk](https://github.com/kjczarne/confuk): `.yaml`, `.yml`, `.toml`, `.json`.

See [`examples/`](examples/) for the same schema in YAML and TOML.

---

## Manifest

A packed directory is self-describing. The manifest at
`.pathmorph_manifest.json` records:

```json
{
  "version": 1,
  "schema_name": "human_v1",
  "schema_description": "...",
  "packed_at": "2026-04-29T...",
  "algorithm": "sha256",
  "entries": [
    {
      "original": "runs/exp_001/A/scores.tsv",
      "packed":   "experiments/exp_001/candidates/A/developability_scores.tsv",
      "hash":     "abc123...",
      "algorithm": "sha256"
    }
  ]
}
```

`unpack` uses the entry table directly — it does **not** re-evaluate schema
rules. This means the manifest is a durable, schema-version-independent
record: even if you later change the schema, existing packed directories
remain unpackable.

---

## Using as a library

```python
from pathlib import Path
from pathmorph import pack, unpack, verify, diff
from pathmorph.schemas import Schema
from pathmorph.collision import CollisionStrategy

schema = Schema.from_file(Path("my_schema.yaml"))

# Dry-run
records = diff(Path("./pipeline_output"), schema)

# Pack
result = pack(
    Path("./pipeline_output"),
    Path("./for_collaborators"),
    schema=schema,
    collision=CollisionStrategy.SKIP,
    hash_algorithm="sha256",
)

# Verify
verify_result = verify(Path("./for_collaborators"))
assert verify_result.passed

# Restore
unpack(Path("./for_collaborators"), Path("./restored"))
```

---

## Development

```bash
git clone https://github.com/yourusername/pathmorph
cd pathmorph
pip install -e ".[dev]"
pytest
```

---

## License

MIT

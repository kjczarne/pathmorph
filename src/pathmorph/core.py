"""
Core filesystem operations.

All four public functions follow the same signature philosophy:
    - Accept plain Path objects and dataclass config
    - Return structured results (not print directly)
    - Side effects are explicit (copy/move, manifest write)
    - Rich console output is handled by the CLI layer
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from pathmorph.collision import CollisionAbort, CollisionResolver, CollisionStrategy
from pathmorph.manifest import MANIFEST_FILENAME, Manifest
from pathmorph.schemas import Schema


# ------------------------------------------------------------------ #
# Result types — structured return values for every operation         #
# ------------------------------------------------------------------ #


@dataclass
class MappingRecord:
    """A single resolved src→dst pair from diff or pack."""

    original: Path   # relative to src root
    packed: Path     # relative to dst root
    matched: bool    # False when file was passed through unchanged
    omitted: bool    # True when schema fallback=omit and no rule matched


@dataclass
class PackResult:
    records: list[MappingRecord]
    manifest_path: Path
    omitted_count: int
    moved: bool  # True if --move was used


@dataclass
class UnpackResult:
    restored: list[tuple[Path, Path]]   # (packed_rel, original_rel)
    skipped: list[Path]                 # packed_rel paths that were skipped
    moved: bool


@dataclass
class VerifyResult:
    ok: list[str]       # packed-relative paths that pass
    failed: list[str]   # packed-relative paths that fail (missing or hash mismatch)

    @property
    def passed(self) -> bool:
        return len(self.failed) == 0


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _iter_files(root: Path) -> Iterator[Path]:
    """Yield all files under *root* (no directories, no manifest)."""
    for p in root.rglob("*"):
        if p.is_file() and p.name != MANIFEST_FILENAME:
            yield p


def _transfer(src: Path, dst: Path, move: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(src), dst)
    else:
        shutil.copy2(src, dst)


# ------------------------------------------------------------------ #
# diff — dry-run forward mapping                                        #
# ------------------------------------------------------------------ #


def diff(src: Path, schema: Schema) -> list[MappingRecord]:
    """
    Compute the forward mapping for all files in *src* without
    touching the filesystem.
    """
    records: list[MappingRecord] = []
    for abs_path in _iter_files(src):
        rel = abs_path.relative_to(src)
        mapped = schema.forward(rel)
        if mapped is None:
            records.append(MappingRecord(rel, Path(), matched=False, omitted=True))
        else:
            matched = mapped != rel
            records.append(MappingRecord(rel, mapped, matched=matched, omitted=False))
    return records


# ------------------------------------------------------------------ #
# pack                                                                  #
# ------------------------------------------------------------------ #


def pack(
    src: Path,
    dst: Path,
    schema: Schema,
    *,
    move: bool = False,
    collision: CollisionStrategy | None = None,
    hash_algorithm: str = "sha256",
) -> PackResult:
    """
    Apply *schema*'s forward mapping from *src* to *dst*.

    Writes a manifest to ``dst/.pathmorph_manifest.json``.
    """
    if not src.is_dir():
        raise NotADirectoryError(f"Source '{src}' is not a directory.")
    dst.mkdir(parents=True, exist_ok=True)

    resolver = CollisionResolver(collision)
    manifest = Manifest.new(schema.name, schema.description, hash_algorithm)
    records: list[MappingRecord] = []
    omitted = 0

    for abs_src in _iter_files(src):
        rel = abs_src.relative_to(src)
        mapped = schema.forward(rel)

        if mapped is None:
            records.append(MappingRecord(rel, Path(), matched=False, omitted=True))
            omitted += 1
            continue

        abs_dst = dst / mapped

        if abs_dst.exists():
            action = resolver.resolve(abs_dst)   # may raise CollisionAbort
            if action == "skip":
                records.append(MappingRecord(rel, mapped, matched=mapped != rel, omitted=False))
                continue
            # overwrite — fall through to transfer

        _transfer(abs_src, abs_dst, move=move)
        manifest.add_entry(rel, mapped, abs_dst)
        records.append(MappingRecord(rel, mapped, matched=mapped != rel, omitted=False))

    manifest_path = manifest.write(dst)
    return PackResult(
        records=records,
        manifest_path=manifest_path,
        omitted_count=omitted,
        moved=move,
    )


# ------------------------------------------------------------------ #
# unpack                                                               #
# ------------------------------------------------------------------ #


def unpack(
    packed_root: Path,
    dst: Path,
    *,
    move: bool = False,
    collision: CollisionStrategy | None = None,
) -> UnpackResult:
    """
    Reverse a pack operation using the manifest inside *packed_root*.

    Restores files to *dst* in their original layout.
    """
    manifest = Manifest.from_file(packed_root)
    dst.mkdir(parents=True, exist_ok=True)

    resolver = CollisionResolver(collision)
    restored: list[tuple[Path, Path]] = []
    skipped: list[Path] = []

    for entry in manifest.iter_entries():
        abs_packed = packed_root / entry.packed
        abs_dst = dst / entry.original

        if not abs_packed.exists():
            # File disappeared from packed dir — warn, don't abort
            skipped.append(Path(entry.packed))
            continue

        if abs_dst.exists():
            action = resolver.resolve(abs_dst)
            if action == "skip":
                skipped.append(Path(entry.packed))
                continue

        _transfer(abs_packed, abs_dst, move=move)
        restored.append((Path(entry.packed), Path(entry.original)))

    return UnpackResult(restored=restored, skipped=skipped, moved=move)


# ------------------------------------------------------------------ #
# verify                                                               #
# ------------------------------------------------------------------ #


def verify(packed_root: Path) -> VerifyResult:
    """
    Check every manifest entry against the file currently on disk.
    """
    manifest = Manifest.from_file(packed_root)
    ok: list[str] = []
    failed: list[str] = []

    for entry in manifest.iter_entries():
        if manifest.verify_entry(entry, packed_root):
            ok.append(entry.packed)
        else:
            failed.append(entry.packed)

    return VerifyResult(ok=ok, failed=failed)

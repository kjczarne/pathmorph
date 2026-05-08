"""
Core filesystem operations.

All four public functions follow the same signature philosophy:
    - Accept plain Path objects and dataclass config
    - Return structured results (not print directly)
    - Side effects are explicit (copy/move, manifest write)
    - Rich console output is handled by the CLI layer
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

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
    crammed: bool = False                          # True when schema fallback=cram and no rule matched
    symlink_targets: list[Path] = field(default_factory=list)  # additional symlink paths created


@dataclass
class PackResult:
    records: list[MappingRecord]
    manifest_path: Path
    omitted_count: int
    symlink_count: int
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


@dataclass
class AddResult:
    original: Path   # original relative path recorded in the manifest
    packed: Path     # destination relative path within the packed root
    moved: bool


@dataclass
class RemoveResult:
    packed: Path     # packed-relative path that was removed
    original: Path   # original relative path from the manifest entry


@dataclass
class MoveResult:
    old_packed: Path   # previous packed-relative path
    new_packed: Path   # new packed-relative path
    original: Path     # original relative path (unchanged)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _iter_files(root: Path) -> Iterator[Path]:
    """Yield all files under *root* (no directories, no manifest)."""
    for p in root.rglob("*"):
        if p.is_file() and p.name != MANIFEST_FILENAME:
            yield p


def _source_label(multi: bool, src: Path) -> str:
    """Return the virtual prefix for *src* in a multi-source pack.

    Empty string for single-source packs preserves backward compatibility.
    """
    if not multi:
        return ""
    if not src.is_absolute():
        return str(src)
    try:
        return str(src.relative_to(Path.cwd()))
    except ValueError:
        return src.name


def _iter_sources(srcs: list[Path]) -> Iterator[tuple[Path, Path, str]]:
    """Yield (abs_path, rel_within_source, label) across all sources."""
    # multi = len(srcs) > 1
    multi = True
    for src in srcs:
        label = _source_label(multi, src)
        for abs_path in _iter_files(src):
            yield abs_path, abs_path.relative_to(src), label


def _transfer(src: Path, dst: Path, move: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(src), dst)
    else:
        shutil.copy2(src, dst)


# ------------------------------------------------------------------ #
# diff — dry-run forward mapping                                        #
# ------------------------------------------------------------------ #


def diff(srcs: list[Path], schema: Schema) -> list[MappingRecord]:
    """
    Compute the forward mapping for all files in *srcs* without
    touching the filesystem.
    """
    records: list[MappingRecord] = []
    for _abs, rel, label in _iter_sources(srcs):
        virtual_rel = Path(label) / rel if label else rel
        res = schema.resolve(virtual_rel)
        if res.mapped is None:
            records.append(MappingRecord(virtual_rel, Path(), matched=False, omitted=True))
        else:
            records.append(MappingRecord(
                virtual_rel, res.mapped,
                matched=res.matched, omitted=False,
                crammed=res.crammed,
                symlink_targets=res.symlink_targets,
            ))
    return records


# ------------------------------------------------------------------ #
# pack                                                                  #
# ------------------------------------------------------------------ #


def pack(
    srcs: list[Path],
    dst: Path,
    schema: Schema,
    *,
    move: bool = False,
    collision: CollisionStrategy | None = None,
    hash_algorithm: str = "sha256",
) -> PackResult:
    """
    Apply *schema*'s forward mapping from one or more *srcs* to *dst*.

    When multiple sources are supplied each file's schema-visible path is
    prefixed with the source's label (the path as given on the CLI, or its
    name when an absolute path cannot be made relative to cwd).  The label
    is stored in the manifest so ``unpack`` can restore files to the correct
    source root.

    Writes a manifest to ``dst/.pathmorph_manifest.json``.
    """
    for src in srcs:
        if not src.is_dir():
            raise NotADirectoryError(f"Source '{src}' is not a directory.")
    dst.mkdir(parents=True, exist_ok=True)

    resolver = CollisionResolver(collision)
    manifest = Manifest.new(schema.name, schema.description, hash_algorithm)
    records: list[MappingRecord] = []
    omitted = 0
    symlinks_created = 0

    for abs_src, rel, label in _iter_sources(srcs):
        virtual_rel = Path(label) / rel if label else rel
        res = schema.resolve(virtual_rel)

        if res.mapped is None:
            records.append(MappingRecord(virtual_rel, Path(), matched=False, omitted=True))
            omitted += 1
            continue

        abs_dst = dst / res.mapped

        if abs_dst.exists():
            action = resolver.resolve(abs_dst)   # may raise CollisionAbort
            if action == "skip":
                records.append(MappingRecord(
                    virtual_rel, res.mapped,
                    matched=res.matched, omitted=False,
                    crammed=res.crammed,
                ))
                continue
            # overwrite — fall through to transfer

        _transfer(abs_src, abs_dst, move=move)
        manifest.add_entry(rel, res.mapped, abs_dst, source_root=label)

        # Create relative symlinks for any SymlinkRules that fired.
        created_sym: list[Path] = []
        for sym_rel in res.symlink_targets:
            abs_sym = dst / sym_rel
            abs_sym.parent.mkdir(parents=True, exist_ok=True)
            rel_target = os.path.relpath(abs_dst, abs_sym.parent)
            if abs_sym.exists() or abs_sym.is_symlink():
                abs_sym.unlink()
            abs_sym.symlink_to(rel_target)
            created_sym.append(sym_rel)
            symlinks_created += 1

        records.append(MappingRecord(
            virtual_rel, res.mapped,
            matched=res.matched, omitted=False,
            crammed=res.crammed,
            symlink_targets=created_sym,
        ))

    manifest_path = manifest.write(dst)
    return PackResult(
        records=records,
        manifest_path=manifest_path,
        omitted_count=omitted,
        symlink_count=symlinks_created,
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
        # source_root="" → single-source; collapses to dst/original (backward-compat)
        # source_root="data/run1" → multi-source; restore under dst/data/run1/
        abs_dst = dst / entry.source_root / entry.original if entry.source_root else dst / entry.original

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


# ------------------------------------------------------------------ #
# add_file — explicitly add a file to an already-packed directory     #
# ------------------------------------------------------------------ #


def add_file(
    src: Path,
    packed_root: Path,
    dest_rel: Path,
    *,
    original: Path | None = None,
    source_root: str = "",
    move: bool = False,
    collision: CollisionStrategy | None = None,
) -> AddResult:
    """
    Copy (or move) *src* into *packed_root* at *dest_rel* and record
    the mapping in the existing manifest.

    *original* is the path stored in the manifest entry for ``unpack`` to
    restore the file to.  Defaults to the source filename.
    """
    if not src.is_file():
        raise FileNotFoundError(f"Source '{src}' does not exist or is not a file.")

    manifest = Manifest.from_file(packed_root)
    orig = original if original is not None else Path(src.name)
    abs_dst = packed_root / dest_rel

    resolver = CollisionResolver(collision)
    if abs_dst.exists():
        action = resolver.resolve(abs_dst)
        if action == "skip":
            return AddResult(original=orig, packed=dest_rel, moved=move)

    _transfer(src, abs_dst, move=move)
    manifest.add_entry(orig, dest_rel, abs_dst, source_root=source_root)
    manifest.write(packed_root)
    return AddResult(original=orig, packed=dest_rel, moved=move)


# ------------------------------------------------------------------ #
# remove_file — remove a file and its manifest entry                  #
# ------------------------------------------------------------------ #


def remove_file(packed_root: Path, packed_rel: Path) -> RemoveResult:
    """
    Delete *packed_rel* from *packed_root* and remove its manifest entry.

    If the file is missing from disk the manifest entry is still removed
    (safe cleanup).
    """
    manifest = Manifest.from_file(packed_root)
    try:
        entry = manifest.remove_entry(str(packed_rel))
    except KeyError:
        raise KeyError(f"No manifest entry for '{packed_rel}'.")

    abs_packed = packed_root / packed_rel
    if abs_packed.exists():
        abs_packed.unlink()

    manifest.write(packed_root)
    return RemoveResult(packed=packed_rel, original=Path(entry.original))


# ------------------------------------------------------------------ #
# move_file — rename a file within the packed directory               #
# ------------------------------------------------------------------ #


def move_file(
    packed_root: Path,
    src_rel: Path,
    dst_rel: Path,
    *,
    collision: CollisionStrategy | None = None,
) -> MoveResult:
    """
    Move *src_rel* to *dst_rel* within *packed_root* and update the manifest.

    The ``original`` path recorded at pack time is preserved unchanged.
    """
    abs_src = packed_root / src_rel
    if not abs_src.exists():
        raise FileNotFoundError(f"'{abs_src}' does not exist in packed directory.")

    manifest = Manifest.from_file(packed_root)
    abs_dst = packed_root / dst_rel

    resolver = CollisionResolver(collision)
    if abs_dst.exists():
        action = resolver.resolve(abs_dst)
        if action == "skip":
            entry = next((e for e in manifest.iter_entries() if e.packed == str(src_rel)), None)
            orig = Path(entry.original) if entry else src_rel
            return MoveResult(old_packed=src_rel, new_packed=dst_rel, original=orig)

    entry = manifest.rename_entry(str(src_rel), str(dst_rel))
    _transfer(abs_src, abs_dst, move=True)
    manifest.write(packed_root)
    return MoveResult(old_packed=src_rel, new_packed=dst_rel, original=Path(entry.original))

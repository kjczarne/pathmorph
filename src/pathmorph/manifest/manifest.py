"""
Manifest — the ground-truth record of a pack operation.

A manifest is written as `.pathmorph_manifest.json` inside the
packed (destination) directory. It records every (original, packed)
path pair and a content hash for each file, enabling both:

    - Lossless inversion via `unpack`
    - Integrity verification via `verify`

The manifest is intentionally append-only in spirit: once written it
should not be edited by hand. Treat it as a commit object.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal

MANIFEST_FILENAME = ".pathmorph_manifest.json"
MANIFEST_VERSION = 1

# Algorithms available without optional extras
_BUILTIN_ALGOS = {"sha256", "sha1", "md5", "blake2b"}


def _get_hasher(algorithm: str) -> Callable[[], "hashlib._Hash"]:
    """Return a zero-argument callable that produces a new hasher."""
    if algorithm in _BUILTIN_ALGOS:
        return lambda: hashlib.new(algorithm)
    # Try xxhash (optional dependency)
    try:
        import xxhash  # type: ignore[import-untyped]

        mapping = {
            "xxh64": xxhash.xxh64,
            "xxh128": xxhash.xxh128,
            "xxh3_64": xxhash.xxh3_64,
            "xxh3_128": xxhash.xxh3_128,
        }
        if algorithm in mapping:
            return mapping[algorithm]
        raise ValueError(f"Unknown algorithm '{algorithm}'. Install 'xxhash' for xxh* variants.")
    except ImportError:
        raise ValueError(
            f"Algorithm '{algorithm}' requires the 'xxhash' package. "
            "Install it with: pip install pathmorph[xxhash]"
        ) from None


def hash_file(path: Path, algorithm: str = "sha256", chunk_size: int = 1 << 20) -> str:
    """Return hex digest of *path* using *algorithm*."""
    h = _get_hasher(algorithm)()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ #
# Data model                                                           #
# ------------------------------------------------------------------ #


@dataclass
class ManifestEntry:
    original: str   # relative to the source root at pack time
    packed: str     # relative to the packed root
    hash: str       # hex digest
    algorithm: str  # algorithm used to compute hash


@dataclass
class Manifest:
    version: int
    schema_name: str
    schema_description: str
    packed_at: str                        # ISO-8601 UTC
    algorithm: str
    entries: list[ManifestEntry] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Factories                                                            #
    # ------------------------------------------------------------------ #

    @classmethod
    def new(cls, schema_name: str, schema_description: str, algorithm: str) -> "Manifest":
        return cls(
            version=MANIFEST_VERSION,
            schema_name=schema_name,
            schema_description=schema_description,
            packed_at=datetime.now(timezone.utc).isoformat(),
            algorithm=algorithm,
        )

    @classmethod
    def from_file(cls, packed_root: Path) -> "Manifest":
        manifest_path = packed_root / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"No manifest found at '{manifest_path}'. "
                "Is this directory a pathmorph-packed directory?"
            )
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = [ManifestEntry(**e) for e in data.pop("entries")]
        return cls(**data, entries=entries)

    # ------------------------------------------------------------------ #
    # I/O                                                                  #
    # ------------------------------------------------------------------ #

    def write(self, packed_root: Path) -> Path:
        """Serialise manifest to JSON inside *packed_root*."""
        manifest_path = packed_root / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def add_entry(self, original: Path, packed: Path, file_path: Path) -> None:
        digest = hash_file(file_path, self.algorithm)
        self.entries.append(
            ManifestEntry(
                original=str(original),
                packed=str(packed),
                hash=digest,
                algorithm=self.algorithm,
            )
        )

    def iter_entries(self) -> Iterator[ManifestEntry]:
        yield from self.entries

    def verify_entry(self, entry: ManifestEntry, packed_root: Path) -> bool:
        """Return True if the packed file matches its recorded digest."""
        file_path = packed_root / entry.packed
        if not file_path.exists():
            return False
        return hash_file(file_path, entry.algorithm) == entry.hash

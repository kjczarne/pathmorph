"""
pathmorph — invertible directory structure transformations.

Public API surface:
    pack()    — apply a schema's forward mapping, write manifest
    unpack()  — reverse a manifest, restore original layout
    verify()  — check manifest integrity against packed directory
    diff()    — dry-run: show forward mapping without touching the filesystem
"""

from pathmorph.core import diff, pack, unpack, verify

__all__ = ["pack", "unpack", "verify", "diff"]

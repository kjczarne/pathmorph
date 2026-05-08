"""
pathmorph — invertible directory structure transformations.

Public API surface:
    pack()        — apply a schema's forward mapping, write manifest
    unpack()      — reverse a manifest, restore original layout
    verify()      — check manifest integrity against packed directory
    diff()        — dry-run: show forward mapping without touching the filesystem
    add_file()    — copy a file into a packed directory and record it in the manifest
    remove_file() — delete a file from a packed directory and remove its manifest entry
    move_file()   — rename a file within a packed directory and update the manifest
"""

from pathmorph.core import add_file, diff, move_file, pack, remove_file, unpack, verify

__all__ = ["pack", "unpack", "verify", "diff", "add_file", "remove_file", "move_file"]

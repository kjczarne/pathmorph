"""
Collision resolution when a destination path already exists.

Three strategies, matching the CLI flag values:

    abort     — raise immediately; let the caller clean up
    skip      — leave the existing file, don't overwrite
    overwrite — replace the existing file silently

When no strategy is set (None), the resolver prompts the user
interactively for each collision. The per-file response can be
promoted to a session-wide default ("all") to avoid repeated prompts.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal


class CollisionStrategy(str, Enum):
    ABORT = "abort"
    SKIP = "skip"
    OVERWRITE = "overwrite"


class CollisionAbort(RuntimeError):
    """Raised when strategy=abort and a collision is detected."""


def _prompt(dst: Path) -> tuple[Literal["skip", "overwrite"], bool]:
    """
    Ask the user what to do about a single collision.

    Returns (action, apply_to_all).
    """
    print(f"\n  [collision] '{dst}' already exists.")
    print("  What should pathmorph do?")
    print("    [s]  skip this file")
    print("    [o]  overwrite this file")
    print("    [sa] skip  ALL remaining collisions")
    print("    [oa] overwrite ALL remaining collisions")
    print("    [a]  abort")

    while True:
        raw = input("  Choice [s/o/sa/oa/a]: ").strip().lower()
        if raw == "s":
            return "skip", False
        if raw == "o":
            return "overwrite", False
        if raw == "sa":
            return "skip", True
        if raw == "oa":
            return "overwrite", True
        if raw == "a":
            raise CollisionAbort(f"Aborted by user at '{dst}'.")
        print("  Please enter one of: s, o, sa, oa, a")


class CollisionResolver:
    """
    Stateful resolver that handles per-file collisions.

    *strategy* is the CLI-supplied flag value (or None for interactive).
    The resolver remembers session-wide "apply to all" decisions so the
    user is only prompted once if they choose [sa] or [oa].
    """

    def __init__(self, strategy: CollisionStrategy | None = None) -> None:
        self._strategy = strategy
        self._session_override: Literal["skip", "overwrite"] | None = None

    def resolve(self, dst: Path) -> Literal["skip", "overwrite"]:
        """
        Decide what to do when *dst* already exists.

        Returns 'skip' or 'overwrite'. Raises CollisionAbort for abort.
        """
        if self._strategy is CollisionStrategy.ABORT:
            raise CollisionAbort(
                f"Destination '{dst}' already exists. "
                "Use --handle-existing skip|overwrite to suppress this error."
            )
        if self._strategy is CollisionStrategy.SKIP:
            return "skip"
        if self._strategy is CollisionStrategy.OVERWRITE:
            return "overwrite"

        # Interactive path
        if self._session_override is not None:
            return self._session_override

        action, apply_to_all = _prompt(dst)
        if apply_to_all:
            self._session_override = action
        return action

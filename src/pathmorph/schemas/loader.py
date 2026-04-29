"""
Schema loading and path-rule evaluation.

A schema file describes a list of rules. Each rule matches files
via a regex pattern and remaps them to a new relative path via a
format string that can reference named capture groups.

Example schema (YAML):

    schema:
      name: human_v1
      description: "Friendly layout for collaborators"
      rules:
        - pattern: "runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\\.tsv"
          target:  "experiments/{exp}/candidates/{variant}/developability_scores.tsv"
        - pattern: "runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/(?P<rest>.+)"
          target:  "experiments/{exp}/candidates/{variant}/{rest}"
      fallback: passthrough   # passthrough | omit
                              # passthrough: unmatched files are copied as-is
                              # omit:        unmatched files are skipped entirely
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from confuk import parse_config
from omegaconf import DictConfig, OmegaConf


@dataclass
class Rule:
    """A single path-rewriting rule."""

    pattern: re.Pattern[str]
    target: str  # format string with named-group placeholders

    @classmethod
    def from_dict(cls, d: DictConfig | dict) -> "Rule":
        raw = OmegaConf.to_container(d) if isinstance(d, DictConfig) else d
        return cls(
            pattern=re.compile(raw["pattern"]),
            target=raw["target"],
        )

    def apply(self, rel_path: Path) -> Path | None:
        """
        Try to match *rel_path* against this rule.

        Returns the remapped Path on success, None if the pattern
        does not match.
        """
        m = self.pattern.fullmatch(str(rel_path))
        if m is None:
            return None
        try:
            return Path(self.target.format(**m.groupdict()))
        except KeyError as exc:
            raise ValueError(
                f"Rule target '{self.target}' references capture group {exc} "
                f"which is not present in pattern '{self.pattern.pattern}'."
            ) from exc


FallbackMode = Literal["passthrough", "omit"]


@dataclass
class Schema:
    """Loaded, validated schema ready to apply forward mappings."""

    name: str
    description: str
    rules: list[Rule]
    fallback: FallbackMode = "passthrough"

    # ------------------------------------------------------------------ #
    # Factory                                                              #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_file(cls, path: Path) -> "Schema":
        """
        Load a schema from any format supported by confuk
        (.toml, .yaml, .yml, .json).
        """
        cfg: DictConfig = parse_config(path, "o")

        if "schema" not in cfg:
            raise ValueError(
                f"Schema file '{path}' must have a top-level 'schema' key."
            )

        s = cfg.schema
        rules_raw = OmegaConf.to_container(s.rules, resolve=True)

        fallback: FallbackMode = s.get("fallback", "passthrough")
        if fallback not in ("passthrough", "omit"):
            raise ValueError(
                f"'fallback' must be 'passthrough' or 'omit', got '{fallback}'."
            )

        return cls(
            name=str(s.name),
            description=str(s.get("description", "")),
            rules=[Rule.from_dict(r) for r in rules_raw],
            fallback=fallback,
        )

    # ------------------------------------------------------------------ #
    # Core mapping                                                         #
    # ------------------------------------------------------------------ #

    def forward(self, rel_path: Path) -> Path | None:
        """
        Apply the first matching rule to *rel_path*.

        Returns:
            Remapped path — or the original path if fallback='passthrough'
            and no rule matched — or None if fallback='omit'.
        """
        for rule in self.rules:
            result = rule.apply(rel_path)
            if result is not None:
                return result

        # No rule matched
        if self.fallback == "passthrough":
            return rel_path
        return None  # omit

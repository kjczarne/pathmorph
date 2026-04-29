"""
Schema loading and path-rule evaluation.

A schema file describes a list of rules. Each rule matches files via a regex
pattern and remaps them to a new relative path via a format string that can
reference named capture groups.  An optional ``symlink`` rule type creates
additional symlinks in the packed output without copying any files.

Example schema (YAML):

    schema:
      name: human_v1
      description: "Friendly layout for collaborators"
      rules:
        - id: scores
          pattern: "runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\\.tsv"
          target:  "experiments/{exp}/candidates/{variant}/developability_scores.tsv"
        - pattern: "runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/(?P<rest>.+)"
          target:  "experiments/{exp}/candidates/{variant}/{rest}"
        - symlink: scores
          target:  "latest/{exp}/{variant}/scores.tsv"
      fallback: passthrough   # passthrough | omit | cram
      # crampath: _misc        # required when fallback=cram
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from confuk import parse_config
from omegaconf import DictConfig, OmegaConf


# ------------------------------------------------------------------ #
# Rule types                                                           #
# ------------------------------------------------------------------ #


@dataclass
class Rule:
    """A single path-rewriting rule."""

    pattern: re.Pattern[str]
    target: str  # format string with named-group placeholders
    id: str = ""  # optional; required when referenced by a SymlinkRule

    @classmethod
    def from_dict(cls, d: DictConfig | dict) -> "Rule":
        raw = OmegaConf.to_container(d) if isinstance(d, DictConfig) else d
        return cls(
            pattern=re.compile(raw["pattern"]),
            target=raw["target"],
            id=raw.get("id", ""),
        )

    def apply(self, rel_path: Path) -> tuple[Path, dict[str, str], str] | None:
        """
        Try to match *rel_path* against this rule.

        The pattern is anchored at the start of the path string and must end
        at a path-component boundary (end of string or immediately before a
        ``/``).  This allows patterns to target either individual files *or*
        entire directory trees:

        - File pattern  ``runs/(?P<exp>[^/]+)/scores\\.tsv`` matches only
          that exact file and renames it to whatever ``target`` specifies.
        - Directory pattern  ``runs/(?P<exp>[^/]+)/raw`` matches every file
          inside ``raw/`` and appends the remaining path components to the
          target, reproducing the subtree under the new name.
        - Glob pattern  ``outputs/(?P<order>o\\d+)/o\\d+_docked.*`` — the
          ``.*`` crosses path separators so it fully consumes the path.
          pathmorph automatically retries with the shortest prefix that also
          matches, preserving everything after it as the suffix::

              # outputs/o000/o000_docked/all_pdbs/file.pdb
              # → shortest prefix match: outputs/o000/o000_docked
              # → suffix: all_pdbs/file.pdb
              # → result: 03_Modeler/outputs/all_pdbs/file.pdb

          For genuinely flat files (no path separator after the matched
          prefix), use ``{__name__}`` to avoid overwriting::

              target = "03_Modeler/outputs/{__name__}"

        Three implicit variables are always available in ``target`` without
        needing to be declared as named groups:

        - ``{__name__}``   — original filename including extension (``file.tsv``)
        - ``{__stem__}``   — filename without extension             (``file``)
        - ``{__suffix__}`` — extension including the dot            (``.tsv``)

        A user-defined capture group with the same name takes priority.

        Returns ``(remapped_path, capture_groups, suffix)`` on success, or
        ``None`` if the pattern does not match at a valid boundary.
        ``suffix`` is the path tail beyond the match (empty string for exact
        file/glob matches); it is appended to symlink targets that reference
        this rule.
        """
        s = str(rel_path)
        m = self.pattern.match(s)
        if m is None:
            return None
        end = m.end()
        # Reject matches that stop in the middle of a path component.
        if end < len(s) and s[end] != "/":
            return None

        suffix = s[end + 1:] if end < len(s) else ""
        effective_m = m

        # When the pattern fully consumed the path (no suffix from the
        # boundary check above), try to find the shortest prefix of the path
        # that the pattern can also match at a '/' boundary.  This preserves
        # subdirectory structure for directory-glob patterns that use '.*'
        # across separators, e.g.
        #
        #   pattern = 'outputs/(?P<order>o\d+)/o\d+_docked.*'
        #   target  = '03_Modeler/outputs'
        #
        # For outputs/o000/o000_docked/all_pdbs/file.pdb the effective prefix
        # becomes 'outputs/o000/o000_docked' and the suffix 'all_pdbs/file.pdb',
        # so the result is '03_Modeler/outputs/all_pdbs/file.pdb' rather than
        # every file overwriting '03_Modeler/outputs'.
        if not suffix:
            parts = s.split("/")
            for split_point in range(1, len(parts)):
                prefix = "/".join(parts[:split_point])
                m2 = self.pattern.match(prefix)
                if m2 is not None and m2.end() == len(prefix):
                    effective_m = m2
                    suffix = "/".join(parts[split_point:])
                    break

        groups = effective_m.groupdict()
        # Implicit path variables — available in every target without being
        # declared as named groups. setdefault means user groups take priority.
        groups.setdefault("__name__", rel_path.name)      # full filename:  "file.txt"
        groups.setdefault("__stem__", rel_path.stem)      # name minus ext: "file"
        groups.setdefault("__suffix__", rel_path.suffix)  # extension:      ".txt"
        try:
            base = Path(self.target.format(**groups))
        except KeyError as exc:
            raise ValueError(
                f"Rule target '{self.target}' references unknown group {exc}. "
                f"Named groups in pattern: {sorted(m.groupdict())}. "
                f"Built-in variables: {{__name__}}, {{__stem__}}, {{__suffix__}}."
            ) from exc
        path = base / suffix if suffix else base
        return path, groups, suffix


@dataclass
class SymlinkRule:
    """Creates a symlink in the packed output pointing to a rule's mapped path.

    The symlink is placed at ``target`` (formatted with the same capture
    groups as the referenced rule).  For directory-prefix matches, the
    remaining path suffix is appended to both the main target and the symlink
    target, so the entire subtree is mirrored.

    Symlinks are not recorded in the manifest and are silently dropped during
    ``unpack``.
    """

    symlink: str  # id of the Rule whose mapped path the symlink points to
    target: str   # format string; uses same named groups as the referenced rule

    @classmethod
    def from_dict(cls, d: DictConfig | dict) -> "SymlinkRule":
        raw = OmegaConf.to_container(d) if isinstance(d, DictConfig) else d
        return cls(symlink=str(raw["symlink"]), target=str(raw["target"]))


# ------------------------------------------------------------------ #
# Resolution — unified output of Schema.resolve()                     #
# ------------------------------------------------------------------ #


@dataclass
class Resolution:
    """Full result of resolving a single path against a Schema."""

    mapped: Path | None           # None means the file is omitted
    matched: bool                 # True when a rule fired and changed the path
    crammed: bool                 # True when the cram fallback fired
    symlink_targets: list[Path]   # additional packed-relative paths to create as symlinks


# ------------------------------------------------------------------ #
# Schema                                                               #
# ------------------------------------------------------------------ #

FallbackMode = Literal["passthrough", "omit", "cram"]


@dataclass
class Schema:
    """Loaded, validated schema ready to apply forward mappings."""

    name: str
    description: str
    rules: list[Rule]
    fallback: FallbackMode = "passthrough"
    crampath: str = ""  # destination bucket for unmatched files when fallback="cram"
    symlink_rules: list[SymlinkRule] = field(default_factory=list)

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
        rules_raw: list[dict[str, Any]] = OmegaConf.to_container(s.rules, resolve=True)  # type: ignore[assignment]

        standard_rules: list[Rule] = []
        symlink_rules: list[SymlinkRule] = []
        for raw in rules_raw:
            if "symlink" in raw:
                symlink_rules.append(SymlinkRule.from_dict(raw))
            else:
                standard_rules.append(Rule.from_dict(raw))

        # Validate that every symlink reference resolves to a known rule id.
        known_ids = {rule.id for rule in standard_rules if rule.id}
        for sr in symlink_rules:
            if sr.symlink not in known_ids:
                raise ValueError(
                    f"Symlink rule references unknown rule id '{sr.symlink}'. "
                    f"Known ids: {sorted(known_ids) or '(none)'}."
                )

        fallback: FallbackMode = s.get("fallback", "passthrough")
        if fallback not in ("passthrough", "omit", "cram"):
            raise ValueError(
                f"'fallback' must be 'passthrough', 'omit', or 'cram', got '{fallback}'."
            )

        crampath = str(s.get("crampath", ""))
        if fallback == "cram" and not crampath:
            raise ValueError(
                "'crampath' must be set when fallback='cram'."
            )

        return cls(
            name=str(s.name),
            description=str(s.get("description", "")),
            rules=standard_rules,
            fallback=fallback,
            crampath=crampath,
            symlink_rules=symlink_rules,
        )

    # ------------------------------------------------------------------ #
    # Core mapping                                                         #
    # ------------------------------------------------------------------ #

    def resolve(self, rel_path: Path) -> Resolution:
        """
        Fully resolve *rel_path*: apply rules, collect symlink targets.

        This is the primary entry point for ``pack`` and ``diff``.
        """
        for rule in self.rules:
            result = rule.apply(rel_path)
            if result is None:
                continue
            mapped, groups, suffix = result
            sym_targets: list[Path] = []
            for sr in self.symlink_rules:
                if sr.symlink == rule.id:
                    sym_base = Path(sr.target.format(**groups))
                    sym_targets.append(sym_base / suffix if suffix else sym_base)
            return Resolution(
                mapped=mapped,
                matched=mapped != rel_path,
                crammed=False,
                symlink_targets=sym_targets,
            )

        # No rule matched — apply fallback.
        if self.fallback == "passthrough":
            return Resolution(mapped=rel_path, matched=False, crammed=False, symlink_targets=[])
        if self.fallback == "cram":
            return Resolution(
                mapped=Path(self.crampath) / rel_path,
                matched=False,
                crammed=True,
                symlink_targets=[],
            )
        return Resolution(mapped=None, matched=False, crammed=False, symlink_targets=[])

    def forward(self, rel_path: Path) -> Path | None:
        """Thin wrapper around ``resolve`` for backward compatibility."""
        return self.resolve(rel_path).mapped

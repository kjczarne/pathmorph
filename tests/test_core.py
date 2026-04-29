"""
Tests for pathmorph core operations.

Uses only tmp_path (pytest built-in) — no extra fixtures required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pathmorph.collision import CollisionAbort, CollisionStrategy
from pathmorph.core import diff, pack, unpack, verify
from pathmorph.manifest import MANIFEST_FILENAME
from pathmorph.schemas import Schema


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

SCHEMA_YAML = """\
schema:
  name: test_schema
  description: Schema used in unit tests
  fallback: passthrough
  rules:
    - pattern: 'runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\\.tsv'
      target: experiments/{exp}/candidates/{variant}/developability_scores.tsv
    - pattern: 'runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/(?P<rest>.+)'
      target: experiments/{exp}/candidates/{variant}/{rest}
"""

SCHEMA_YAML_OMIT = """\
schema:
  name: omit_schema
  description: Schema that omits unmatched files
  fallback: omit
  rules:
    - pattern: 'runs/(?P<exp>[^/]+)/scores\\.tsv'
      target: experiments/{exp}/scores.tsv
"""


@pytest.fixture()
def schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "schema.yaml"
    p.write_text(SCHEMA_YAML)
    return p


@pytest.fixture()
def omit_schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "omit_schema.yaml"
    p.write_text(SCHEMA_YAML_OMIT)
    return p


@pytest.fixture()
def src_dir(tmp_path: Path) -> Path:
    """A small source directory tree."""
    src = tmp_path / "src"
    files = {
        "runs/exp_001/A/scores.tsv": "score_data\n",
        "runs/exp_001/A/report.txt": "report\n",
        "runs/exp_002/B/scores.tsv": "score_data_2\n",
        "unrelated/config.yaml": "key: value\n",
    }
    for rel, content in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return src


# ------------------------------------------------------------------ #
# Schema loading                                                        #
# ------------------------------------------------------------------ #

class TestSchemaLoading:
    def test_loads_yaml(self, schema_file: Path) -> None:
        s = Schema.from_file(schema_file)
        assert s.name == "test_schema"
        assert len(s.rules) == 2

    def test_missing_schema_key_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: oops\n")
        with pytest.raises(ValueError, match="top-level 'schema' key"):
            Schema.from_file(bad)

    def test_bad_fallback_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("schema:\n  name: x\n  fallback: delete\n  rules: []\n")
        with pytest.raises(ValueError, match="fallback"):
            Schema.from_file(bad)


# ------------------------------------------------------------------ #
# Rule.apply                                                           #
# ------------------------------------------------------------------ #

class TestRuleApply:
    def test_matching_rule(self, schema_file: Path) -> None:
        schema = Schema.from_file(schema_file)
        result = schema.forward(Path("runs/exp_001/A/scores.tsv"))
        assert result == Path("experiments/exp_001/candidates/A/developability_scores.tsv")

    def test_passthrough_fallback(self, schema_file: Path) -> None:
        schema = Schema.from_file(schema_file)
        result = schema.forward(Path("unrelated/config.yaml"))
        assert result == Path("unrelated/config.yaml")

    def test_omit_fallback(self, omit_schema_file: Path) -> None:
        schema = Schema.from_file(omit_schema_file)
        result = schema.forward(Path("unrelated/config.yaml"))
        assert result is None


# ------------------------------------------------------------------ #
# diff                                                                  #
# ------------------------------------------------------------------ #

class TestDiff:
    def test_diff_no_filesystem_change(self, src_dir: Path, schema_file: Path) -> None:
        schema = Schema.from_file(schema_file)
        records = diff(src_dir, schema)
        # filesystem untouched
        assert (src_dir / "runs" / "exp_001" / "A" / "scores.tsv").exists()
        assert len(records) == 4  # all 4 source files

    def test_diff_identifies_remapped(self, src_dir: Path, schema_file: Path) -> None:
        schema = Schema.from_file(schema_file)
        records = diff(src_dir, schema)
        remapped = [r for r in records if r.matched]
        assert len(remapped) == 3  # 2 scores.tsv + 1 report.txt


# ------------------------------------------------------------------ #
# pack                                                                  #
# ------------------------------------------------------------------ #

class TestPack:
    def test_basic_pack(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        result = pack(src_dir, dst, schema=schema)

        assert (dst / MANIFEST_FILENAME).exists()
        assert (
            dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        ).exists()

    def test_manifest_is_valid_json(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        data = json.loads((dst / MANIFEST_FILENAME).read_text())
        assert data["version"] == 1
        assert isinstance(data["entries"], list)

    def test_source_unchanged_after_copy(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        assert (src_dir / "runs" / "exp_001" / "A" / "scores.tsv").exists()

    def test_collision_abort(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        with pytest.raises(CollisionAbort):
            pack(src_dir, dst, schema=schema, collision=CollisionStrategy.ABORT)

    def test_collision_skip(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        # Modify a file to confirm skip doesn't overwrite
        target = dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        target.write_text("modified\n")
        pack(src_dir, dst, schema=schema, collision=CollisionStrategy.SKIP)
        assert target.read_text() == "modified\n"

    def test_collision_overwrite(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        target = dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        target.write_text("modified\n")
        pack(src_dir, dst, schema=schema, collision=CollisionStrategy.OVERWRITE)
        assert target.read_text() == "score_data\n"

    def test_omit_fallback(self, src_dir: Path, omit_schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(omit_schema_file)
        result = pack(src_dir, dst, schema=schema)
        assert result.omitted_count > 0
        assert not (dst / "unrelated" / "config.yaml").exists()


# ------------------------------------------------------------------ #
# unpack (roundtrip)                                                   #
# ------------------------------------------------------------------ #

class TestUnpack:
    def _roundtrip(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> Path:
        dst = tmp_path / "packed"
        restored = tmp_path / "restored"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        unpack(dst, restored)
        return restored

    def test_roundtrip_files_present(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        restored = self._roundtrip(src_dir, schema_file, tmp_path)
        assert (restored / "runs/exp_001/A/scores.tsv").exists()
        assert (restored / "runs/exp_002/B/scores.tsv").exists()

    def test_roundtrip_content_identical(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        restored = self._roundtrip(src_dir, schema_file, tmp_path)
        original = src_dir / "runs/exp_001/A/scores.tsv"
        restored_f = restored / "runs/exp_001/A/scores.tsv"
        assert original.read_text() == restored_f.read_text()

    def test_unpack_missing_manifest_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            unpack(empty, tmp_path / "out")


# ------------------------------------------------------------------ #
# verify                                                               #
# ------------------------------------------------------------------ #

class TestVerify:
    def test_verify_clean_pack(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        result = verify(dst)
        assert result.passed

    def test_verify_detects_tamper(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack(src_dir, dst, schema=schema)
        # Tamper with a file
        target = dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        target.write_text("tampered!\n")
        result = verify(dst)
        assert not result.passed
        assert len(result.failed) == 1

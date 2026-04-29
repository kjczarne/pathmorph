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

SCHEMA_YAML_CRAM = """\
schema:
  name: cram_schema
  description: Schema that cramsunmatched files into a bucket
  fallback: cram
  crampath: _uncategorized
  rules:
    - pattern: 'runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\\.tsv'
      target: experiments/{exp}/candidates/{variant}/developability_scores.tsv
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
def cram_schema_file(tmp_path: Path) -> Path:
    p = tmp_path / "cram_schema.yaml"
    p.write_text(SCHEMA_YAML_CRAM)
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
        records = diff([src_dir], schema)
        # filesystem untouched
        assert (src_dir / "runs" / "exp_001" / "A" / "scores.tsv").exists()
        assert len(records) == 4  # all 4 source files

    def test_diff_identifies_remapped(self, src_dir: Path, schema_file: Path) -> None:
        schema = Schema.from_file(schema_file)
        records = diff([src_dir], schema)
        remapped = [r for r in records if r.matched]
        assert len(remapped) == 3  # 2 scores.tsv + 1 report.txt


# ------------------------------------------------------------------ #
# pack                                                                  #
# ------------------------------------------------------------------ #

class TestPack:
    def test_basic_pack(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        result = pack([src_dir], dst, schema=schema)

        assert (dst / MANIFEST_FILENAME).exists()
        assert (
            dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        ).exists()

    def test_manifest_is_valid_json(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        data = json.loads((dst / MANIFEST_FILENAME).read_text())
        assert data["version"] == 1
        assert isinstance(data["entries"], list)

    def test_source_unchanged_after_copy(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        assert (src_dir / "runs" / "exp_001" / "A" / "scores.tsv").exists()

    def test_collision_abort(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        with pytest.raises(CollisionAbort):
            pack([src_dir], dst, schema=schema, collision=CollisionStrategy.ABORT)

    def test_collision_skip(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        # Modify a file to confirm skip doesn't overwrite
        target = dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        target.write_text("modified\n")
        pack([src_dir], dst, schema=schema, collision=CollisionStrategy.SKIP)
        assert target.read_text() == "modified\n"

    def test_collision_overwrite(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        target = dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        target.write_text("modified\n")
        pack([src_dir], dst, schema=schema, collision=CollisionStrategy.OVERWRITE)
        assert target.read_text() == "score_data\n"

    def test_omit_fallback(self, src_dir: Path, omit_schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(omit_schema_file)
        result = pack([src_dir], dst, schema=schema)
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
        pack([src_dir], dst, schema=schema)
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
# multi-source pack / unpack                                           #
# ------------------------------------------------------------------ #

SCHEMA_YAML_MULTI = """\
schema:
  name: multi_schema
  description: Schema for multi-source tests
  fallback: passthrough
  rules:
    - pattern: 'data/(?P<file>.+)'
      target: 'inputs/{file}'
    - pattern: 'outputs/(?P<file>.+)'
      target: 'results/{file}'
"""


class TestMultiSource:
    @pytest.fixture()
    def multi_schema_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "multi_schema.yaml"
        p.write_text(SCHEMA_YAML_MULTI)
        return p

    @pytest.fixture()
    def data_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "data"
        (d / "scores.tsv").parent.mkdir(parents=True, exist_ok=True)
        (d / "scores.tsv").write_text("s1\n")
        (d / "config.yaml").write_text("k: v\n")
        return d

    @pytest.fixture()
    def outputs_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "result.json").write_text("{}\n")
        return d

    def test_multi_source_pack(
        self, data_dir: Path, outputs_dir: Path, multi_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(multi_schema_file)
        result = pack([data_dir, outputs_dir], dst, schema=schema)

        assert (dst / "inputs/scores.tsv").exists()
        assert (dst / "inputs/config.yaml").exists()
        assert (dst / "results/result.json").exists()
        assert result.omitted_count == 0

    def test_multi_source_roundtrip(
        self, data_dir: Path, outputs_dir: Path, multi_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        restored = tmp_path / "restored"
        schema = Schema.from_file(multi_schema_file)
        pack([data_dir, outputs_dir], dst, schema=schema)
        unpack(dst, restored)

        # source labels are relative paths (e.g. "tmp/.../data"), so we
        # check that each original file lands under its source subdirectory
        assert any((restored / data_dir.name).rglob("scores.tsv"))
        assert any((restored / outputs_dir.name).rglob("result.json"))

    def test_multi_source_manifest_has_source_root(
        self, data_dir: Path, outputs_dir: Path, multi_schema_file: Path, tmp_path: Path
    ) -> None:
        import json as _json
        dst = tmp_path / "packed"
        schema = Schema.from_file(multi_schema_file)
        pack([data_dir, outputs_dir], dst, schema=schema)
        data = _json.loads((dst / MANIFEST_FILENAME).read_text())
        roots = {e["source_root"] for e in data["entries"]}
        assert len(roots) == 2  # one label per source

    def test_single_source_source_root_empty(
        self, data_dir: Path, multi_schema_file: Path, tmp_path: Path
    ) -> None:
        import json as _json
        dst = tmp_path / "packed"
        schema = Schema.from_file(multi_schema_file)
        pack([data_dir], dst, schema=schema)
        data = _json.loads((dst / MANIFEST_FILENAME).read_text())
        assert all(e["source_root"] == "" for e in data["entries"])


# ------------------------------------------------------------------ #
# cram fallback                                                         #
# ------------------------------------------------------------------ #

class TestCramFallback:
    def test_unmatched_files_land_in_crampath(
        self, src_dir: Path, cram_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(cram_schema_file)
        pack([src_dir], dst, schema=schema)

        # scores.tsv files are remapped by the rule
        assert (dst / "experiments/exp_001/candidates/A/developability_scores.tsv").exists()
        # report.txt and config.yaml have no matching rule → crammed
        assert (dst / "_uncategorized/runs/exp_001/A/report.txt").exists()
        assert (dst / "_uncategorized/unrelated/config.yaml").exists()

    def test_cram_records_marked(
        self, src_dir: Path, cram_schema_file: Path, tmp_path: Path
    ) -> None:
        schema = Schema.from_file(cram_schema_file)
        records = diff([src_dir], schema)
        crammed = [r for r in records if r.crammed]
        remapped = [r for r in records if r.matched]
        # both scores.tsv match the rule; report.txt and config.yaml are crammed
        assert len(crammed) == 2
        assert len(remapped) == 2
        assert all(not r.omitted for r in crammed)
        assert all(not r.matched for r in crammed)

    def test_cram_roundtrip(
        self, src_dir: Path, cram_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        restored = tmp_path / "restored"
        schema = Schema.from_file(cram_schema_file)
        pack([src_dir], dst, schema=schema)
        unpack(dst, restored)
        assert (restored / "runs/exp_001/A/scores.tsv").exists()
        assert (restored / "unrelated/config.yaml").exists()

    def test_cram_missing_crampath_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "schema:\n  name: x\n  fallback: cram\n  rules: []\n"
        )
        with pytest.raises(ValueError, match="crampath"):
            Schema.from_file(bad)

    def test_bad_fallback_includes_cram_in_message(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("schema:\n  name: x\n  fallback: delete\n  rules: []\n")
        with pytest.raises(ValueError, match="fallback"):
            Schema.from_file(bad)


# ------------------------------------------------------------------ #
# verify                                                               #
# ------------------------------------------------------------------ #

class TestVerify:
    def test_verify_clean_pack(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        result = verify(dst)
        assert result.passed

    def test_verify_detects_tamper(self, src_dir: Path, schema_file: Path, tmp_path: Path) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src_dir], dst, schema=schema)
        # Tamper with a file
        target = dst / "experiments/exp_001/candidates/A/developability_scores.tsv"
        target.write_text("tampered!\n")
        result = verify(dst)
        assert not result.passed
        assert len(result.failed) == 1

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

    def test_directory_prefix_match_appends_suffix(self, tmp_path: Path) -> None:
        """A pattern targeting a directory renames the tree and preserves subtree paths."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/fp'\n"
            "      target: '04_Shortlist/outputs/FinalPicks'\n"
        )
        schema = Schema.from_file(schema_file)
        assert schema.forward(Path("outputs/o01/fp/a.txt")) == Path("04_Shortlist/outputs/FinalPicks/a.txt")
        assert schema.forward(Path("outputs/o01/fp/sub/b.txt")) == Path("04_Shortlist/outputs/FinalPicks/sub/b.txt")

    def test_directory_prefix_exact_match(self, tmp_path: Path) -> None:
        """Pattern matches a path with no trailing components → target returned as-is."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'raw'\n      target: 'inputs'\n"
        )
        schema = Schema.from_file(schema_file)
        assert schema.forward(Path("raw")) == Path("inputs")

    def test_implicit_name_variable(self, tmp_path: Path) -> None:
        """Glob pattern + {__name__} sends matched files into a directory bucket."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/o\\d+_docked.*'\n"
            "      target: '03_Modeler/outputs/{__name__}'\n"
        )
        schema = Schema.from_file(schema_file)
        assert schema.forward(Path("outputs/o01/o01_docked_result.txt")) == Path(
            "03_Modeler/outputs/o01_docked_result.txt"
        )
        assert schema.forward(Path("outputs/o02/o02_docked_other.csv")) == Path(
            "03_Modeler/outputs/o02_docked_other.csv"
        )

    def test_implicit_stem_and_suffix_variables(self, tmp_path: Path) -> None:
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'raw/(?P<run>[^/]+)/.*'\n"
            "      target: 'processed/{run}/{__stem__}.parquet'\n"
        )
        schema = Schema.from_file(schema_file)
        assert schema.forward(Path("raw/r01/data.csv")) == Path(
            "processed/r01/data.parquet"
        )

    def test_glob_preserves_subdirectory_structure(self, tmp_path: Path) -> None:
        """'.*' pattern auto-finds shortest prefix, preserving subdirectory structure."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: omit\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/o\\d+_docked.*'\n"
            "      target: '03_Modeler/outputs'\n"
        )
        src = tmp_path / "src"
        (src / "outputs/o000/o000_docked/all_pdbs").mkdir(parents=True)
        (src / "outputs/o000/o000_docked/other").mkdir(parents=True)
        (src / "outputs/o000/o000_docked/all_pdbs/a.pdb").write_text("a")
        (src / "outputs/o000/o000_docked/all_pdbs/b.pdb").write_text("b")
        (src / "outputs/o000/o000_docked/other/c.txt").write_text("c")

        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src], dst, schema=schema)

        # Subdirectory structure is preserved beneath the target
        assert (dst / "03_Modeler/outputs/all_pdbs/a.pdb").read_text() == "a"
        assert (dst / "03_Modeler/outputs/all_pdbs/b.pdb").read_text() == "b"
        assert (dst / "03_Modeler/outputs/other/c.txt").read_text() == "c"

    def test_glob_different_docked_dirs_dont_mix(self, tmp_path: Path) -> None:
        """Two o*_docked directories land in separate subtrees, not merged."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: omit\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/(?P<docked>o\\d+_docked[^/]*)'\n"
            "      target: '03_Modeler/outputs/{docked}'\n"
        )
        src = tmp_path / "src"
        (src / "outputs/o000/o000_docked/all_pdbs").mkdir(parents=True)
        (src / "outputs/o000/o000_docked_v2/all_pdbs").mkdir(parents=True)
        (src / "outputs/o000/o000_docked/all_pdbs/a.pdb").write_text("a")
        (src / "outputs/o000/o000_docked_v2/all_pdbs/b.pdb").write_text("b")

        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src], dst, schema=schema)

        assert (dst / "03_Modeler/outputs/o000_docked/all_pdbs/a.pdb").exists()
        assert (dst / "03_Modeler/outputs/o000_docked_v2/all_pdbs/b.pdb").exists()

    def test_glob_without_name_overwrites(self, tmp_path: Path) -> None:
        """Without {__name__}, a glob pattern maps every file to the same target."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/o\\d+_docked.*'\n"
            "      target: '03_Modeler/outputs'\n"
        )
        schema = Schema.from_file(schema_file)
        # Both files resolve to the same path — this is the bug the user reported
        r1 = schema.forward(Path("outputs/o01/o01_docked_a.txt"))
        r2 = schema.forward(Path("outputs/o01/o01_docked_b.txt"))
        assert r1 == r2 == Path("03_Modeler/outputs")

    def test_pack_with_name_variable_no_collision(self, tmp_path: Path) -> None:
        """Each matched file lands at its own path in the bucket directory."""
        src = tmp_path / "src"
        (src / "outputs/o01").mkdir(parents=True)
        (src / "outputs/o01/o01_docked_a.txt").write_text("a")
        (src / "outputs/o01/o01_docked_b.txt").write_text("b")

        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: omit\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/o\\d+_docked.*'\n"
            "      target: '03_Modeler/outputs/{__name__}'\n"
        )
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        result = pack([src], dst, schema=schema)

        assert (dst / "03_Modeler/outputs/o01_docked_a.txt").read_text() == "a"
        assert (dst / "03_Modeler/outputs/o01_docked_b.txt").read_text() == "b"
        assert result.omitted_count == 0

    def test_partial_component_not_matched(self, tmp_path: Path) -> None:
        """Pattern 'data' must not match 'database/file.txt'."""
        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'data'\n      target: 'inputs'\n"
        )
        schema = Schema.from_file(schema_file)
        # 'database/...' shares a prefix with 'data' but is a different component
        result = schema.forward(Path("database/file.txt"))
        assert result == Path("database/file.txt")  # passthrough, not remapped


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

    def test_directory_pattern_copies_subtree(self, tmp_path: Path) -> None:
        """A rule matching a directory prefix recursively remaps all files beneath it."""
        src = tmp_path / "src"
        (src / "outputs/o01/fp/sub").mkdir(parents=True)
        (src / "outputs/o01/fp/a.txt").write_text("a")
        (src / "outputs/o01/fp/sub/b.txt").write_text("b")

        schema_file = tmp_path / "s.yaml"
        schema_file.write_text(
            "schema:\n  name: t\n  fallback: passthrough\n  rules:\n"
            "    - pattern: 'outputs/(?P<order>o\\d+)/fp'\n"
            "      target: '04_Shortlist/outputs/FinalPicks'\n"
        )
        dst = tmp_path / "packed"
        schema = Schema.from_file(schema_file)
        pack([src], dst, schema=schema)

        assert (dst / "04_Shortlist/outputs/FinalPicks/a.txt").exists()
        assert (dst / "04_Shortlist/outputs/FinalPicks/sub/b.txt").exists()


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
# symlink rules                                                         #
# ------------------------------------------------------------------ #

SCHEMA_YAML_SYMLINK = """\
schema:
  name: symlink_schema
  description: Schema with a symlink rule
  fallback: passthrough
  rules:
    - id: scores
      pattern: 'runs/(?P<exp>[^/]+)/(?P<variant>[^/]+)/scores\\.tsv'
      target: experiments/{exp}/candidates/{variant}/developability_scores.tsv
    - symlink: scores
      target: latest/{exp}/{variant}/scores.tsv
"""

SCHEMA_YAML_SYMLINK_DIR = """\
schema:
  name: symlink_dir_schema
  description: Symlink rule on a directory-prefix match
  fallback: passthrough
  rules:
    - id: fp
      pattern: 'outputs/(?P<order>o\\d+)/fp'
      target: '04_Shortlist/{order}/FinalPicks'
    - symlink: fp
      target: 'links/{order}/fp'
"""


class TestSymlinkRules:
    @pytest.fixture()
    def symlink_schema_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "symlink_schema.yaml"
        p.write_text(SCHEMA_YAML_SYMLINK)
        return p

    @pytest.fixture()
    def symlink_dir_schema_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "symlink_dir_schema.yaml"
        p.write_text(SCHEMA_YAML_SYMLINK_DIR)
        return p

    def test_symlink_created_in_packed_output(
        self, src_dir: Path, symlink_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(symlink_schema_file)
        result = pack([src_dir], dst, schema=schema)

        # Real file exists
        assert (dst / "experiments/exp_001/candidates/A/developability_scores.tsv").exists()
        # Symlink exists and points to the real file
        sym = dst / "latest/exp_001/A/scores.tsv"
        assert sym.is_symlink()
        assert sym.resolve() == (dst / "experiments/exp_001/candidates/A/developability_scores.tsv").resolve()

    def test_symlink_count_in_result(
        self, src_dir: Path, symlink_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        schema = Schema.from_file(symlink_schema_file)
        result = pack([src_dir], dst, schema=schema)
        # Two scores.tsv files → two symlinks
        assert result.symlink_count == 2

    def test_symlink_not_in_manifest(
        self, src_dir: Path, symlink_schema_file: Path, tmp_path: Path
    ) -> None:
        import json as _json
        dst = tmp_path / "packed"
        schema = Schema.from_file(symlink_schema_file)
        pack([src_dir], dst, schema=schema)
        data = _json.loads((dst / MANIFEST_FILENAME).read_text())
        packed_paths = {e["packed"] for e in data["entries"]}
        assert not any("latest" in p for p in packed_paths)

    def test_symlink_dropped_on_unpack(
        self, src_dir: Path, symlink_schema_file: Path, tmp_path: Path
    ) -> None:
        dst = tmp_path / "packed"
        restored = tmp_path / "restored"
        schema = Schema.from_file(symlink_schema_file)
        pack([src_dir], dst, schema=schema)
        unpack(dst, restored)
        # Original files restored, symlink paths not present
        assert (restored / "runs/exp_001/A/scores.tsv").exists()
        assert not (restored / "latest").exists()

    def test_symlink_directory_prefix(
        self, tmp_path: Path, symlink_dir_schema_file: Path
    ) -> None:
        src = tmp_path / "src"
        (src / "outputs/o01/fp/sub").mkdir(parents=True)
        (src / "outputs/o01/fp/a.txt").write_text("a")
        (src / "outputs/o01/fp/sub/b.txt").write_text("b")

        dst = tmp_path / "packed"
        schema = Schema.from_file(symlink_dir_schema_file)
        pack([src], dst, schema=schema)

        # Real files remapped
        assert (dst / "04_Shortlist/o01/FinalPicks/a.txt").exists()
        assert (dst / "04_Shortlist/o01/FinalPicks/sub/b.txt").exists()
        # Symlinks mirror the subtree
        sym_a = dst / "links/o01/fp/a.txt"
        sym_b = dst / "links/o01/fp/sub/b.txt"
        assert sym_a.is_symlink()
        assert sym_b.is_symlink()
        assert sym_a.resolve() == (dst / "04_Shortlist/o01/FinalPicks/a.txt").resolve()
        assert sym_b.resolve() == (dst / "04_Shortlist/o01/FinalPicks/sub/b.txt").resolve()

    def test_symlink_diff_records(
        self, src_dir: Path, symlink_schema_file: Path
    ) -> None:
        schema = Schema.from_file(symlink_schema_file)
        records = diff([src_dir], schema)
        sym_records = [r for r in records if r.symlink_targets]
        assert len(sym_records) == 2  # two scores.tsv files

    def test_invalid_symlink_reference_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "schema:\n  name: x\n  fallback: passthrough\n  rules:\n"
            "    - symlink: nonexistent\n      target: foo/bar\n"
        )
        with pytest.raises(ValueError, match="nonexistent"):
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

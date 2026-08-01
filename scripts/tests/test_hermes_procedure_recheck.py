"""Tests for scripts/hermes_procedure_recheck.py (LIA-511).

Hermetic: every test builds a throwaway skills/procedures tree under pytest's
tmp_path and monkeypatches the module's path constants — nothing touches the
real ~/deus/.claude/skills or the real auto-memory directory.

measure-precision's Ollama-dependent query generation is not unit-tested here
(network/model dependency, same reasoning as the plan's own "not unit-testable
this way" note) — covered by the manual before/after run documented in the
ticket instead.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "hermes_procedure_recheck.py"


def _load():
    spec = importlib.util.spec_from_file_location("hermes_procedure_recheck", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_procedure_recheck"] = mod
    spec.loader.exec_module(mod)
    return mod


hpr = _load()


def _mk_skill(skills_dir: Path, name: str, skill_md_body: str | None = "description: x") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True)
    if skill_md_body is not None:
        (d / "SKILL.md").write_text(skill_md_body)
    return d


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    deus_dir = tmp_path / ".deus"
    snapshot_path = deus_dir / "hermes_skill_classification_snapshot.json"
    monkeypatch.setattr(hpr, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(hpr, "DEUS_DIR", deus_dir)
    monkeypatch.setattr(hpr, "SNAPSHOT_PATH", snapshot_path)
    # Isolate from the real 65-entry LIA-509 table so a test's small,
    # deliberately-incomplete skills_dir doesn't spuriously report every
    # real skill it didn't create as "removed" (see the module's own
    # _seed_snapshot_if_absent, which seeds from _LIA_509_BUCKETS alone).
    # Tests that care about specific buckets override this per-test.
    monkeypatch.setattr(hpr, "_LIA_509_BUCKETS", {"add-asana": "PORTABLE"})
    return skills_dir, deus_dir, snapshot_path


# ── detect-drift ─────────────────────────────────────────────────────────────

def test_seeding_preserves_existing_measurement_history(
    _isolate, monkeypatch: pytest.MonkeyPatch
):
    # Regression test for a real data-loss bug (verification-gate finding):
    # _seed_snapshot_if_absent() used to REASSIGN `snapshot = {"skills": ...}`
    # wholesale instead of merging into it - silently destroying
    # `last_measurement`/`consecutive_drop_count` from a prior
    # measure-precision run on every re-seed (including the corrupt/partial
    # -read fallback, since _load_snapshot() also returns {} in that case,
    # routing through this same path). Reproduced against this project's own
    # real persisted 94.1%/94.1% record before being fixed.
    skills_dir, _, snapshot_path = _isolate
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    existing_measurement = {
        "recall_at_1": 0.9411764705882353,
        "veto_rate": 0.9411764705882353,
        "node_count": 17,
        "measured_at": "2026-08-01T09:40:21.013381+00:00",
    }
    snapshot_path.write_text(json.dumps({
        "last_measurement": existing_measurement,
        "consecutive_drop_count": 0,
    }))
    _mk_skill(skills_dir, "add-asana")

    hpr.detect_drift()

    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["last_measurement"] == existing_measurement
    assert snapshot["consecutive_drop_count"] == 0
    assert "skills" in snapshot  # the actual seeding still happened


def test_detect_drift_seeds_snapshot_on_first_run(_isolate):
    skills_dir, _, snapshot_path = _isolate
    _mk_skill(skills_dir, "add-asana")

    report = hpr.detect_drift()

    assert snapshot_path.is_file()
    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["skills"]["add-asana"]["bucket"] == "PORTABLE"
    # First run: add-asana is both LIA-509-known and currently present, so no
    # drift for it.
    assert report["new"] == []
    assert report["changed"] == []
    assert report["removed"] == []


def test_detect_drift_flags_unclassified_skill_as_new_even_on_first_run(_isolate):
    # Regression test for a real bug (GPT-backend code-review round 2): an
    # earlier fix seeded the baseline from the UNION of current dirs and
    # _LIA_509_BUCKETS — which fixed "removed" detection but broke "new"
    # detection the same way the original bug broke it, since every
    # currently-present skill (classified or not) ended up IN the baseline
    # by construction, so `current - known` could never be non-empty on a
    # first run. Fixed: seed from _LIA_509_BUCKETS alone, not unioned with
    # current state, so an unclassified skill is correctly "new" immediately.
    skills_dir, _, _ = _isolate
    _mk_skill(skills_dir, "add-asana")
    _mk_skill(skills_dir, "unknown-skill")  # not in _LIA_509_BUCKETS at all

    report = hpr.detect_drift()

    assert report["new"] == ["unknown-skill"]


def test_detect_drift_flags_removed_skill_even_on_first_run(
    _isolate, monkeypatch: pytest.MonkeyPatch
):
    # Regression test for a real bug (GPT-backend code-review finding): the
    # original _seed_snapshot_if_absent() seeded ONLY from currently-present
    # directories, so it always seeded-then-compared against the same state
    # — a skill LIA-509 classified but that no longer exists could never be
    # reported as removed on the very first run, only on a SECOND run after
    # a prior seed. Fixed: seed from _LIA_509_BUCKETS alone (see the module
    # docstring for why a union-based fix was tried and superseded), so a
    # first run can detect this immediately.
    skills_dir, _, _ = _isolate
    monkeypatch.setattr(hpr, "_LIA_509_BUCKETS", {
        "add-asana": "PORTABLE",
        "long-gone-skill": "PORTABLE",  # classified by LIA-509, no dir exists now
    })
    _mk_skill(skills_dir, "add-asana")
    # Deliberately do NOT create "long-gone-skill" — this IS the first run.

    report = hpr.detect_drift()

    assert report["removed"] == ["long-gone-skill"]
    assert report["new"] == []


def test_detect_drift_flags_new_skill(_isolate):
    skills_dir, _, _ = _isolate
    _mk_skill(skills_dir, "add-asana")
    hpr.detect_drift()  # seed

    _mk_skill(skills_dir, "brand-new-skill")
    report = hpr.detect_drift()

    assert report["new"] == ["brand-new-skill"]
    assert report["changed"] == []
    assert report["removed"] == []


def test_detect_drift_flags_changed_skill_md_hash(_isolate):
    skills_dir, _, _ = _isolate
    d = _mk_skill(skills_dir, "add-asana", "description: original")
    hpr.detect_drift()  # seed

    (d / "SKILL.md").write_text("description: totally different now")
    report = hpr.detect_drift()

    assert report["changed"] == ["add-asana"]
    assert report["new"] == []


def test_detect_drift_flags_removed_skill(_isolate):
    skills_dir, _, _ = _isolate
    d = _mk_skill(skills_dir, "add-asana")
    hpr.detect_drift()  # seed

    import shutil
    shutil.rmtree(d)
    report = hpr.detect_drift()

    assert report["removed"] == ["add-asana"]


def test_detect_drift_handles_skill_with_no_skill_md(_isolate):
    skills_dir, _, _ = _isolate
    _mk_skill(skills_dir, "ui-ux-pro-max", skill_md_body=None)

    report = hpr.detect_drift()  # must not raise

    # Not in the (patched, small) _LIA_509_BUCKETS -> correctly flagged as
    # needing classification, same as any other unclassified skill.
    assert report["new"] == ["ui-ux-pro-max"]


# ── mark-import-complete ─────────────────────────────────────────────────────

def test_mark_import_complete_stamps_snapshot(_isolate):
    _, _, snapshot_path = _isolate
    result = hpr.mark_import_complete()

    assert "lia510_completed_at" in result
    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["lia510_completed_at"] == result["lia510_completed_at"]


# ── prune-candidates ─────────────────────────────────────────────────────────

def _mk_procedure_node(procedures_dir: Path, name: str, *, source_skill: str | None,
                        description: str = "how to do the thing") -> Path:
    procedures_dir.mkdir(parents=True, exist_ok=True)
    path = procedures_dir / f"{name}.md"
    fm_lines = ["---", "id: 01TEST", "kind: procedure", "type: procedure",
                f"title: {name}", f"description: {description}"]
    if source_skill:
        fm_lines.append(f"source_skill: {source_skill}")
    fm_lines.append("---")
    path.write_text("\n".join(fm_lines) + "\n\n## Steps\n\n1. do it\n")
    return path


def test_prune_candidates_no_warning_when_import_not_yet_marked_complete(
    _isolate, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    procedures_dir = tmp_path / "automem" / "procedures"
    monkeypatch.setattr(hpr, "resolve_auto_memory_dir", lambda: tmp_path / "automem")
    # Nodes with no source_skill: at all — LIA-510 simply hasn't run yet.
    _mk_procedure_node(procedures_dir, "pre-existing-node", source_skill=None)

    report = hpr.prune_candidates()

    assert report["warning"] is None
    assert report["tagged_count"] == 0


def test_prune_candidates_warns_loudly_when_import_marked_complete_but_untagged(
    _isolate, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    procedures_dir = tmp_path / "automem" / "procedures"
    monkeypatch.setattr(hpr, "resolve_auto_memory_dir", lambda: tmp_path / "automem")
    hpr.mark_import_complete()
    _mk_procedure_node(procedures_dir, "pre-existing-node", source_skill=None)

    report = hpr.prune_candidates()

    assert report["warning"] is not None
    assert "ZERO procedure nodes" in report["warning"]


def test_prune_candidates_flags_node_that_does_not_self_surface(
    _isolate, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    procedures_dir = tmp_path / "automem" / "procedures"
    monkeypatch.setattr(hpr, "resolve_auto_memory_dir", lambda: tmp_path / "automem")
    node = _mk_procedure_node(procedures_dir, "grilling", source_skill="grilling")

    def fake_recall(query, *, k=1, source="unknown", **kwargs):
        return {"paths": ["/some/other/node.md"], "context": "", "confidence": 0.1, "fell_back": True}

    monkeypatch.setattr(hpr.memory_query, "recall", fake_recall)

    report = hpr.prune_candidates()

    assert report["tagged_count"] == 1
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["path"] == str(node)


def test_prune_candidates_does_not_flag_node_that_self_surfaces_top1(
    _isolate, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    procedures_dir = tmp_path / "automem" / "procedures"
    monkeypatch.setattr(hpr, "resolve_auto_memory_dir", lambda: tmp_path / "automem")
    node = _mk_procedure_node(procedures_dir, "grilling", source_skill="grilling")

    def fake_recall(query, *, k=1, source="unknown", **kwargs):
        return {"paths": [str(node)], "context": "x", "confidence": 0.9, "fell_back": False}

    monkeypatch.setattr(hpr.memory_query, "recall", fake_recall)

    report = hpr.prune_candidates()

    assert report["candidates"] == []


def test_resolve_result_path_strips_auto_memory_namespace_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # Regression test for a real bug found via a live run against the actual
    # corpus (not caught by the mocked unit tests above): memory_query.recall()
    # returns auto-memory-namespace paths as "auto-memory/<relpath>" strings,
    # NOT absolute filesystem paths - naively calling Path(x).resolve() on one
    # resolves against cwd instead of the real auto-memory dir, silently
    # failing every top-1 comparison even when retrieval is correct.
    automem = tmp_path / "automem"
    monkeypatch.setattr(hpr, "resolve_auto_memory_dir", lambda: automem)

    resolved = hpr._resolve_result_path("auto-memory/procedures/prune-merged-worktrees.md")

    assert resolved == automem / "procedures" / "prune-merged-worktrees.md"


def test_resolve_result_path_passes_through_non_namespaced_paths(tmp_path: Path):
    absolute = str(tmp_path / "some" / "vault" / "note.md")
    assert hpr._resolve_result_path(absolute) == Path(absolute)


def test_prune_candidates_handles_auto_memory_namespace_result_path(
    _isolate, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # End-to-end version of the regression above, through prune_candidates()
    # itself rather than the helper in isolation.
    automem = tmp_path / "automem"
    procedures_dir = automem / "procedures"
    monkeypatch.setattr(hpr, "resolve_auto_memory_dir", lambda: automem)
    _mk_procedure_node(procedures_dir, "grilling", source_skill="grilling")

    def fake_recall(query, *, k=1, source="unknown", **kwargs):
        return {"paths": ["auto-memory/procedures/grilling.md"], "context": "x",
                "confidence": 0.9, "fell_back": False}

    monkeypatch.setattr(hpr.memory_query, "recall", fake_recall)

    report = hpr.prune_candidates()

    assert report["candidates"] == []

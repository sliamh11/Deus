"""
Completeness pin for evolution/reflexion/store.py's save_reflection() call
sites (LIA-1011).

AST-walks the evolution/ package (excluding tests/) for every call to
save_reflection(...) and classifies each by whether it passes an
`interaction_id=` keyword ("interaction-keyed" writer). The counts below are
PINNED: 10 interaction-keyed writers (the 9 pre-existing sites -- cc_backfill.py
x2, cli.py x3, maintenance.py x2, backfill.py x1, mcp_server.py x1 -- plus
maintenance.process_human_feedback()'s own call, a 10th) and 1 non-interaction-keyed
writer (reflexion/principles.py's cross-group extraction, explicitly out of
scope for polarity).

Every interaction-keyed writer must also pass a `polarity=` keyword. If a
future writer is added without updating BOTH this file's pinned counts AND
its own polarity kwarg, this test fails -- it cannot silently regress to an
untagged writer.
"""
import ast
from pathlib import Path

EVOLUTION_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_INTERACTION_KEYED = 10
EXPECTED_NON_INTERACTION_KEYED = 1


def _iter_evolution_py_files():
    for path in EVOLUTION_ROOT.rglob("*.py"):
        if "tests" in path.relative_to(EVOLUTION_ROOT).parts:
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _is_save_reflection_call(node: ast.Call) -> bool:
    """Match calls to the module-level reflexion.store.save_reflection(...)
    wrapper, called bare (`save_reflection(...)`) as every caller does via
    `from .reflexion.store import save_reflection`.

    Deliberately does NOT match attribute calls like `store.save_reflection(...)`
    -- that's the StorageProvider.save_reflection() method (a different,
    lower-level function with the same name), called exactly once, from
    inside reflexion/store.py's own save_reflection() wrapper. Matching it
    would double-count that single writer.
    """
    func = node.func
    return isinstance(func, ast.Name) and func.id == "save_reflection"


def _find_save_reflection_calls():
    """Return a list of (path, lineno, keyword_names) for every save_reflection(...) call."""
    calls = []
    for path in _iter_evolution_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_save_reflection_call(node):
                kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
                calls.append((path, node.lineno, kw_names))
    return calls


def test_save_reflection_call_site_counts_are_pinned():
    calls = _find_save_reflection_calls()
    interaction_keyed = [c for c in calls if "interaction_id" in c[2]]
    non_interaction_keyed = [c for c in calls if "interaction_id" not in c[2]]

    assert len(interaction_keyed) == EXPECTED_INTERACTION_KEYED, (
        f"Expected {EXPECTED_INTERACTION_KEYED} interaction-keyed save_reflection( "
        f"call sites, found {len(interaction_keyed)}: "
        f"{[(str(p.relative_to(EVOLUTION_ROOT)), ln) for p, ln, _ in interaction_keyed]}. "
        "A new writer was added (or removed) -- review whether it should "
        "thread polarity, then update this pin."
    )
    assert len(non_interaction_keyed) == EXPECTED_NON_INTERACTION_KEYED, (
        f"Expected {EXPECTED_NON_INTERACTION_KEYED} non-interaction-keyed "
        f"save_reflection( call site (reflexion/principles.py), found "
        f"{len(non_interaction_keyed)}: "
        f"{[(str(p.relative_to(EVOLUTION_ROOT)), ln) for p, ln, _ in non_interaction_keyed]}."
    )


def test_every_interaction_keyed_call_site_passes_polarity():
    calls = _find_save_reflection_calls()
    interaction_keyed = [c for c in calls if "interaction_id" in c[2]]
    missing_polarity = [
        (p, ln) for p, ln, kw in interaction_keyed if "polarity" not in kw
    ]
    assert not missing_polarity, (
        "The following interaction-keyed save_reflection( call sites do not "
        f"pass polarity=: {[(str(p.relative_to(EVOLUTION_ROOT)), ln) for p, ln in missing_polarity]}. "
        "Every interaction-keyed writer must thread its zone (corrective/"
        "positive) so maintenance.process_human_feedback()'s zone-alignment "
        "archival can reconcile contradicting reflections (LIA-1011)."
    )

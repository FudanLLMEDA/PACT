"""Unit tests for the time-budget guard and scope-degradation helper.

Covers the cold-design transferability fix: a clock_tighten sweep that stops at
the first legal candidate must not be charged the multi-candidate time floor,
and a degradable skill that does not fit at full scope must degrade to a single
candidate rather than be abandoned.
"""
import types

from FDAgents.agent import _time_budget_blocks_skill, _fit_skill_scope_to_budget


def _mem(remaining):
    return types.SimpleNamespace(budget={"time_remaining": float(remaining)})


def test_stop_after_first_legal_uses_single_candidate_floor():
    p = {"max_candidates": 2, "stop_after_first_legal": True, "factors": [0.92, 0.88]}
    # Without the fix this is charged the 1200s multi floor and blocked at 1000s.
    assert _time_budget_blocks_skill(_mem(1000), "clock_tighten", p) is False
    # The single-candidate floor (900s) still applies.
    assert _time_budget_blocks_skill(_mem(800), "clock_tighten", p) is True


def test_multi_candidate_without_stop_first_still_blocked():
    p = {"max_candidates": 3, "factors": [0.92, 0.88, 0.84]}
    assert _time_budget_blocks_skill(_mem(1000), "clock_tighten", p) is True
    assert _time_budget_blocks_skill(_mem(1300), "clock_tighten", p) is False


def test_fit_degrades_multi_candidate_to_single_when_blocked():
    p = {"max_candidates": 3, "factors": [0.92, 0.88, 0.84], "recipes": ["a", "b"]}
    fitted = _fit_skill_scope_to_budget(_mem(1000), "clock_tighten", p)
    assert fitted["max_candidates"] == 1
    assert fitted["factors"] == [0.92]
    assert fitted["recipes"] == ["a"]
    assert _time_budget_blocks_skill(_mem(1000), "clock_tighten", fitted) is False


def test_fit_returns_original_when_even_single_cannot_fit():
    p = {"max_candidates": 3, "factors": [0.92, 0.88]}
    fitted = _fit_skill_scope_to_budget(_mem(800), "clock_tighten", p)
    assert fitted["max_candidates"] == 3  # unchanged; caller's guard still blocks


def test_fit_noop_for_non_degradable_skill():
    p = {"max_candidates": 5}
    assert _fit_skill_scope_to_budget(_mem(100), "phys_opt", p) == p


def test_fit_noop_when_already_fits():
    p = {"max_candidates": 3, "factors": [0.92, 0.88, 0.84]}
    fitted = _fit_skill_scope_to_budget(_mem(3000), "clock_tighten", p)
    assert fitted["max_candidates"] == 3


def test_fit_degrades_fresh_place_route_max_candidates():
    # full (mc=2) blocked by the >1 / <2400 floor at 1500s; single fits (>=1200s)
    p = {"max_candidates": 2}
    assert _time_budget_blocks_skill(_mem(1500), "fresh_place_route", p) is True
    fitted = _fit_skill_scope_to_budget(_mem(1500), "fresh_place_route", p)
    assert fitted["max_candidates"] == 1
    assert _time_budget_blocks_skill(_mem(1500), "fresh_place_route", fitted) is False


def test_fit_degrades_post_route_cleanup_iterations():
    # full (8 candidates x 2 iters = 16 attempts) blocked at 2000s; 1x1 fits
    p = {"max_candidates": 8, "max_iterations": 2}
    assert _time_budget_blocks_skill(_mem(2000), "post_route_cleanup", p) is True
    fitted = _fit_skill_scope_to_budget(_mem(2000), "post_route_cleanup", p)
    assert fitted["max_candidates"] == 1
    assert fitted["max_iterations"] == 1
    assert _time_budget_blocks_skill(_mem(2000), "post_route_cleanup", fitted) is False


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

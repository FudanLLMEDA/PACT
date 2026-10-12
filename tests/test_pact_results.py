import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from FDAgents import agent
from FDAgents.results import sha256, write_summary
from FDAgents.validation import capture_constraints, classify_reports, clock_lines, tcl_word, validate_checkpoint, check_functional

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pact_summary", ROOT / "scripts/summarize_pact_results.py")
SUMMARY = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SUMMARY)

ROUTE = """# of routable nets : 10
# of fully routed nets : 10
# of nets with routing errors : 0
"""


def timing(wns="-0.1", whs="0.02", wpws="0.03"):
    return f"WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints WPWS(ns) TPWS(ns) TPWS Failing Endpoints TPWS Total Endpoints\n-----\n{wns} -1.0 1 10 {whs} 0.0 0 10 {wpws} 0.0 0 10\n"


def test_gate_does_not_treat_negative_setup_as_hold_failure():
    result = classify_reports(ROUTE, timing())
    assert result["passed"]
    assert result["timing"]["wns"] == -0.1
    assert result["functional"] == "not_checked"
    assert result["formal_equivalence"] == "not_checked"


@pytest.mark.parametrize("route,report", [
    ("", timing()), (ROUTE.replace("routed nets : 10", "routed nets : 9"), timing()),
    (ROUTE, ""), (ROUTE, timing(whs="-0.01")), (ROUTE, timing(wpws="-0.01")),
    (ROUTE, timing(whs="NA")), (ROUTE, timing(wpws="NA")), (ROUTE, timing(wns="NA")),
])
def test_gate_fails_closed(route, report):
    assert not classify_reports(route, report)["passed"]


def test_tcl_word_quotes_substitutions():
    assert tcl_word('a$[b]"\\c') == '"a\\$\\[b]\\"\\\\c"'


def test_snapshot_ignores_console_noise():
    assert clock_lines("noise\nPACT_CLOCK clk 10 {0 5} 0\nother") == ["PACT_CLOCK clk 10 {0 5} 0"]


class FakeMCP:
    def __init__(self, clock_change=False):
        self.calls = []
        self.clock_change = clock_change

    async def call_vivado(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if name == "run_tcl":
            cmd = arguments["command"]
            if "report_timing_summary" in cmd:
                return timing()
            if "reset_timing" in cmd:
                clock = "20" if self.clock_change else "10"
                return f"PACT_CLOCK clk {clock} {{0 5}} 0\nPACT_CONSTRAINTS_RESTORED"
            return "PACT_CLOCK clk 10 {0 5} 0"
        if name == "report_route_status":
            return ROUTE
        if name == "write_checkpoint":
            Path(arguments["dcp_path"]).write_text("DCP")
        return "ok"


def test_gate_restores_then_writes_and_reopens(tmp_path):
    mcp = FakeMCP()
    result = asyncio.run(validate_checkpoint(mcp, tmp_path / "candidate.dcp", tmp_path / "normal.dcp",
        {"xdc": str(tmp_path / "original.xdc"), "clocks": ["PACT_CLOCK clk 10 {0 5} 0"]}, tmp_path / "evidence"))
    assert result["passed"]
    assert [name for name, _ in mcp.calls] == ["open_checkpoint", "run_tcl", "write_checkpoint", "open_checkpoint", "run_tcl", "report_route_status", "run_tcl"]
    assert "reset_timing\nread_xdc" in mcp.calls[1][1]["command"]
    assert json.loads((tmp_path / "evidence/validation.json").read_text())["checks"]["checkpoint_reopen"] == "passed"


def test_clock_mismatch_never_exports(tmp_path):
    mcp = FakeMCP(clock_change=True)
    result = asyncio.run(validate_checkpoint(mcp, tmp_path / "candidate.dcp", tmp_path / "normal.dcp",
        {"xdc": "original.xdc", "clocks": ["PACT_CLOCK clk 10 {0 5} 0"]}, tmp_path))
    assert not result["passed"]
    assert not (tmp_path / "normal.dcp").exists()


def test_capture_requires_real_xdc(tmp_path):
    with pytest.raises(RuntimeError):
        asyncio.run(capture_constraints(FakeMCP(), tmp_path))


def test_atomic_summary_and_hash(tmp_path):
    p = tmp_path / "summary.json"
    write_summary(p, {"status": "running"})
    write_summary(p, {"status": "failed"})
    assert json.loads(p.read_text()) == {"status": "failed"}
    assert not p.with_suffix(".json.tmp").exists()
    assert len(sha256(p)) == 64


def manifest():
    return [{"example": "a", "split": "train", "dcp_relpath": "benchmarks/a.dcp", "orig_fmax_csv_mhz": "100"},
            {"example": "b", "split": "test", "dcp_relpath": "benchmarks/b.dcp", "orig_fmax_csv_mhz": "100"}]


def completed():
    v = classify_reports(ROUTE, timing())
    v["checks"].update(constraints="passed", checkpoint_reopen="passed")
    v["functional"] = "passed"
    return {"schema_version": 1, "method": "pact", "input_dcp": "/some/host/a.dcp", "status": "completed",
            "validation": v, "final": {"fmax_mhz": 120}}


def test_aggregation_preserves_missing_coverage():
    rows, report = SUMMARY.aggregate(manifest(), [completed()])
    assert rows[1]["status"] == "missing"
    group = report["groups"]["all"]
    assert group["scored"] == 1 and not group["complete"]
    assert group["geomean_improvement_pct"] == pytest.approx(20)


@pytest.mark.parametrize("change", ["status", "functional", "hold", "reopen"])
def test_aggregation_excludes_unverified(change):
    run = completed()
    if change == "status":
        run["status"] = "failed"
    elif change == "functional":
        run["validation"]["functional"] = "not_checked"
    else:
        key = "hold" if change == "hold" else "checkpoint_reopen"
        run["validation"]["checks"][key] = "missing"
    rows, report = SUMMARY.aggregate(manifest(), [run])
    assert not rows[0]["eligible"]
    assert report["groups"]["all"]["geomean_improvement_pct"] is None


def test_duplicate_runs_and_invalid_metrics_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        SUMMARY.aggregate(manifest(), [completed(), completed()])
    run = completed()
    run["final"]["fmax_mhz"] = float("nan")
    with pytest.raises(ValueError, match="Invalid Fmax"):
        SUMMARY.aggregate(manifest(), [run])


def test_failed_skill_cannot_be_accepted():
    assert not agent._accepts_result(SimpleNamespace(success=False, is_legal=True, delta_wns=1.0))


def test_startup_failure_writes_summary_without_export(tmp_path, monkeypatch):
    dcp = tmp_path / "input.dcp"
    dcp.write_bytes(b"input")
    fake = SimpleNamespace(start_servers=AsyncMock(side_effect=RuntimeError("not installed")), cleanup=AsyncMock())
    monkeypatch.setattr(agent, "MCPClient", lambda *a, **kw: fake)
    out = tmp_path / "output.dcp"
    rc = asyncio.run(agent.run(dcp, out, use_llm=False, run_dir=tmp_path / "run"))
    assert rc == 1 and not out.exists()
    summary = json.loads((tmp_path / "run/summary.json").read_text())
    assert summary["status"] == "failed" and summary["input_sha256"] == sha256(dcp)


def test_functional_missing_report_is_failure_even_with_zero_exit(tmp_path, monkeypatch):
    proc = SimpleNamespace(wait=AsyncMock(return_value=0))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = asyncio.run(check_functional(tmp_path / "in.dcp", tmp_path / "out.dcp", tmp_path, 10))
    assert result["status"] == "failed"


def test_functional_skipped_report_is_not_pass(tmp_path, monkeypatch):
    (tmp_path / "functional.json").write_text(json.dumps({"phase1_passed": True, "phase2_passed": True, "phase2_skipped": True}))
    proc = SimpleNamespace(wait=AsyncMock(return_value=0))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = asyncio.run(check_functional(tmp_path / "in.dcp", tmp_path / "out.dcp", tmp_path, 10))
    assert result["status"] == "failed"


@pytest.mark.parametrize("functional,skip,gate_pass,expected_status,expected_rc,exported", [
    ("passed", False, True, "completed", 0, True),
    ("failed", False, True, "validation_failed", 2, False),
    ("timeout", False, True, "validation_failed", 2, False),
    ("infrastructure_failed", False, True, "validation_failed", 2, False),
    ("not_checked", True, True, "functional_not_checked", 0, True),
    ("passed", False, False, "validation_failed", 2, False),
])
def test_final_export_contract(tmp_path, monkeypatch, functional, skip, gate_pass, expected_status, expected_rc, exported):
    dcp = tmp_path / "input.dcp"
    dcp.write_bytes(b"input")
    fake = SimpleNamespace(start_servers=AsyncMock(), cleanup=AsyncMock())
    monkeypatch.setattr(agent, "MCPClient", lambda *a, **kw: fake)
    monkeypatch.setattr(agent, "initial_analysis", AsyncMock(return_value={
        "baseline_wns": -0.1, "clock_period": 10.0, "fanout_nets": [], "fanout_count": 0}))
    monkeypatch.setattr(agent, "capture_constraints", AsyncMock(return_value={"xdc": "original.xdc", "clocks": []}))
    monkeypatch.setattr(agent, "_deterministic_analysis_from_initial", lambda *a, **kw: {"bottleneck": "mixed"})
    monkeypatch.setattr(agent, "MAX_ROUNDS", 0)

    async def gate(mcp, candidate, normalized, constraints, evidence):
        evidence.mkdir(parents=True, exist_ok=True)
        normalized.write_bytes(b"normalized")
        result = classify_reports(ROUTE, timing())
        result["passed"] = gate_pass
        return result

    monkeypatch.setattr(agent, "validate_checkpoint", gate)
    functional_mock = AsyncMock(return_value={"status": functional})
    monkeypatch.setattr(agent, "check_functional", functional_mock)
    out = tmp_path / "output.dcp"
    rc = asyncio.run(agent.run(dcp, out, use_llm=False, run_dir=tmp_path / "run",
                              skip_functional_check=skip, recipe_timeout_scale=2, elaboration_timeout=5400))
    assert rc == expected_rc
    assert out.exists() is exported
    summary = json.loads((tmp_path / "run/summary.json").read_text())
    assert summary["status"] == expected_status
    assert summary["recipe_timeout_scale"] == 2
    assert summary["elaboration_timeout_s"] == 5400
    assert (summary["output_dcp"] is not None) is exported
    if not gate_pass or skip:
        functional_mock.assert_not_awaited()
    else:
        assert functional_mock.await_args.kwargs["elaboration_timeout"] == 5400


def test_validator_does_not_return_success_for_skipped_simulation(tmp_path):
    from validate_dcps import DCPValidator
    validator = DCPValidator(tmp_path / "in.dcp", tmp_path / "out.dcp", work_dir=tmp_path)
    validator.phase1_structural_checks = AsyncMock(return_value=True)
    validator.phase2_functional_simulation = AsyncMock(return_value={"status": "skipped", "reason": "encrypted IP"})
    validator.print_final_report = lambda elapsed: None
    assert asyncio.run(validator.validate()) is False
    assert validator.phase2_skipped is True


def test_nan_report_is_missing_and_json_serializable():
    result = classify_reports(ROUTE, timing(wns="NaN"))
    assert not result["passed"]
    assert result["checks"]["setup"] == "missing"
    json.dumps(result, allow_nan=False)


def test_llm_dispatch_uses_selected_target_recipe(tmp_path, monkeypatch):
    import copy
    from FDAgents.recipe_planner import HIGH_UTIL_TARGET_PERIOD_CLOCK_TIGHTEN_PARAMS
    dcp = tmp_path / "input.dcp"
    dcp.write_bytes(b"input")
    fake_mcp = SimpleNamespace(start_servers=AsyncMock(), cleanup=AsyncMock())
    monkeypatch.setattr(agent, "MCPClient", lambda *a, **kw: fake_mcp)
    monkeypatch.setattr(agent, "initial_analysis", AsyncMock(return_value={
        "baseline_wns": -0.1, "clock_period": 10.0, "fanout_nets": [],
        "fanout_count": 0, "timing_summary": "synthetic"}))
    monkeypatch.setattr(agent, "capture_constraints", AsyncMock(return_value={"xdc": "original.xdc", "clocks": []}))
    class FakeLLM(agent.RuleOnlyLLMStats):
        def call(self, task, memory, extra=None):
            if task == "analyze_timing":
                return {"bottleneck": "mixed", "suggested_skill_order": ["clock_tighten"],
                        "initial_params": {"clock_tighten": copy.deepcopy(HIGH_UTIL_TARGET_PERIOD_CLOCK_TIGHTEN_PARAMS)}}
            return {"skill": "clock_tighten", "target": "target_period_fast_route_floor", "params": {}}
    monkeypatch.setattr(agent, "LLMClient", lambda **kw: FakeLLM())
    monkeypatch.setattr(agent.rules, "quick_filter", lambda mem: None)
    monkeypatch.setattr(agent, "_action_override_reason", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "MAX_ROUNDS", 1)
    captured = {}
    async def execute(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after inspecting dispatched parameters")
    monkeypatch.setitem(agent.SKILLS, "clock_tighten", SimpleNamespace(execute=execute))
    rc = asyncio.run(agent.run(dcp, tmp_path / "out.dcp", run_dir=tmp_path / "run", force_llm_analysis=True))
    assert rc == 1
    params = captured["params"]
    assert [r["name"] for r in params["recipes"]] == ["target_period_fast_route_floor"]
    assert params["recipe_timeout_s"] == 3000
    assert "target_params" not in params


@pytest.mark.parametrize("column", [6, 10])
def test_rounded_slack_cannot_hide_failing_hold_or_pulse_endpoints(column):
    report = timing(whs="-0.000", wpws="-0.000").splitlines()
    values = report[2].split()
    values[column] = "1"
    report[2] = " ".join(values)
    assert not classify_reports(ROUTE, "\n".join(report))["passed"]


def test_recipe_timeout_scale_preserves_priors_and_other_parameters():
    params = {"recipe_timeout_s": 3000, "min_remaining_s": 2400, "max_candidates": 1}
    assert agent._scale_recipe_timeout(params, 2) == {**params, "recipe_timeout_s": 6000}
    assert params["recipe_timeout_s"] == 3000
    assert agent._scale_recipe_timeout(params, 1) == params
    assert agent._scale_recipe_timeout({"max_candidates": 1}, 2) == {"max_candidates": 1}


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_runtime_controls_are_rejected(tmp_path, value):
    from validate_dcps import DCPValidator
    with pytest.raises(ValueError):
        agent._scale_recipe_timeout({}, value)
    with pytest.raises(ValueError):
        DCPValidator(tmp_path / "in.dcp", tmp_path / "out.dcp", work_dir=tmp_path,
                     elaboration_timeout=value)
    with pytest.raises(ValueError):
        asyncio.run(agent.run(tmp_path / "in.dcp", tmp_path / "out.dcp",
                             run_dir=tmp_path / "run", recipe_timeout_scale=value))
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("rc,stderr,killed", [
    (-9, "", True), (137, "tool terminated", True),
    (1, 'rdiArgs.sh: 718 Killed "$RDI_PROG" "$@"', True),
    (1, "compilation error", False),
])
def test_elaboration_failure_is_not_a_functional_mismatch(tmp_path, rc, stderr, killed):
    from validate_dcps import DCPValidator
    validator = DCPValidator(tmp_path / "in.dcp", tmp_path / "out.dcp", work_dir=tmp_path,
                             elaboration_timeout=5400)
    validator._record_tool_failure("xelab (elaboration)",
                                   SimpleNamespace(returncode=rc, stdout="", stderr=stderr))
    assert validator.infrastructure_failure
    assert not validator.phase2_passed
    assert validator.simulation_report["killed"] == killed
    assert validator.simulation_report["returncode"] == rc
    assert "mismatch_count" not in validator.simulation_report
    assert "out_of_memory" not in validator.infrastructure_reason  # SIGKILL alone is not OOM proof.


def test_functional_wrapper_passes_timeout_and_preserves_infrastructure_status(tmp_path, monkeypatch):
    calls = []
    async def create(*args, **kwargs):
        calls.append(args)
        (tmp_path / "functional.json").write_text(json.dumps({"infrastructure_failure": True}))
        return SimpleNamespace(wait=AsyncMock(return_value=2))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = asyncio.run(check_functional(tmp_path / "in.dcp", tmp_path / "out.dcp", tmp_path,
                                         7200, elaboration_timeout=5400))
    assert result["status"] == "infrastructure_failed"
    assert calls[0][-2:] == ("--elaboration-timeout", "5400")

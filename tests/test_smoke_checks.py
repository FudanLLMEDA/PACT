"""Regression tests for deterministic artifact smoke-test gates."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smoke_checks import (
    MCPToolPayloadError,
    check_mcp_tool_payload,
    require_route_clean,
    route_error_count,
)


def test_mcp_json_error_payload_is_fatal():
    try:
        check_mcp_tool_payload(
            '{"error": "java.lang.NullPointerException"}',
            "rapidwright_optimize_cell_placement",
        )
    except MCPToolPayloadError as exc:
        assert "NullPointerException" in str(exc)
    else:
        raise AssertionError("error payload was accepted")


def test_non_json_tool_output_is_allowed():
    assert check_mcp_tool_payload("Opened checkpoint: design.dcp", "open_checkpoint") is None


def test_clean_route_report_passes():
    report = """
    # of routable nets..................... : 28012
    # of fully routed nets................. : 28012
    # of nets with routing errors.......... : 0
    """
    assert route_error_count(report) == 0
    require_route_clean(report)


def test_routing_errors_are_fatal():
    report = "# of nets with routing errors.......... : 2"
    try:
        require_route_clean(report)
    except RuntimeError as exc:
        assert "2 routing error" in str(exc)
    else:
        raise AssertionError("routing errors were accepted")


def test_missing_route_count_fails_closed():
    try:
        require_route_clean("route status unavailable")
    except ValueError as exc:
        assert "Could not parse" in str(exc)
    else:
        raise AssertionError("unparseable route report was accepted")


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")

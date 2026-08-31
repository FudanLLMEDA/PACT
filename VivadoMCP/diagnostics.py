"""Small dependency-free diagnostics shared by the Vivado MCP server."""


def tcl_completion_diagnostic(output_text: str, *, limit: int = 500) -> str:
    """Keep the terminal Tcl rejection visible ahead of a bounded raw tail."""
    lines = [line.strip() for line in str(output_text).splitlines() if line.strip()]
    if not lines:
        return "no Vivado completion output"
    diagnostic = lines[-1]
    if len(diagnostic) > limit:
        diagnostic = diagnostic[:limit]
    return diagnostic


def tcl_failure_diagnostic(output_text: str, *, limit: int = 1000) -> str:
    """Keep terminal Tcl errors even when verbose output pushes them past a tail."""
    lines = [line.strip() for line in str(output_text).splitlines() if line.strip()]
    if not lines:
        return "no Vivado completion output"
    error_lines = [
        line for line in lines
        if line.startswith("ERROR:") or line.startswith("CRITICAL WARNING:")
    ]
    selected = error_lines[-2:] if error_lines else lines[-1:]
    return " | ".join(selected)[:limit]

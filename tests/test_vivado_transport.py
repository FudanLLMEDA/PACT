"""Real Tcl + PTY transport regression tests; these are NOT Vivado/DCP tests."""
import asyncio
import importlib.util
from pathlib import Path
import shutil
import signal
import types
from unittest.mock import AsyncMock

import pexpect
import pytest

from FDAgents.mcp_client import MCPClient, MCPToolError, _detect_tool_error
from VivadoMCP.tcl_transport import TclTransport, TclCommandError

# Import actual MCP server registration too: mocks missed the incompatible
# mcp 2.x Server API. Do not replace pytest's signal handlers during import.
handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
try:
    from VivadoMCP import vivado_mcp_server as server
finally:
    for sig, handler in handlers.items():
        signal.signal(sig, handler)


@pytest.fixture
def transport():
    if not shutil.which('tclsh'):
        pytest.skip('tclsh required for real PTY regression; no Vivado needed')
    proc = pexpect.spawn('tclsh', encoding='utf-8', timeout=3)
    proc.expect('% ')
    proc.sendline('set tcl_prompt1 {puts -nonewline "Vivado% "}')
    proc.expect(server.VIVADO_PROMPT)
    transport = TclTransport(proc, server.VIVADO_PROMPT)
    try:
        yield transport
    finally:
        proc.terminate(force=True)
        transport.close()


def test_multiline_script_is_one_response(transport):
    assert transport.execute('set constraint 10\nputs "PACT_CLOCK clk 10 {0 5} 0"') == 'PACT_CLOCK clk 10 {0 5} 0'
    assert transport.execute('puts NEXT_RESPONSE') == 'NEXT_RESPONSE'


def test_prompt_text_in_tool_output_is_not_completion(transport):
    result = transport.execute('puts "report\\nVivado% "\nafter 40\nputs FINISHED')
    assert result == 'report\nVivado% \nFINISHED'
    assert transport.execute('expr {2+2}') == '4'


def test_comments_newlines_and_long_scripts_survive(transport):
    assert transport.execute('# comment\nset text {a\nb}\nputs $text') == 'a\nb'
    assert transport.execute('set long {' + 'x' * 20000 + '}\nstring length $long') == '20000'


def test_tcl_error_and_syntax_error_are_not_success(transport):
    with pytest.raises(TclCommandError, match='deliberate'):
        transport.execute('puts BEFORE\nerror deliberate\nputs SHOULD_NOT_RUN')
    assert transport.pending is None
    with pytest.raises(TclCommandError):
        transport.execute('set invalid {')
    assert transport.execute('puts RECOVERED') == 'RECOVERED'


def test_timeout_drains_previous_response_before_next_command(transport):
    with pytest.raises(pexpect.TIMEOUT):
        transport.execute('after 250\nputs OLD_RESPONSE', timeout=0.1)
    pending_file = transport.pending.path
    assert pending_file.exists()
    assert transport.execute('puts NEW_RESPONSE', timeout=3) == 'NEW_RESPONSE'
    assert not pending_file.exists()


def test_timeout_error_does_not_prevent_explicit_recovery(transport):
    with pytest.raises(pexpect.TIMEOUT):
        transport.execute('after 250\nerror OLD_FAILURE', timeout=0.1)
    assert transport.execute('puts REOPEN_RECOVERY', timeout=3) == 'REOPEN_RECOVERY'


def test_timeout_after_end_marker_retains_completed_response(transport):
    with pytest.raises(pexpect.TIMEOUT):
        transport.execute('set tcl_prompt1 {after 250; puts -nonewline "Vivado% "}\nputs DONE', timeout=0.15)
    assert transport.pending.output is not None
    assert transport.recover(timeout=3) == 'DONE'
    assert transport.execute('puts NEXT', timeout=3) == 'NEXT'


def test_eof_cleans_up_pending_script(transport):
    with pytest.raises(pexpect.EOF):
        transport.execute('exit')
    assert transport.pending is None


def test_checkpoint_reopen_queries_actual_project_not_python_flag(transport, monkeypatch):
    transport.execute('''set project_open 1
proc get_projects {args} {if {$::project_open} {return project}; return {}}
proc close_project {} {set ::project_open 0; return {}}
proc open_checkpoint {path} {
    if {$::project_open} {error "project already open"}
    if {$path eq "bad.dcp"} {error "invalid checkpoint"}
    set ::project_open 1
    return "OPENED:$path"
}''')
    monkeypatch.setattr(server, 'ensure_vivado', lambda: transport.process)
    monkeypatch.setattr(server, '_tcl_transport', transport)
    monkeypatch.setattr(server, '_design_open', False)
    monkeypatch.setattr(server, '_command_pending', False)
    response = asyncio.run(server.call_tool('open_checkpoint', {'dcp_path': 'input with space.dcp'}))
    assert 'OPENED:input with space.dcp' in response[0].text
    assert server._design_open is True
    response = asyncio.run(server.call_tool('open_checkpoint', {'dcp_path': 'bad.dcp'}))
    assert response[0].text.startswith('Error:')
    assert server._design_open is False
    with pytest.raises(MCPToolError):
        _detect_tool_error('VIV', 'open_checkpoint', response[0].text)


def test_client_forwards_backend_deadline_and_checks_mcp_error(tmp_path):
    client = MCPClient(tmp_path)
    result = types.SimpleNamespace(content=[types.SimpleNamespace(text='backend failure')], isError=True)
    client.vivado_session = types.SimpleNamespace(call_tool=AsyncMock(return_value=result))
    with pytest.raises(MCPToolError):
        asyncio.run(client.call_vivado('run_tcl', {'command': 'x'}, timeout=12))
    client.vivado_session.call_tool.assert_awaited_once_with('run_tcl', {'command': 'x', 'timeout': 12})


def test_datuner_relative_output_paths_are_resolved_before_chdir(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'baselines/datuner/run_local.py'
    spec = importlib.util.spec_from_file_location('datuner_local_path_test', path)
    datuner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(datuner)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'input.dcp').write_bytes(b'not a real DCP; path test only')
    monkeypatch.setattr(datuner, 'write_options_tcl', lambda *a, **kw: None)
    def subprocess_run(argv, **kwargs):
        cwd = Path(kwargs['cwd'])
        script = Path(argv[argv.index('-source') + 1])
        time_log = Path(argv[argv.index('-o') + 1])
        assert cwd.is_absolute() and script.is_absolute() and time_log.is_absolute()
        assert script.is_file()
        assert (cwd / 'design.dcp').resolve() == tmp_path / 'input.dcp'
        return types.SimpleNamespace(returncode=1, stdout='', stderr='intentional stop after path validation')
    monkeypatch.setattr(datuner.subprocess, 'run', subprocess_run)
    datuner.run_vivado('input.dcp', {}, 10, {}, Path('relative/output/trial'))


def test_real_mcp_stdio_server_multiline_and_error_boundary(tmp_path):
    """Actual MCP subprocess + Tcl interpreter, explicitly NOT FPGA validation."""
    if not shutil.which('tclsh'):
        pytest.skip('tclsh required for real transport regression')
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    tcl_loop = tmp_path / 'fake_vivado_repl.tcl'
    tcl_loop.write_text('''rename source original_source
proc source {args} {
    if {[lindex $args 0] eq "-notrace"} {set args [lrange $args 1 end]}
    uplevel 1 [linsert $args 0 original_source]
}
proc get_projects {args} {return {}}
proc close_project {} {return {}}
proc open_checkpoint {path} {return "FAKE_OPEN:$path"}
puts -nonewline "\\nVivado% "
flush stdout
set buffer ""
while {[gets stdin line] >= 0} {
    append buffer $line "\\n"
    if {![info complete $buffer]} {continue}
    set code [catch {uplevel #0 $buffer} result]
    if {$result ne ""} {puts $result}
    set buffer ""
    puts -nonewline "\\nVivado% "
    flush stdout
}
''')
    executable = tmp_path / 'fake-vivado'
    executable.write_text(f'#!/bin/sh\nexec "{shutil.which("tclsh")}" "{tcl_loop}"\n')
    executable.chmod(0o755)
    root = Path(__file__).resolve().parents[1]

    async def check():
        params = StdioServerParameters(command=sys.executable, args=[
            str(root / 'VivadoMCP/vivado_mcp_server.py'), '--vivado-path', str(executable)])
        with (tmp_path / 'mcp-stderr.log').open('w') as log:
            async with stdio_client(params, errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    client = MCPClient(tmp_path / 'run')
                    client.vivado_session = session
                    output = await client.call_vivado('run_tcl', {'command': 'puts FIRST\nputs SECOND'}, timeout=5)
                    assert 'FIRST\nSECOND' in output
                    with pytest.raises(MCPToolError, match='intentional_failure'):
                        await client.call_vivado('run_tcl', {'command': 'error intentional_failure'}, timeout=5)
                    output = await client.call_vivado('run_tcl', {'command': 'puts RECOVERED'}, timeout=5)
                    assert output.strip() == 'RECOVERED'
                    old_pid = await client.call_vivado('run_tcl', {'command': 'pid'}, timeout=5)
                    with pytest.raises(MCPToolError, match='timed out'):
                        await client.call_vivado('run_tcl', {'command': 'after 600000'}, timeout=0.1)
                    output = await client.call_vivado('open_checkpoint', {'dcp_path': 'saved.dcp'}, timeout=5)
                    assert 'FAKE_OPEN:saved.dcp' in output
                    new_pid = await client.call_vivado('run_tcl', {'command': 'pid'}, timeout=5)
                    assert new_pid != old_pid
    asyncio.run(check())


def test_large_fast_response_does_not_hide_begin_marker(transport):
    transport.process.maxread = 10000000
    transport.process.searchwindowsize = 64
    output = transport.execute('puts [string repeat x 100000]\nputs COMPLETE', timeout=3)
    assert output == 'x' * 100000 + '\nCOMPLETE'


def test_clock_period_ignores_echoed_commands_and_unrelated_numbers(tmp_path):
    client = MCPClient(tmp_path)
    client.call_vivado = AsyncMock(return_value='# set tp [get_timing_paths -max_paths 1 -setup]\nWARNING: 8 objects\nCLOCK:clk_fpl26contest\nPERIOD_NS:1.570\n')
    assert asyncio.run(client.get_clock_period()) == 1.570
    assert client.target_clock == 'clk_fpl26contest'


def test_clock_period_does_not_guess_without_tagged_measurement(tmp_path):
    client = MCPClient(tmp_path)
    client.call_vivado = AsyncMock(return_value='1.0\nWARNING: 8 objects')
    assert asyncio.run(client.get_clock_period()) is None


def test_backend_timeout_text_is_recognized_for_partial_checkpoint_recovery():
    from FDAgents.skills.clock_tighten import _exception_looks_like_timeout
    assert _exception_looks_like_timeout(MCPToolError('VIV', 'run_tcl', 'Error: Command timed out.'))
    assert _exception_looks_like_timeout(TimeoutError())
    assert not _exception_looks_like_timeout(RuntimeError('invalid checkpoint'))


def test_timed_out_backend_restarts_before_checkpoint_recovery(tmp_path):
    client = MCPClient(tmp_path)
    def response(text):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], isError=False)
    client.vivado_session = types.SimpleNamespace(call_tool=AsyncMock(side_effect=[
        response('Error: Command timed out. Vivado may be stuck.'),
        response('Vivado restarted successfully.'),
        response('Opened checkpoint: best.dcp')]))
    async def run():
        with pytest.raises(MCPToolError):
            await client.call_vivado('run_tcl', {'command': 'long operation'}, timeout=1)
        assert client._vivado_needs_restart
        await client.call_vivado('open_checkpoint', {'dcp_path': 'best.dcp'}, timeout=20)
        assert not client._vivado_needs_restart
    asyncio.run(run())
    assert [c.args[0] for c in client.vivado_session.call_tool.await_args_list] == ['run_tcl', 'restart_vivado', 'open_checkpoint']

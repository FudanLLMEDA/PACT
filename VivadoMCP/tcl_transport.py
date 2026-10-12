"""Frame one complete Tcl script per PTY request, including timeout recovery.

A prompt is not a command boundary when several lines are sent to a REPL.
Source a file with one physical input line instead, and require a unique
completion marker before consuming the prompt. Keep the frame after timeout.
"""
from dataclasses import dataclass
from pathlib import Path
import re
import logging
import tempfile
import time
import uuid

import pexpect


logger = logging.getLogger(__name__)


class TclCommandError(RuntimeError):
    """A complete, synchronized Tcl command returned a nonzero catch status."""


def tcl_word(value):
    text = str(value)
    for old, new in (("\\", "\\\\"), ('"', '\\"'), ("$", "\\$"), ("[", "\\["), ("\n", "\\n"), ("\r", "\\r")):
        text = text.replace(old, new)
    return '"' + text + '"'


@dataclass
class PendingCommand:
    path: Path
    token: str
    started: bool = False
    output: str | None = None
    status: int = 0


class TclTransport:
    def __init__(self, process, prompt, *, notrace=False):
        self.process = process
        self.prompt = prompt
        self.source_command = "source -notrace" if notrace else "source"
        self.pending = None

    def close(self):
        if self.pending:
            self.pending.path.unlink(missing_ok=True)
            self.pending = None

    def _receive(self, deadline):
        frame = self.pending
        def remaining():
            return max(0.0, deadline - time.monotonic())
        try:
            if not frame.started:
                self.process.expect(r"(?:^|\r*\n)" + frame.token + r"_BEGIN\r*\n", timeout=remaining(), searchwindowsize=None)
                frame.started = True
            if frame.output is None:
                self.process.expect(r"(?:^|\r*\n)" + frame.token + r"_END:([0-9]+)(?=\r*\n)", timeout=remaining(), searchwindowsize=None)
                frame.output = self.process.before
                frame.status = int(self.process.match.group(1))
            self.process.expect(self.prompt, timeout=remaining())
        except pexpect.EOF:
            self.close()
            raise
        # On TIMEOUT leave the file and frame intact: Tcl may still be using it.
        output, status = re.sub(r"\r+\n", "\n", frame.output).strip(), frame.status
        self.close()
        if status:
            raise TclCommandError(f"Tcl command failed (status {status}): {output}")
        return output

    def recover(self, timeout=3600):
        if self.pending is None:
            return ""
        return self._receive(time.monotonic() + timeout)

    def execute(self, command, timeout=300):
        deadline = time.monotonic() + timeout
        if self.pending is not None:
            try:
                self._receive(deadline)
            except TclCommandError as exc:
                # The earlier call already failed with a timeout. Drain its
                # eventual error, then allow an explicit reopen/recovery call.
                logger.warning("Previous timed-out command completed with an error: %s", exc)
        if time.monotonic() >= deadline:
            raise pexpect.TIMEOUT("Timeout while synchronizing the previous Tcl command")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tcl", prefix="pact-vivado-", delete=False, encoding="utf-8") as script:
            script.write(command)
        token = "PACT_" + uuid.uuid4().hex
        self.pending = PendingCommand(Path(script.name), token)
        code, result = f"::{token}_code", f"::{token}_result"
        # catch source errors too (including invalid Tcl syntax); never treat a
        # prompt or a partially executed script as a successful tool response.
        line = (f'puts "\\n{token}_BEGIN"; '
                f'set {code} [catch {{uplevel #0 [list {self.source_command} {tcl_word(script.name)}]}} {result}]; '
                f'if {{${result} ne ""}} {{puts ${result}}}; '
                f'puts "\\n{token}_END:${code}"; flush stdout; unset {code} {result}')
        try:
            self.process.sendline(line)
        except Exception:
            self.close()
            raise
        return self._receive(deadline)

"""Async subprocess terminal with a controlling PTY and bounded input writes."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import shutil
from pathlib import Path
import struct
import sys
import termios

# Run setup after execing Python, rather than preexec_fn in a threaded server.
_SETUP = ('import fcntl, os, sys, termios; '
          'fcntl.ioctl(0, termios.TIOCSCTTY, 0); '
          'os.execvpe(sys.argv[1], sys.argv[1:], os.environ)')


class _TerminalProtocol(asyncio.StreamReaderProtocol):
    def connection_lost(self, exc):
        # Linux PTYs report EIO when the last slave descriptor closes.
        if isinstance(exc, OSError) and exc.errno == errno.EIO:
            exc = None
        super().connection_lost(exc)


class TerminalInput:
    def __init__(self, fd):
        self.fd = fd
        self.pending = bytearray()
        self.closed = False

    def write(self, data):
        if not self.closed:
            self.pending.extend(data)

    async def drain(self):
        loop = asyncio.get_running_loop()
        while self.pending:
            try:
                count = os.write(self.fd, self.pending)
                del self.pending[:count]
            except BlockingIOError:
                ready = loop.create_future()
                def writable():
                    if not ready.done():
                        ready.set_result(None)
                loop.add_writer(self.fd, writable)
                try:
                    await ready
                finally:
                    loop.remove_writer(self.fd)

    def close(self):
        if not self.closed:
            # A terminal has no half-close; VEOF preserves the output side.
            self.pending.extend(b'\x04')
            self.closed = True

    def is_closing(self):
        return self.closed


class TerminalProcess:
    def __init__(self, proc, master, reader, transport):
        self.proc = proc
        self.master = master
        self.stdout = reader
        self.stderr = None
        self.stdin = TerminalInput(master)
        self.transport = transport

    @property
    def returncode(self):
        return self.proc.returncode

    async def wait(self):
        return await self.proc.wait()

    def resize(self, width, height):
        if not (0 < width <= 65535 and 0 < height <= 65535):
            raise ValueError('invalid terminal size')
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))

    def _signal(self, sig):
        # Include a foreground job, which an interactive shell puts in another
        # process group, as well as the PRoot/session leader.
        try:
            foreground = os.tcgetpgrp(self.master)
            if foreground > 0 and foreground != self.proc.pid:
                os.killpg(foreground, sig)
        except (ProcessLookupError, OSError):
            pass
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def terminate(self):
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(signal.SIGKILL)

    def close(self):
        if self.master is not None:
            self.transport.close()
            os.close(self.master)
            self.master = None


def terminal_host_command(command, env):
    """Launch Termux ELF files through Android's linker, without LD_PRELOAD.

    The PTY helper is a fresh Python process: it no longer has the original
    Termux shell's exec interceptor mapped. Android may deny direct exec of
    app-data binaries even when their executable mode bits are correct.
    """
    prefix = env.get('PREFIX')
    if not prefix or not command:
        return command
    executable = shutil.which(command[0], path=env.get('PATH'))
    if executable is None:
        return command
    path = Path(executable).resolve()
    try:
        path.relative_to(Path(prefix).resolve())
    except ValueError:
        return command
    try:
        with path.open('rb') as binary:
            header = binary.read(5)
    except OSError:
        return command
    if header[:4] != b'\x7fELF' or header[4:] not in (b'\x01', b'\x02'):
        return command
    linker = '/system/bin/linker64' if header[4] == 2 else '/system/bin/linker'
    if not Path(linker).is_file():
        return command
    return [linker, str(path), *command[1:]]


async def spawn_terminal(command, env=None):
    master, slave = pty.openpty()
    transport = None
    proc = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        terminal_env = dict(os.environ if env is None else env)
        terminal_env.setdefault('TERM', 'xterm')
        command = terminal_host_command(command, terminal_env)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-c', _SETUP, *command,
            stdin=slave, stdout=slave, stderr=slave,
            env=terminal_env, start_new_session=True,
        )
        os.close(slave)
        slave = None
        reader = asyncio.StreamReader()
        pipe = os.fdopen(os.dup(master), 'rb', buffering=0)
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: _TerminalProtocol(reader), pipe)
        except BaseException:
            pipe.close()
            raise
        return TerminalProcess(proc, master, reader, transport)
    except BaseException:
        if transport:
            transport.close()
        os.close(master)
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        if slave is not None:
            os.close(slave)

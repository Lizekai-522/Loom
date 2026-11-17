"""Explicit, local-user-owned interactive terminals with bounded transient output.

Windows uses ConPTY (pywinpty), POSIX uses a controlling PTY. No command or output
is written to the project database, no model is involved, and application secrets
are not inherited by the child shell. This is a user's local terminal, not an
agent sandbox, so it is deliberately unavailable on public/remote API requests.
"""
from collections import deque
import codecs
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import is_human
from .local_console import allows_local_console


class TerminalInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TerminalCreate(TerminalInput):
    project_id: str = Field(min_length=1, max_length=128)
    worktree_id: str | None = Field(default=None, min_length=1, max_length=80)
    cols: int = Field(default=100, ge=20, le=300)
    rows: int = Field(default=28, ge=5, le=120)


class TerminalWrite(TerminalInput):
    data: str = Field(min_length=1, max_length=16384)


class TerminalSize(TerminalInput):
    cols: int = Field(ge=20, le=300)
    rows: int = Field(ge=5, le=120)


def shell_environment():
    # An allowlist also excludes unknown future provider or application secrets.
    allowed = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
               'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA',
               'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'USERNAME',
               'COMPUTERNAME', 'OS', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
               'PSMODULEPATH', 'LANG', 'LC_ALL', 'SHELL', 'HOME'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(TERM='xterm-256color', COLORTERM='truecolor')
    return env


class WindowsJob:
    """A non-inherited job handle makes API exit and terminal close kill its tree."""

    def __init__(self, process_id):
        import ctypes
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('process_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class IOCounts(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Limits(ctypes.Structure):
            _fields_ = [('basic', BasicLimits), ('io', IOCounts),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        self.handle = k.CreateJobObjectW(None, None)
        process = None
        try:
            if not self.handle:
                raise OSError('TERMINAL_JOB_FAILED')
            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError('TERMINAL_JOB_FAILED')
            process = k.OpenProcess(0x0100 | 0x0001, False, process_id)
            if not process or not k.AssignProcessToJobObject(self.handle, process):
                raise OSError('TERMINAL_JOB_FAILED')
        except Exception:
            self.close()
            raise
        finally:
            if process:
                k.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _windows_terminal_host(connection, directory, cols, rows):
    """ConPTY native cleanup is isolated so it cannot freeze the API's GIL.

    The parent assigns this host to its terminal job before allowing shell
    creation. IPC carries native PTY bytes; this is not a redirected shell.
    """
    native = None
    try:
        if connection.recv() != {'type': 'start'}:
            return
        from winpty import PTY
        from winpty.enums import Backend
        shell = shutil.which('pwsh.exe') or str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                                                'System32/WindowsPowerShell/v1.0/powershell.exe')
        native = PTY(cols, rows, backend=Backend.ConPTY)
        env = '\0'.join(k + '=' + v for k, v in sorted(shell_environment().items())) + '\0'
        if not native.spawn(shell, cmdline=' -NoLogo -NoProfile -NoExit', cwd=directory, env=env):
            raise RuntimeError()
        connection.send(dict(type='ready', pid=native.pid))
        while True:
            while connection.poll():
                message = connection.recv()
                if message['type'] == 'close':
                    connection.send(dict(type='exit'))
                    return
                if message['type'] == 'input':
                    native.write(message['data'])
                elif message['type'] == 'resize':
                    native.set_size(message['cols'], message['rows'])
            data = native.read(blocking=False)
            if data:
                connection.send(dict(type='output', data=data))
            if not native.isalive():
                connection.send(dict(type='exit'))
                return
            time.sleep(.02)
    except Exception:
        try:
            connection.send(dict(type='error'))
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
        # pywinpty's destructor may wait on Windows console cleanup. The
        # parent's JobObject guarantees this host and all descendants can be
        # forcibly reclaimed without blocking the API.
        if native is not None:
            del native


class WindowsPty:
    shell = 'PowerShell'
    backend = 'conpty'

    def __init__(self, directory, cols, rows):
        import importlib.util
        import multiprocessing
        if importlib.util.find_spec('winpty') is None:
            raise HTTPException(503, 'TERMINAL_DEPENDENCY_MISSING')
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_windows_terminal_host,
            args=(child, str(directory), cols, rows), name='verilo-terminal-host', daemon=True)
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.closed, self.running, self.failed = False, True, False
        self.job = None
        self.process.start()
        child.close()
        try:
            self.job = WindowsJob(self.process.pid)
            self.connection.send(dict(type='start'))
            if not self.connection.poll(15):
                raise RuntimeError('TERMINAL_START_TIMEOUT')
            ready = self.connection.recv()
            if ready.get('type') != 'ready':
                raise RuntimeError('TERMINAL_START_FAILED')
            self.pid = ready['pid']
        except Exception:
            self.close()
            raise

    def read(self):
        with self.lock:
            if self.closed:
                return ''
            text = []
            while self.connection.poll() and len(text) < 16:
                try:
                    message = self.connection.recv()
                except (EOFError, OSError):
                    self.running = False
                    break
                kind = message.get('type')
                if kind == 'output':
                    text.append(message['data'])
                elif kind in {'exit', 'error'}:
                    self.running = False
                    self.failed = kind == 'error'
                    break
            return ''.join(text)

    def alive(self):
        with self.lock:
            return not self.closed and self.running and self.process.is_alive()

    def write(self, data):
        with self.send_lock:
            with self.lock:
                if self.closed or not self.running or not self.process.is_alive():
                    raise EOFError()
            # Input and output use different locks: a large paste must not
            # prevent the output reader from draining a full duplex PTY pipe.
            self.connection.send(dict(type='input', data=data))

    def resize(self, cols, rows):
        with self.send_lock:
            with self.lock:
                if self.closed or not self.running or not self.process.is_alive():
                    raise EOFError()
            self.connection.send(dict(type='resize', cols=cols, rows=rows))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed, self.running = True, False
        # Never wait for native ConPTY finalizers inside the API process.
        if self.job:
            self.job.close()
        if self.process.is_alive():
            self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=1)
        self.connection.close()
        if not self.process.is_alive():
            self.process.close()


class PosixPty:
    backend = 'posix-pty'

    def __init__(self, directory, cols, rows):
        import fcntl
        import pty
        import termios
        import struct
        self.master, slave = pty.openpty()
        self.lock = threading.Lock()
        self.closed = False
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        shell = shutil.which('bash') or '/bin/sh'
        self.shell = Path(shell).name
        args = ['--noprofile', '--norc', '-i'] if self.shell == 'bash' else ['-i']
        # A fresh child interpreter acquires the controlling terminal before
        # exec; no unsafe preexec_fn executes after a multithreaded API forks.
        child = ('import os,sys,fcntl,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);'
                 'os.execve(sys.argv[1],sys.argv[1:],os.environ)')
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        try:
            self.process = subprocess.Popen([sys.executable, '-c', child, shell, *args], cwd=directory,
                env=shell_environment(), stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True)
        except Exception:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.pid = self.process.pid
        os.set_blocking(self.master, False)

    def read(self):
        with self.lock:
            if self.closed:
                return ''
            try:
                return self.decoder.decode(os.read(self.master, 8192))
            except BlockingIOError:
                return ''

    def alive(self):
        return not self.closed and self.process.poll() is None

    def write(self, data):
        with self.lock:
            if not self.alive():
                raise EOFError()
            pending = data.encode('utf-8')
            while pending:
                pending = pending[os.write(self.master, pending):]

    def resize(self, cols, rows):
        import fcntl
        import termios
        import struct
        with self.lock:
            if not self.alive():
                raise EOFError()
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            # Signal the PTY's process group; never an application-wide group.
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait(timeout=1)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            os.close(self.master)


class TerminalSession:
    max_output_chars = 262144

    def __init__(self, identity, project, cols, rows, backend):
        self.id = 'term-' + secrets.token_hex(12)
        self.owner = (identity.tenant_id, identity.subject_id)
        self.project_id = project['id']
        self.cols, self.rows = cols, rows
        self.backend = backend
        self.state = 'running'
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.events = deque()
        self.seq = 0
        self.output_chars = 0
        self.touched = time.monotonic()
        self.thread = threading.Thread(target=self._read, name='verilo-terminal', daemon=True)
        self.thread.start()

    def _append(self, text):
        with self.lock:
            for start in range(0, len(text), 8192):
                chunk = text[start:start + 8192]
                self.seq += 1
                self.events.append(dict(seq=self.seq, text=chunk))
                self.output_chars += len(chunk)
                while self.output_chars > self.max_output_chars and self.events:
                    self.output_chars -= len(self.events.popleft()['text'])

    def _read(self):
        try:
            while not self.stop.is_set():
                data = self.backend.read()
                if data:
                    self._append(data)
                if not self.backend.alive():
                    if getattr(self.backend, 'failed', False):
                        self.state = 'failed'
                    break
                self.stop.wait(.04)
        except Exception:
            # Native PTYs raise EOF/OSError when the interactive shell exits;
            # an actual I/O failure while the process lives is a failed session.
            try:
                if self.state == 'running' and self.backend.alive():
                    self.state = 'failed'
            except Exception:
                if self.state == 'running':
                    self.state = 'failed'
        finally:
            if self.state == 'running':
                self.state = 'exited'
            self.backend.close()

    def summary(self):
        return dict(id=self.id, project_id=self.project_id, shell=self.backend.shell,
                    backend=self.backend.backend, state=self.state, cursor=self.seq,
                    cols=self.cols, rows=self.rows, cwd=getattr(self, 'cwd', None))

    def output(self, after):
        self.touched = time.monotonic()
        with self.lock:
            events = [event for event in self.events if event['seq'] > after][:32]
            cursor = events[-1]['seq'] if events else self.seq
            return dict(id=self.id, state=self.state, events=events, cursor=cursor,
                        truncated=bool(self.events and after < self.events[0]['seq'] - 1))

    def close(self, state='closed'):
        self.state = state
        self.stop.set()
        self.backend.close()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=1)


class TerminalManager:
    idle_seconds = 1200

    def __init__(self):
        self.sessions = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.sweeper = None

    def _reap(self):
        while not self.stop.wait(15):
            now = time.monotonic()
            with self.lock:
                stale = [s for s in self.sessions.values() if now - s.touched > self.idle_seconds]
            for session in stale:
                session.close('expired')
                with self.lock:
                    self.sessions.pop(session.id, None)

    def create(self, identity, project, cols, rows, directory=None):
        owner = (identity.tenant_id, identity.subject_id)
        directory = Path(directory or project['path']).resolve()
        if not directory.is_dir():
            raise HTTPException(409, 'PROJECT_DIRECTORY_UNAVAILABLE')
        with self.lock:
            active = [s for s in self.sessions.values() if s.state == 'running']
            if len(active) >= 16 or sum(s.owner == owner for s in active) >= 4:
                raise HTTPException(409, 'TERMINAL_LIMIT_REACHED')
            # Keep completed buffers bounded even while the UI is connected.
            old = [s for s in self.sessions.values() if s.owner == owner and s.state != 'running']
            for session in old[:-12]:
                self.sessions.pop(session.id, None)
            try:
                backend = WindowsPty(directory, cols, rows) if os.name == 'nt' else PosixPty(directory, cols, rows)
                session = TerminalSession(identity, project, cols, rows, backend)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(503, 'TERMINAL_START_FAILED') from None
            session.cwd = str(directory)
            self.sessions[session.id] = session
            if self.sweeper is None:
                self.sweeper = threading.Thread(target=self._reap, name='verilo-terminal-cleanup', daemon=True)
                self.sweeper.start()
            return session

    def get(self, identity, terminal_id):
        with self.lock:
            session = self.sessions.get(terminal_id)
            if session is None or session.owner != (identity.tenant_id, identity.subject_id):
                raise HTTPException(404, 'TERMINAL_NOT_FOUND')
            session.touched = time.monotonic()
            return session

    def list(self, identity, project_id=None):
        owner = (identity.tenant_id, identity.subject_id)
        with self.lock:
            return [s.summary() for s in self.sessions.values() if s.owner == owner and
                    (project_id is None or s.project_id == project_id)]

    def close(self):
        self.stop.set()
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.close()
        if self.sweeper and self.sweeper is not threading.current_thread():
            self.sweeper.join(timeout=1)


def install_terminal_api(app, store, *, Reader, Writer, executor_config=None, local_console=False):
    """Mount only in the explicitly enabled local application, never a preview."""
    if not local_console:
        return None
    manager = TerminalManager()
    app.state.terminals = manager
    app.router.add_event_handler('shutdown', manager.close)

    def guard(request, identity):
        if (not is_human(identity) or not allows_local_console(request)
                or urlsplit('//' + request.headers.get('host', '')).hostname not in {'127.0.0.1', '::1'}):
            raise HTTPException(403, 'LOCAL_TERMINAL_ONLY')

    @app.get('/v1/terminals')
    def list_terminals(request: Request, identity: Reader, project_id: str | None = Query(default=None, max_length=128)):
        guard(request, identity)
        return dict(items=manager.list(identity, project_id))

    @app.post('/v1/terminals', status_code=201)
    def create_terminal(body: TerminalCreate, request: Request, identity: Writer):
        guard(request, identity)
        workspace = getattr(app.state, 'workspace', None)
        if workspace is None:
            raise HTTPException(503, 'WORKSPACE_NOT_CONFIGURED')
        project = workspace.project(identity, body.project_id)
        directory = None
        if body.worktree_id:
            worktree = workspace.worktree(identity, body.worktree_id)
            if worktree['project_id'] != project['id']:
                raise HTTPException(409, 'WORKTREE_PROJECT_MISMATCH')
            directory = worktree['path']
        return manager.create(identity, project, body.cols, body.rows, directory).summary()

    @app.get('/v1/terminals/{terminal_id}/output')
    def read_terminal(terminal_id: str, request: Request, identity: Reader,
                      after: int = Query(default=0, ge=0)):
        guard(request, identity)
        return manager.get(identity, terminal_id).output(after)

    @app.post('/v1/terminals/{terminal_id}/input')
    def write_terminal(terminal_id: str, body: TerminalWrite, request: Request, identity: Writer):
        guard(request, identity)
        session = manager.get(identity, terminal_id)
        if session.state != 'running':
            raise HTTPException(409, 'TERMINAL_CLOSED')
        try:
            session.backend.write(body.data)
        except Exception:
            raise HTTPException(409, 'TERMINAL_CLOSED') from None
        return dict(accepted=True)

    @app.post('/v1/terminals/{terminal_id}/resize')
    def resize_terminal(terminal_id: str, body: TerminalSize, request: Request, identity: Writer):
        guard(request, identity)
        session = manager.get(identity, terminal_id)
        if session.state != 'running':
            raise HTTPException(409, 'TERMINAL_CLOSED')
        try:
            session.backend.resize(body.cols, body.rows)
        except Exception:
            raise HTTPException(409, 'TERMINAL_CLOSED') from None
        session.cols, session.rows = body.cols, body.rows
        return session.summary()

    @app.delete('/v1/terminals/{terminal_id}')
    def close_terminal(terminal_id: str, request: Request, identity: Writer):
        guard(request, identity)
        session = manager.get(identity, terminal_id)
        session.close()
        return session.summary()

    return manager

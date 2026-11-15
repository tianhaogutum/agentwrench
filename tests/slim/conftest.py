"""Shared helpers for the ``agentwrench slim`` test suite (port of mcp-slim's node:test suite)."""

from __future__ import annotations

import heapq
import importlib.util
import itertools
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / 'src'
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentwrench.slim import timers  # noqa: E402

FIXTURES = HERE / 'fixtures'
TOOLS_40 = FIXTURES / 'tools-40.json'
EXAMPLE_CONFIG = FIXTURES / 'slim.json'
PYTHON = sys.executable
MISSING: Any = object()


def fixture(name: str) -> str:
    return str(FIXTURES / name)


def cli_command() -> list[str]:
    """``AGENTWRENCH_SLIM_CLI`` (e.g. ``agentwrench slim``) overrides the module runner."""
    override = os.environ.get('AGENTWRENCH_SLIM_CLI')
    return shlex.split(override) if override else [PYTHON, '-m', 'agentwrench.slim']


def cli_env(env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    if SRC.is_dir():
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(SRC), env.get('PYTHONPATH')]))
    return env


def run(*args: str, cwd: str | Path = HERE, env: dict[str, str] | None = None, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    """Run the CLI synchronously (``spawnSync``) and capture UTF-8 stdout/stderr."""
    return subprocess.run([*cli_command(), *args], cwd=cwd, env=cli_env(env), capture_output=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def spawn_marker(marker: str | Path) -> list[str]:
    """A command that proves it was spawned by creating ``marker``."""
    return [PYTHON, '-c', f"open({str(marker)!r}, 'w').write('spawned')"]


def load_fixture_module(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signal_handlers() -> tuple[Any, Any]:
    return signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """A physical (symlink-free) temporary directory."""
    return Path(os.path.realpath(tmp_path))


class MockTimer:
    def __init__(self, clock: MockTimers, due: float, seq: int, callback: Callable[[], Any], ms: float) -> None:
        self.clock, self.due, self.seq, self.callback, self.ms = clock, due, seq, callback, ms
        self.cancelled = False
        self._ref = True

    def __lt__(self, other: MockTimer) -> bool:
        return (self.due, self.seq) < (other.due, other.seq)

    def cancel(self) -> None:
        self.cancelled = True

    def unref(self) -> MockTimer:
        self._ref = False
        return self

    def has_ref(self) -> bool:
        return self._ref


class MockTimers:
    """Deterministic replacement for ``timers.set_timeout`` (``t.mock.timers``)."""

    def __init__(self) -> None:
        self.now = 0.0
        self._queue: list[MockTimer] = []
        self._seq = itertools.count()

    def set_timeout(self, callback: Callable[[], Any], ms: float) -> MockTimer:
        timer = MockTimer(self, self.now + ms, next(self._seq), callback, ms)
        heapq.heappush(self._queue, timer)
        return timer

    def clear_timeout(self, timer: MockTimer | None) -> None:
        if timer is not None:
            timer.cancel()

    def tick(self, ms: float) -> None:
        target = self.now + ms
        while self._queue and self._queue[0].due <= target:
            timer = heapq.heappop(self._queue)
            if timer.cancelled:
                continue
            self.now = timer.due
            timer.callback()
        self.now = target


@pytest.fixture
def mock_timers(monkeypatch: pytest.MonkeyPatch) -> MockTimers:
    clock = MockTimers()
    monkeypatch.setattr(timers, 'set_timeout', clock.set_timeout)
    monkeypatch.setattr(timers, 'clear_timeout', clock.clear_timeout)
    return clock


class TimerSpy:
    """Record real timers (``t.mock.method(globalThis, 'setTimeout', ...)``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, delay: Callable[[float], float] | None = None,
                 on_set: Callable[[Any, float], Any] | None = None, on_clear: Callable[[Any], Any] | None = None) -> None:
        real_set, real_clear = timers.set_timeout, timers.clear_timeout
        self.timers: list[tuple[Any, float]] = []
        self.cleared: set[Any] = set()

        def spy_set(callback: Callable[[], Any], ms: float) -> Any:
            timer = real_set(callback, delay(ms) if delay else ms)
            self.timers.append((timer, ms))
            if on_set:
                on_set(timer, ms)
            return timer

        def spy_clear(timer: Any) -> None:
            if timer is not None:
                self.cleared.add(timer)
            if on_clear:
                on_clear(timer)
            real_clear(timer)

        monkeypatch.setattr(timers, 'set_timeout', spy_set)
        monkeypatch.setattr(timers, 'clear_timeout', spy_clear)

    def with_ms(self, *values: float) -> list[Any]:
        return [timer for timer, ms in self.timers if ms in values]

    @property
    def durations(self) -> list[float]:
        return [ms for _, ms in self.timers]


class Session:
    """A running ``proxy`` CLI with line-oriented stdout capture (port of the node test harness)."""

    def __init__(self, tmp: Path, mode: str = 'normal', config: dict[str, Any] | None = None,
                 command: list[str] | None = None, options: list[str] | None = None) -> None:
        config_path = tmp / f'slim-{time.monotonic_ns()}.json'
        config_path.write_text(json.dumps({} if config is None else config))
        server = command if command is not None else [PYTHON, fixture('fake_server.py'), mode]
        self.child = subprocess.Popen([*cli_command(), 'proxy', '--config', str(config_path), *(options or []), '--', *server],
                                      cwd=HERE, env=cli_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.raw: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self._stderr = bytearray()
        self._cond = threading.Condition()
        self.ended = False
        self.returncode: int | None = None
        self._threads = [threading.Thread(target=target, daemon=True) for target in (self._read_stdout, self._read_stderr, self._wait)]
        for thread in self._threads:
            thread.start()

    def _read_stdout(self) -> None:
        for line in self.child.stdout:
            text = line.decode('utf-8').rstrip('\n')
            with self._cond:
                self.raw.append(text)
                self.messages.append(json.loads(text))
                self._cond.notify_all()

    def _read_stderr(self) -> None:
        while chunk := self.child.stderr.read1(65536):
            with self._cond:
                self._stderr += chunk
                self._cond.notify_all()

    def _wait(self) -> None:
        code = self.child.wait()
        time.sleep(0.05)  # let the reader threads drain the final output
        with self._cond:
            self.returncode = code
            self.ended = True
            self._cond.notify_all()

    def stderr(self) -> str:
        with self._cond:
            return self._stderr.decode('utf-8', 'replace')

    def until(self, predicate: Callable[[], bool], timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not predicate():
                if self.ended:
                    if predicate():
                        return
                    raise AssertionError(f'proxy exited unexpectedly: {self._stderr.decode(errors="replace")}')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f'timed out; stderr: {self._stderr.decode(errors="replace")}')
                self._cond.wait(remaining)

    def write(self, data: str | bytes) -> None:
        try:
            self.child.stdin.write(data.encode() if isinstance(data, str) else data)
            self.child.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def send(self, message: dict[str, Any]) -> None:
        self.write(json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n')

    @staticmethod
    def same_id(message: dict[str, Any], request_id: Any) -> bool:
        value = message.get('id', MISSING)
        return value is not MISSING and type(value) is type(request_id) and value == request_id

    def reply(self, request_id: Any) -> dict[str, Any] | None:
        return next((m for m in self.messages if self.same_id(m, request_id) and not m.get('method')), None)

    def request(self, request_id: Any, method: str, params: Any = MISSING, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
        if params is not MISSING:
            message['params'] = params
        message.update(extra or {})
        self.send(message)
        self.until(lambda: self.reply(request_id) is not None)
        return self.reply(request_id)

    def closed(self, timeout: float = 15) -> int:
        self.until(lambda: self.ended, timeout)
        return self.returncode

    def end_stdin(self) -> None:
        try:
            self.child.stdin.close()
        except BrokenPipeError:
            pass

    def finish(self) -> int:
        self.end_stdin()
        return self.closed()

    def kill(self) -> None:
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait()
        for thread in self._threads:
            thread.join(5)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            try:
                stream.close()
            except OSError:
                pass


@pytest.fixture
def session(tmp_dir: Path):  # type: ignore[no-untyped-def]
    sessions: list[Session] = []

    def start(mode: str = 'normal', config: dict[str, Any] | None = None, command: list[str] | None = None,
              options: list[str] | None = None) -> Session:
        s = Session(tmp_dir, mode, config, command, options)
        sessions.append(s)
        return s

    yield start
    for s in sessions:
        s.kill()


SLIM_EXPECTED = json.dumps({'tools': [{'name': name, 'description': 'Read.', 'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'integer'}}},
                                       'annotations': {'readOnlyHint': True}} for name in ['read_z', 'read_a', 'read_tail']]},
                           separators=(',', ':')) + '\n'

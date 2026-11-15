"""pytest fixture that builds argv and checks subprocess results at teardown."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sys

import pytest

from .format import CassetteError, load_cassette, loads, save_cassette
from .faults import validate_fault_scenario


MAX_EXIT_REPORT = 4096


def pytest_addoption(parser):
    group = parser.getgroup("mcp-cassette")
    group.addoption("--mcp-record", choices=("none", "once", "all"), default="none",
                    help="none: replay only; once: record missing; all: re-record (executes server)")


def pytest_configure(config):
    config.addinivalue_line("markers", "mcp_cassette(name=None, command=None, redact_paths=(), strict=True, match='params', fault_scenario=None): configure MCP cassette; fault_scenario requires a v1 dictionary and replay mode")


@dataclass(frozen=True)
class CassetteLaunch:
    argv: list[str]
    path: Path
    recording: bool


def _path(directory, name):
    if not isinstance(name, str) or len(name) > 180 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise pytest.UsageError("cassette name must be a plain ASCII filename (no paths)")
    if not name.endswith(".json"):
        name += ".json"
    directory = directory.absolute()
    # Prevent marker-controlled paths or existing symlinks escaping test dir.
    if directory.is_symlink() or directory.resolve().parent != directory.parent.resolve():
        raise pytest.UsageError("cassette directory must not be a symlink")
    target = directory / name
    if target.is_symlink() or target.resolve().parent != directory.resolve():
        raise pytest.UsageError("cassette path must stay inside the cassettes directory")
    return target


@pytest.fixture
def mcp_cassette(request, tmp_path):
    marker = request.node.get_closest_marker("mcp_cassette")
    options = dict(marker.kwargs) if marker else {}
    if marker and marker.args:
        if len(marker.args) != 1 or "name" in options:
            raise pytest.UsageError("mcp_cassette accepts one positional cassette name")
        options["name"] = marker.args[0]
    unknown = options.keys() - {"name", "command", "redact_paths", "strict", "match", "fault_scenario"}
    if unknown:
        raise pytest.UsageError("unknown mcp_cassette marker options: " + ", ".join(sorted(unknown)))
    default = re.sub(r"[^A-Za-z0-9_.-]", "_", request.node.nodeid.split("::", 1)[-1])[:120]
    default += "-" + hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:10]
    path = _path(Path(request.node.path).parent / "cassettes", options.get("name", default))
    mode = request.config.getoption("--mcp-record")
    recording = mode == "all" or (mode == "once" and not path.exists())
    if recording and "fault_scenario" in options:
        raise pytest.UsageError("fault_scenario is replay-only; recording would execute the server")
    scenario = None
    report = tmp_path / "mcp-exit.json"
    argv = [sys.executable, "-m", "agentwrench", "cassette"]
    if recording:
        command = options.get("command")
        if (not isinstance(command, (list, tuple)) or not command
                or not all(isinstance(arg, str) and arg for arg in command)):
            raise pytest.UsageError("recording requires marker command=[executable, ...]; shell strings are not accepted")
        paths = options.get("redact_paths", ())
        if not isinstance(paths, (list, tuple)) or not all(isinstance(p, str) for p in paths):
            raise pytest.UsageError("redact_paths must be a list of JSON Pointers")
        from .format import pointer_tokens
        try:
            for pointer in paths:
                pointer_tokens(pointer)
        except CassetteError as exc:
            raise pytest.UsageError(str(exc)) from exc
        argv += ["record", "--cassette", str(path), "--report", str(report)]
        for pointer in paths:
            argv += ["--redact-path", pointer]
        argv += ["--", *command]
    else:
        if not path.is_file():
            raise pytest.UsageError(f"missing cassette {path}; use --mcp-record=once with a server command")
        try:
            data = load_cassette(path)
            if data["incomplete"]:
                raise CassetteError("incomplete cassette; use --mcp-record=all to replace")
        except CassetteError as exc:
            raise pytest.UsageError(str(exc)) from exc
        match = options.get("match", "params")
        if match not in {"params", "sequence"}:
            raise pytest.UsageError("match must be params or sequence")
        strict = options.get("strict", True)
        if type(strict) is not bool:
            raise pytest.UsageError("strict must be boolean")
        argv += ["replay", "--cassette", str(path), "--report", str(report), "--match", match]
        if strict:
            argv.append("--strict")
        if "fault_scenario" in options:
            try:
                # v1 permits only bounded scalar fields. Validation copies the
                # marker dict so later mutations cannot change the armed plan.
                scenario = validate_fault_scenario(options["fault_scenario"], data)
                scenario_path = tmp_path / "mcp-fault.json"
                save_cassette(scenario_path, scenario)
            except (CassetteError, OSError) as exc:
                raise pytest.UsageError(f"invalid fault_scenario: {exc}") from exc
            argv += ["--fault-scenario", str(scenario_path)]
    yield CassetteLaunch(argv, path, recording)
    # The MCP SDK may ignore server exit codes. The sidecar makes strict
    # failures visible to pytest even when the client swallows shutdown errors.
    if not report.is_file():
        pytest.fail("MCP process did not finalize: launch mcp_cassette.argv and close/wait for it inside the test")
    try:
        # Include whitespace/encoding bytes and avoid buffered read-ahead.
        with report.open("rb", buffering=0) as stream:
            raw = stream.read(MAX_EXIT_REPORT + 1)
        if len(raw) > MAX_EXIT_REPORT:
            raise CassetteError(f"exit report exceeds {MAX_EXIT_REPORT} bytes")
        result = loads(raw)
        if not isinstance(result, dict):
            raise CassetteError("exit report must be an object")
        if type(result.get("exit_code")) is not int:
            raise CassetteError("exit_code must be an integer")
        for field in ("unmatched", "unused"):
            if type(result.get(field)) is not int or result[field] < 0:
                raise CassetteError(f"{field} must be a nonnegative integer")
        if (result["exit_code"] != 0 or (not recording and strict
                and (result["unmatched"] or result["unused"]))):
            pytest.fail(f"MCP cassette process failed: {result}; see subprocess stderr")
        if scenario is not None:
            fault = result.get("fault")
            if not isinstance(fault, dict) or fault.get("triggered") is not True:
                raise CassetteError("fault report must contain triggered=true for the armed scenario")
            actual = validate_fault_scenario({k: v for k, v in fault.items() if k != "triggered"}, data)
            if actual != scenario:
                raise CassetteError("fault report does not match the armed scenario")
        if recording and load_cassette(path)["incomplete"]:
            pytest.fail("MCP recording is incomplete")
    except (OSError, CassetteError, KeyError, TypeError) as exc:
        pytest.fail(f"invalid MCP subprocess result: {exc}")

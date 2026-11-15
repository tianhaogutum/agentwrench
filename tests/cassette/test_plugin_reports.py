"""Validate fixture sidecars even when a client supplies a malformed result."""
import json

import pytest

from .conftest import cassette, request, response


FAULT = {"version": 1, "request_seq": 0, "kind": "eof"}
VALID = {"exit_code": 0, "unmatched": 0, "unused": 0}
CONTEXTS = ["replay", "fault", "record"]


def reports_case(pytester, context, reports, *, strict=True, raw=False):
    directory = pytester.path / "cassettes"
    directory.mkdir()
    (directory / "ping.json").write_text(json.dumps(cassette(
        ("client", request("ping")), ("server", response()))))
    # Synthetic sidecars isolate the validation boundary; real CLI controls live
    # in the existing record-mode/fault tests and installed smoke check.
    pytester.makepyfile('''
import json, sys
from pathlib import Path
import pytest
REPORTS = %r
@pytest.mark.parametrize("report", REPORTS)
@pytest.mark.mcp_cassette("ping", strict=%r, %s)
def test_client(mcp_cassette, report):
    if %r and isinstance(report, dict):
        report = {**report, "fault": {**%r, "triggered": True}}
    path = Path(mcp_cassette.argv[mcp_cassette.argv.index("--report") + 1])
    path.write_bytes(report if %r else json.dumps(report).encode())
''' % (reports, strict,
       'fault_scenario=%r' % FAULT if context == "fault" else 'command=[sys.executable, "-c", "pass"]',
       context == "fault", FAULT, raw))
    return pytester.runpytest_subprocess(
        "--mcp-record=" + ("all" if context == "record" else "none"),
        "-q", "--tb=line", "-p", "no:cacheprovider")


@pytest.mark.parametrize("context", CONTEXTS)
def test_boolean_exit_and_missing_counts_rejected(pytester, context):
    result = reports_case(pytester, context, [{"exit_code": False}])
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*invalid MCP subprocess result*exit_code must be an integer*"])


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("field", ["exit_code", "unmatched", "unused"])
def test_required_report_fields_are_typed(pytester, context, field):
    values = [None, False, True, 0.0, "0", [], {}]
    if field != "exit_code":
        values.append(-1)
    reports = [{**VALID, field: value} for value in values]
    reports.append({key: value for key, value in VALID.items() if key != field})
    result = reports_case(pytester, context, reports)
    result.assert_outcomes(passed=len(reports), errors=len(reports))
    required = "an integer" if field == "exit_code" else "a nonnegative integer"
    result.stdout.fnmatch_lines([f"*invalid MCP subprocess result*{field} must be {required}*"])
    assert "MCP cassette process failed" not in result.stdout.str()


@pytest.mark.parametrize("context", CONTEXTS)
def test_nonobject_reports_get_fixture_diagnostic(pytester, context):
    reports = [None, [], 0, False, "success", [VALID]]
    result = reports_case(pytester, context, reports)
    result.assert_outcomes(passed=len(reports), errors=len(reports))
    result.stdout.fnmatch_lines(["*invalid MCP subprocess result*exit report must be an object*"])
    assert "AttributeError" not in result.stdout.str()


@pytest.mark.parametrize("context", CONTEXTS)
def test_nonzero_integer_exit_retains_process_failure(pytester, context):
    reports = [{**VALID, "exit_code": code} for code in [1, 2, -15]]
    result = reports_case(pytester, context, reports)
    result.assert_outcomes(passed=3, errors=3)
    result.stdout.fnmatch_lines(["*MCP cassette process failed*"])
    assert "invalid MCP subprocess result" not in result.stdout.str()


@pytest.mark.parametrize("context", ["replay", "fault"])
def test_strict_counters_cannot_be_hidden_by_zero_exit(pytester, context):
    reports = [{**VALID, "unmatched": 1}, {**VALID, "unused": 1}]
    result = reports_case(pytester, context, reports)
    result.assert_outcomes(passed=2, errors=2)
    result.stdout.fnmatch_lines(["*MCP cassette process failed*"])


@pytest.mark.parametrize("context", ["replay", "fault"])
def test_nonstrict_allows_valid_positive_counts_and_extensions(pytester, context):
    # Match actual CLI non-strict behavior; shape validation must not silently
    # impose strict mode or reject a future top-level extension.
    reports = [{**VALID, "unmatched": 1, "unused": 2, "extra": {"future": True}}]
    reports_case(pytester, context, reports, strict=False).assert_outcomes(passed=1)


def report_bytes(context):
    report = dict(VALID)
    if context == "fault":
        report["fault"] = {**FAULT, "triggered": True}
    return json.dumps(report).encode()


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("size", [4096, 4097, 1024 * 1024 + 4096])
@pytest.mark.parametrize("padding", ["leading", "trailing"])
def test_report_byte_limit(pytester, context, size, padding):
    body = report_bytes(context)
    whitespace = (b" \t\r\n" * size)[:size - len(body)]
    raw = whitespace + body if padding == "leading" else body + whitespace
    assert len(raw) == size
    result = reports_case(pytester, context, [raw], raw=True)
    result.assert_outcomes(passed=1, errors=int(size > 4096))
    if size > 4096:
        result.stdout.fnmatch_lines([
            "*invalid MCP subprocess result: exit report exceeds 4096 bytes*"])


@pytest.mark.parametrize("context", CONTEXTS)
def test_report_limit_counts_bytes_not_characters(pytester, context):
    report = json.loads(report_bytes(context))
    report["extension"] = "é" * 2100
    raw = json.dumps(report, ensure_ascii=False).encode()
    assert len(raw.decode()) < 4096 < len(raw)
    result = reports_case(pytester, context, [raw], raw=True)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([
        "*invalid MCP subprocess result: exit report exceeds 4096 bytes*"])


@pytest.mark.parametrize("context", CONTEXTS)
def test_bounded_malformed_reports_have_fixture_diagnostics(pytester, context):
    reports = [b"{", b"\xff", b"{" + b" " * 4095]
    result = reports_case(pytester, context, reports, raw=True)
    result.assert_outcomes(passed=3, errors=3)
    result.stdout.fnmatch_lines(["*invalid MCP subprocess result: invalid*JSON*"])
    assert "exit report exceeds" not in result.stdout.str()


def test_oversized_report_read_is_bounded_before_parse(pytester):
    pytester.makeconftest('''
from pathlib import Path
import pytest
import agentwrench.cassette.pytest_plugin as plugin

@pytest.fixture(autouse=True)
def guard_report_read(monkeypatch):
    original = Path.open
    reads = []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size=-1):
            assert 0 < size <= 4097, "unbounded exit-report read"
            data = self.stream.read(size)
            reads.append(len(data))
            assert sum(reads) <= 4097, "exit-report read budget exceeded"
            return data
    def guarded_open(path, mode="r", *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        if path.name == "mcp-exit.json" and mode == "rb":
            # Prevent buffered read-ahead beyond the byte budget as well.
            assert kwargs.get("buffering") == 0
            return Reader(stream)
        return stream
    def forbidden_parse(data):
        pytest.fail("oversized report reached JSON parser")
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(plugin, "loads", forbidden_parse)
    yield
    assert reads == [4097]
''')
    result = reports_case(pytester, "replay", [report_bytes("replay") + b" " * 1024 * 1024], raw=True)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([
        "*invalid MCP subprocess result: exit report exceeds 4096 bytes*"])


@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("operation", ["open", "read"])
def test_report_io_errors_have_fixture_diagnostics(pytester, context, operation):
    pytester.makeconftest('''
from pathlib import Path
from unittest.mock import MagicMock
import pytest

@pytest.fixture(autouse=True)
def fail_report_io(monkeypatch):
    original = Path.open
    def broken_open(path, mode="r", *args, **kwargs):
        if path.name == "mcp-exit.json" and mode == "rb":
            error = OSError("synthetic report %s failure")
            if %r == "open":
                raise error
            stream = MagicMock()
            stream.__enter__.return_value.read.side_effect = error
            return stream
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", broken_open)
    yield
''' % (operation, operation))
    result = reports_case(pytester, context, [report_bytes(context)], raw=True)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([
        f"*invalid MCP subprocess result: synthetic report {operation} failure*"])

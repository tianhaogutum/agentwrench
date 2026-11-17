from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

from agentwrench.sift import BudgetError, verify


def example():
    file = Path(__file__).resolve().parents[2] / "examples/sift/agent_adapter.py"
    spec = importlib.util.spec_from_file_location("example_adapter", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_survives_impossible_budget_without_rerunning_backend(tmp_path):
    raw = b'{"isError":true,"content":[{"type":"text","text":"ConnectionError: refused"}]}'
    archive = tmp_path / "original.json"
    with pytest.raises(BudgetError):
        example().observation(raw, archive, budget=5)
    assert archive.read_bytes() == raw
    with pytest.raises(FileExistsError):
        example().observation(b"{}", archive, budget=500)
    assert archive.read_bytes() == raw


def test_mcp_original_stays_exact_and_iserror_is_pinned(tmp_path):
    payload = {
        "isError": True,
        "content": [{"type": "text", "text": "ValueError: checkout checksum" + " noise" * 500}],
        "structuredContent": {"original_schema": [1, 2, 3]},
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    archive = tmp_path / "original.json"
    result = example().observation(raw, archive, budget=600)
    assert archive.read_bytes() == raw
    assert json.loads(archive.read_bytes()) == payload
    assert any(
        b["pointer"] == "/isError" and b["text"] == "true" for b in result.document["blocks"]
    )
    assert verify(raw, result.text, budget=600) == result.tokens


def test_missing_mcp_flag_is_not_filled_in(tmp_path):
    raw = b'{"content":[{"type":"text","text":"ok"}]}'
    result = example().observation(raw, tmp_path / "original.json", budget=500)
    assert "isError" not in result.text

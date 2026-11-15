"""Port of test/canonicalize.test.js."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentwrench.cache import (
    CacheError,
    FsError,
    analyze,
    canonicalize,
    parse_capture,
    read_capture,
    write_capture,
)

from .helpers import clone, js, read_text, without
from .helpers import fixture as fixture_path


def fixture(name: str):
    return parse_capture(read_text(fixture_path(f"{name}.jsonl")))


def round_trip(entries):
    return parse_capture("\n".join(js(e) for e in entries))


def test_canonicalization_improves_tool_reordering_is_idempotent_and_preserves_metadata() -> None:
    records = fixture("anthropic_tool_reorder")
    original = clone([r.entry for r in records])
    entries = canonicalize(records)
    assert [r.entry for r in records] == original, "must not mutate parsed input"
    assert canonicalize(round_trip(entries)) == entries
    assert analyze(records)["stability"] < 0.52
    assert analyze(round_trip(entries))["stability"] == 1
    for entry, before in zip(entries, original):
        assert without(entry["request"], "tools") == without(before["request"], "tools")
        assert entry.get("usage") == before.get("usage")
        assert entry.get("ts") == before.get("ts")


def test_schema_key_normalization_covers_all_provider_tool_locations() -> None:
    for format in ("anthropic", "openai-chat", "openai-responses"):
        schema = {
            "required": ["z", "a"],
            "type": "object",
            "properties": {"z": {"type": "string"}, "a": {"type": "object", "default": {"z": 1, "a": 2}}},
            "enum": [{"z": 1, "a": 2}],
        }
        if format == "anthropic":
            tool = {"name": "x", "input_schema": schema}
        elif format == "openai-chat":
            tool = {"type": "function", "function": {"name": "x", "parameters": schema}}
        else:
            tool = {"type": "function", "name": "x", "parameters": schema}
        body = {
            "model": "claude-test" if format == "anthropic" else "gpt-test",
            "tools": [tool],
            **(
                {"input": "Do not change me."}
                if format == "openai-responses"
                else {"messages": [{"role": "user", "content": "Do not change me."}]}
            ),
        }
        [entry] = canonicalize(parse_capture(js(body)), ["sort-schema-keys"])
        if format == "anthropic":
            result = entry["tools"][0]["input_schema"]
        elif format == "openai-chat":
            result = entry["tools"][0]["function"]["parameters"]
        else:
            result = entry["tools"][0]["parameters"]
        assert list(result) == ["enum", "properties", "required", "type"]
        assert list(result["properties"]) == ["a", "z"]
        assert result["required"] == ["z", "a"]
        assert list(result["enum"][0]) == ["z", "a"]
        assert list(result["properties"]["a"]["default"]) == ["z", "a"]
        assert entry.get("messages", entry.get("input")) == body.get("messages", body.get("input"))


def test_sort_schema_keys_repairs_schema_property_order_only() -> None:
    a = {
        "model": "gpt-test",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "z",
                    "parameters": {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "string"}}},
                },
            }
        ],
        "messages": [{"role": "user", "content": "same"}],
    }
    b = clone(a)
    b["tools"][0]["function"]["parameters"]["properties"] = {"a": {"type": "string"}, "b": {"type": "string"}}
    records = round_trip([a, b])
    assert analyze(records)["rows"][0]["class"] == "tool_schema_changed"
    assert analyze(round_trip(canonicalize(records, ["sort-schema-keys"])))["stability"] == 1


def test_sorting_refuses_cache_boundary_movement_and_builtin_tools() -> None:
    body = {
        "model": "claude-test",
        "tools": [{"name": "z", "input_schema": {}, "cache_control": {"type": "ephemeral"}}],
        "messages": [],
    }
    records = round_trip([body])
    with pytest.raises(CacheError, match="refusing to move"):
        canonicalize(records)
    canonicalize(records, ["sort-schema-keys"])
    with pytest.raises(CacheError, match="named function"):
        canonicalize(round_trip([{"model": "gpt-test", "input": "hi", "tools": [{"type": "web_search"}]}]))
    with pytest.raises(CacheError, match="rules"):
        canonicalize(records, ["custom-rule"])


def test_writes_complete_private_files_rejects_overwrite_symlink_and_leaves_no_temporary_files(tmp_path) -> None:
    directory = tmp_path
    entries = canonicalize(fixture("anthropic_tool_reorder"))
    out = str(directory / "canonical.jsonl")
    write_capture(out, entries)
    assert stat.S_IMODE(os.lstat(out).st_mode) == 0o600
    assert len(read_capture(out)) == 3
    with pytest.raises(FsError) as error:
        write_capture(out, entries)
    assert error.value.code == "EEXIST"
    target = directory / "target"
    target.write_text("untouched")
    symlink = directory / "link"
    symlink.symlink_to("target")
    with pytest.raises(FsError) as error:
        write_capture(str(symlink), entries)
    assert error.value.code == "EEXIST"
    assert target.read_text() == "untouched"
    assert not (directory / "escape").exists()
    assert sorted(os.listdir(directory)) == ["canonical.jsonl", "link", "target"]


def test_prototype_shaped_keys_remain_inert_json_data() -> None:
    records = parse_capture(
        '{"model":"claude-test","tools":[{"name":"x","input_schema":{"type":"object","properties":'
        '{"__proto__":{"type":"string"},"constructor":{"type":"string"}}}}],"messages":[]}'
    )
    [entry] = canonicalize(records)
    assert "__proto__" in entry["tools"][0]["input_schema"]["properties"]
    assert not hasattr(object(), "polluted")


def test_schema_traversal_distinguishes_property_names_from_keywords_and_preserves_extensions() -> None:
    schema = {
        "properties": {"default": {"type": "object", "properties": {"z": {}, "a": {}}}},
        "x-extension": {"z": 1, "a": 2},
        "allOf": [{"type": "string", "description": "same"}],
        "dependencies": {"legacy": ["z", "a"], "other": {"type": "object", "description": "same"}},
    }
    records = round_trip([{"model": "claude-test", "tools": [{"name": "x", "input_schema": schema}], "messages": []}])
    out = canonicalize(records)[0]["tools"][0]["input_schema"]
    assert list(out["properties"]["default"]["properties"]) == ["a", "z"]
    assert list(out["x-extension"]) == ["z", "a"]
    assert out["dependencies"]["legacy"] == ["z", "a"]
    assert list(out["dependencies"]["other"]) == ["description", "type"]
    assert list(out["allOf"][0]) == ["description", "type"]
    with pytest.raises(CacheError, match="named function"):
        canonicalize(
            round_trip(
                [
                    {
                        "model": "claude-test",
                        "messages": [],
                        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    }
                ]
            )
        )


def test_a_tool_schema_property_named_cache_control_does_not_forbid_sorting() -> None:
    records = round_trip(
        [
            {
                "model": "claude-test",
                "tools": [
                    {"name": "z", "input_schema": {"properties": {"cache_control": {"type": "string"}}}},
                    {"name": "a", "input_schema": {}},
                ],
                "messages": [],
            }
        ]
    )
    assert canonicalize(records)[0]["tools"][0]["name"] == "a"


def test_paths_may_be_pathlike(tmp_path) -> None:
    """Python addition: ``os.PathLike`` arguments behave like strings."""
    out = tmp_path / "canonical.jsonl"
    write_capture(out, canonicalize(read_capture(Path(fixture_path("anthropic_tool_reorder.jsonl")))))
    assert len(read_capture(out)) == 3

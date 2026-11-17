import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch
from agentwrench.fence.errors import FenceError
from agentwrench.fence.syntax import list_symbols, parse_source, same_events, syntax_events, syntax_fingerprint

from .support import FIXTURES, create_two_files_patch


def compare(source, candidate, file, selectors=(), **options):
    return check_patch(patch=create_two_files_patch(f"a/{file}", f"b/{file}", source, candidate),
                       allow=[f"{file}::{selector}" for selector in selectors], read_base=lambda _: source, **options)


def read_fixture(name):
    return (FIXTURES / "upstream" / name).read_bytes().decode("utf-8")


def test_production_react_query_tsx_component_edits_stay_inside_their_initializer():
    source = read_fixture("query-client-provider.tsx")
    candidate = source.replace("{children}", "<React.Fragment>{children}</React.Fragment>")
    assert source != candidate
    assert compare(source, candidate, "provider.tsx", ["QueryClientProvider"])["ok"] is True
    leaked = candidate.replace("'use client'", "'use server'")
    assert compare(source, leaked, "provider.tsx", ["QueryClientProvider"])["ok"] is False


def test_production_hydration_boundary_supports_nested_hook_callbacks_and_exact_comments():
    source = read_fixture("hydration-boundary.tsx")
    candidate = source.replace("return children as React.ReactElement", "return <>{children}</>")
    assert source != candidate
    assert compare(source, candidate, "hydration.tsx", ["HydrationBoundary"])["ok"] is True
    leaked = candidate.replace("'use client'", "'use strict'")
    assert compare(source, leaked, "hydration.tsx", ["HydrationBoundary"])["ok"] is False


@pytest.mark.parametrize("file", ["api.d.ts", "api.mts", "api.cts"])
def test_synthetic_declaration_files_and_mts_cts_use_typescript_grammar(file):
    source = ("declare namespace API { interface Options { readonly timeout?: number; } "
              "function load<T>(arg: T): Promise<T>; }\n")
    assert compare(source, source.replace("timeout?: number", "timeout?: number | undefined"), file,
                   ["API.Options.timeout"])["ok"] is True
    assert compare(source, source.replace("readonly timeout", "timeout"), file, ["API.load"])["ok"] is False


@pytest.mark.parametrize("source", [
    "export default function(){ return 1; }\n",
    "export default class { run(){ return 1; } }\n",
    "export default () => 1;\n",
    "export default { value: 1 };\n",
])
def test_synthetic_anonymous_default_exports_have_a_stable_default_selector(source):
    assert any(symbol["selector"] == "default" for symbol in list_symbols(parse_source(source)))
    assert compare(source, source.replace("1", "2", 1), "a.ts", ["default"])["ok"] is True
    with pytest.raises(FenceError) as error:
        compare(source, source.replace("default ", "", 1), "a.ts", ["default"])
    assert error.value.code in ("syntax_error", "symbol_disappeared")


def test_synthetic_enum_initializer_and_import_alias_are_selectable():
    source = "import Alias = Other.Name;\nenum Mode { Auto, Manual = 2 }\n"
    assert [symbol["selector"] for symbol in list_symbols(parse_source(source))] == ["Alias", "Mode", "Mode.Manual"]
    assert compare(source, source.replace("Manual = 2", "Manual = 3"), "a.ts", ["Mode.Manual"])["ok"] is True


def test_streamed_fingerprints_agree_with_event_equality_on_structural_and_trivia_variants():
    variants = ["function f(){return 1;}", "function f() {\n return 1;\n}", "function f(){return\n1;}",
                "function f(){return 2;}", "// comment\nfunction f(){return 1;}"]
    for a in variants:
        for b in variants:
            left, right = parse_source(a), parse_source(b)
            assert (syntax_fingerprint(left) == syntax_fingerprint(right)) == same_events(syntax_events(left),
                                                                                         syntax_events(right))


def test_event_keys_are_the_original_compact_json_arrays():
    # Same stream (node kinds, field roles, leaves) as the Node.js binding produced.
    keys = [event.key for event in syntax_events(parse_source("let x = 1;"))]
    assert keys == [
        '["open","program"]', '["field",""]', '["open","lexical_declaration"]', '["field","kind"]',
        '["leaf","let","let"]', '["field",""]', '["open","variable_declarator"]', '["field","name"]',
        '["leaf","identifier","x"]', '["field",""]', '["leaf","=","="]', '["field","value"]',
        '["leaf","number","1"]', '["close","variable_declarator"]', '["field",""]', '["leaf",";",";"]',
        '["close","lexical_declaration"]', '["close","program"]',
    ]


def test_multiple_files_read_each_baseline_once_including_permissions_in_unchanged_files():
    source = "function f(){return 1;}\n"
    patch = "".join(create_two_files_patch(f"a/{file}", f"b/{file}", source, source.replace("1", "2"))
                    for file in ("a.ts", "b.ts"))
    reads = []

    def read(file):
        reads.append(file)
        return source

    report = check_patch(patch=patch, allow=["a.ts::f", "b.ts::f", "unchanged.ts::f"], read_base=read)
    assert report["ok"] is True
    assert reads == ["a.ts", "b.ts", "unchanged.ts"]
    with pytest.raises(FenceError) as error:
        check_patch(patch=patch, allow=["a.ts::f", "b.ts::f", "unchanged.ts::typo"], read_base=lambda _: source)
    assert error.value.code == "unknown_symbol"

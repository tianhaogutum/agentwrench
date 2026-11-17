import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.check import check_patch
from agentwrench.fence.syntax import list_symbols, parse_source

from .support import create_two_files_patch


def check(before, after, symbols):
    return check_patch(patch=create_two_files_patch("a/a.ts", "b/a.ts", before, after), read_base=lambda _: before,
                       allow=[f"a.ts::{symbol}" for symbol in symbols])


def test_shared_const_declarators_retain_comma_declaration_kind_and_adjacent_initializer_boundaries():
    source = "const allowed = () => 1, adjacent = () => 2;\n"
    assert check(source, source.replace("=> 1", "=> 3"), ["allowed"])["ok"] is True
    assert check(source, source.replace("=> 2", "=> 3"), ["allowed"])["ok"] is False
    assert check(source, source.replace("const", "let"), ["allowed"])["ok"] is False
    assert check(source, source.replace(", adjacent", "; const adjacent"), ["allowed"])["ok"] is False


def test_nested_function_permission_preserves_enclosing_logic_and_rejects_structural_relocation():
    source = "function outer(){ const marker=1; function inner(){return 2;} return marker; }\n"
    assert check(source, source.replace("return 2", "return 3"), ["outer.inner"])["ok"] is True
    assert check(source, source.replace("return marker", "return marker + 1"), ["outer.inner"])["ok"] is False
    blocks = "if (true) { function f(){return 1;} }\nif (false) { }\n"
    moved = "if (true) { }\nif (false) { function f(){return 2;} }\n"
    assert check(blocks, moved, ["f"])["ok"] is False


def test_unselected_overload_signatures_remain_protected_when_implementation_is_allowed():
    source = "function f(x:string):string;\nfunction f(x:number):number;\nfunction f(x:any){return x;}\n"
    assert check(source, source.replace("return x", "return x ?? null"), ["f#3"])["ok"] is True
    assert check(source, source.replace("x:string", "x:string | undefined"), ["f#3"])["ok"] is False


def test_computed_and_private_methods_get_exact_narrow_selectors():
    source = "class Bag { *[Symbol.iterator](){ yield 1; } #work(){return 2;} other(){return 3;} }\n"
    assert [symbol["selector"] for symbol in list_symbols(parse_source(source))] == [
        "Bag", "Bag[Symbol.iterator]", "Bag.#work", "Bag.other"]
    assert check(source, source.replace("yield 1", "yield 2"), ["Bag[Symbol.iterator]"])["ok"] is True
    assert check(source, source.replace("return 2", "return 5"), ["Bag.#work"])["ok"] is True
    assert check(source, source.replace("return 3", "return 5"), ["Bag[Symbol.iterator]"])["ok"] is False


def test_computed_object_keys_preserve_adjacent_entries_and_prototype_like_names_are_plain_selectors():
    source = "const settings = { [KEY]: 1, safe: 2 };\nfunction __proto__(){return 3;}\n"
    assert check(source, source.replace("[KEY]: 1", "[KEY]: 4"), ["settings[KEY]"])["ok"] is True
    assert check(source, source.replace("safe: 2", "safe: 4"), ["settings[KEY]"])["ok"] is False
    assert check(source, source.replace("return 3", "return 4"), ["__proto__"])["ok"] is True

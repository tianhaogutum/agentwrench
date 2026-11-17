import json
import os
import shlex
import subprocess

import pytest

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_typescript")

from agentwrench.fence.patch import apply_exactly, parse_candidate

from .support import EXAMPLES, cli


def git(directory, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(directory),
                           *args], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout


def commit(directory, message="baseline"):
    git(directory, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)


def write(path, text):
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def needs_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is not installed")


@pytest.mark.parametrize("patch, status", [("allowed.patch", 0), ("outside.patch", 1)])
def test_cli_examples_expose_expected_0_1_exits_and_valid_json(patch, status):
    run = cli("check", "--base-dir", EXAMPLES / "base", "--patch", EXAMPLES / patch,
              "--allow-file", EXAMPLES / "allow.json", "--json")
    assert run.returncode == status, run.stdout + run.stderr
    assert json.loads(run.stdout)["ok"] is (status == 0)


def test_cli_text_output_matches_the_original_lines():
    run = cli("check", "--base-dir", EXAMPLES / "base", "--patch", EXAMPLES / "outside.patch",
              "--allow", "calculator.ts::add")
    assert run.returncode == 1
    assert run.stdout == ("REJECT: 1 accepted symbol edit(s), 2 outside change(s)\n"
                          "  allowed calculator.ts::add\n"
                          "  outside calculator.ts:6:12 outside_syntax * baseline\n"
                          "  outside calculator.ts:6:12 outside_syntax ** candidate\n")
    run = cli("symbols", "--base-dir", EXAMPLES / "base", "--file", "calculator.ts")
    assert run.returncode == 0
    assert run.stdout == ("calculator.ts::add\tfunction_declaration\t1:8-3:2\n"
                          "calculator.ts::multiply\tfunction_declaration\t5:8-7:2\n")


def test_cli_errors_use_exit_2_and_json_without_a_stack_trace():
    run = cli("check", "--json")
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "usage"
    assert run.stderr == ""
    run = cli("check")
    assert run.returncode == 2
    assert run.stdout == ""
    assert run.stderr.startswith("ERROR [usage]: ")
    assert "Traceback" not in run.stderr


def test_cli_help_and_version():
    run = cli("--help")
    assert run.returncode == 0
    assert "agentwrench fence" in run.stdout and "check" in run.stdout and "symbols" in run.stdout
    run = cli("check", "--help")
    assert run.returncode == 0
    assert "agentwrench fence check" in run.stdout and "--ignore-formatting" in run.stdout
    run = cli("--version")
    assert run.returncode == 0 and run.stdout.strip()


def test_real_git_patches_and_immutable_blob_baseline_preserve_working_tree_and_index(tmp_path, needs_git):
    directory = tmp_path / "repo"
    directory.mkdir()
    git(directory, "init", "-q")
    file = "a file.ts"
    source = "export function allowed(){ return 1; }\nexport function other(){ return 2; }\n"
    write(directory / file, source)
    git(directory, "add", "--", file)
    commit(directory)
    write(directory / file, source.replace("return 1", "return 3"))
    patch = git(directory, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", file)
    status = git(directory, "status", "--porcelain=v1")
    index = (directory / ".git" / "index").read_bytes()
    args = ["check", "--repo", directory, "--base", "HEAD", "--patch", "-", "--allow", f"{file}::allowed", "--json"]
    run = cli(*args, input=patch)
    assert run.returncode == 0, run.stdout + run.stderr
    report = json.loads(run.stdout)
    assert report["accepted"][0]["selector"] == "allowed"
    assert report["evidence"]["baseline"]["kind"] == "git"
    assert report["evidence"]["baseline"]["tree"] == git(directory, "rev-parse", "HEAD^{tree}").strip()
    assert len(report["evidence"]["patchSha256"]) == 64
    assert report["files"][0]["baselineSha256"] != report["files"][0]["candidateSha256"]
    assert git(directory, "status", "--porcelain=v1") == status
    assert (directory / ".git" / "index").read_bytes() == index
    assert (directory / file).read_text(encoding="utf-8") == source.replace("return 1", "return 3")
    for options in (["--unified=0"], ["--function-context"], ["--no-prefix"], ["--full-index", "--histogram"]):
        variant = git(directory, "diff", "--no-ext-diff", "--no-textconv", *options, "HEAD", "--", file)
        checked = cli(*args, input=variant)
        assert checked.returncode == 0, checked.stdout + checked.stderr
    (directory / "subdir").mkdir()
    nested = cli("check", "--repo", directory / "subdir", "--base", "HEAD", "--patch", "-",
                 "--allow", f"{file}::allowed", "--json", input=patch)
    assert nested.returncode == 0, nested.stdout + nested.stderr
    env = {**os.environ, "GIT_DIR": str(directory / "missing.git"), "GIT_WORK_TREE": "/nonexistent",
           "GIT_NAMESPACE": "irrelevant"}
    environment = cli(*args, input=patch, env=env)
    assert environment.returncode == 0, environment.stdout + environment.stderr


def test_baseline_symlink_and_path_traversal_are_rejected(tmp_path):
    write(tmp_path / "real.ts", "function allowed(){}\n")
    try:
        (tmp_path / "linked.ts").symlink_to("real.ts")
    except OSError:
        pytest.skip("symlinks are not available")
    run = cli("symbols", "--base-dir", tmp_path, "--file", "linked.ts", "--json")
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "unsafe_baseline"
    traversal = cli("symbols", "--base-dir", tmp_path, "--file", "../real.ts", "--json")
    assert traversal.returncode == 2
    assert json.loads(traversal.stdout)["error"]["code"] == "unsafe_path"


def test_real_git_octal_quoted_unicode_spaces_literal_glob_and_colon_paths_are_read_exactly(tmp_path, needs_git):
    directory = tmp_path / "repo"
    directory.mkdir()
    git(directory, "init", "-q")
    source = "export function allowed(){ return 1; }\n"
    files = ["雪.ts", " leading.ts", "dir/[literal]*.ts", "dir/a:b.ts"]
    (directory / "dir").mkdir()
    for file in files:
        write(directory / file, source)
    git(directory, "add", ".")
    commit(directory)
    for file in files:
        write(directory / file, source.replace("return 1", "return 3"))
    patch = git(directory, "-c", "core.quotePath=true", "diff", "--no-ext-diff", "--no-textconv", "HEAD")
    args = ["check", "--repo", directory, "--base", "HEAD", "--patch", "-", "--json"]
    for file in files:
        args += ["--allow", f"{file}::allowed"]
    run = cli(*args, input=patch)
    assert run.returncode == 0, run.stdout + run.stderr
    assert sorted(edit["file"] for edit in json.loads(run.stdout)["accepted"]) == sorted(files)


def test_cli_refuses_invalid_utf8_input_and_oversized_files_before_parsing(tmp_path):
    invalid = tmp_path / "invalid.patch"
    invalid.write_bytes(b"\xff\xfe")
    run = cli("check", "--base-dir", EXAMPLES / "base", "--patch", invalid, "--json")
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "invalid_encoding"
    huge = tmp_path / "huge.patch"
    with huge.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    run = cli("check", "--base-dir", EXAMPLES / "base", "--patch", huge, "--json")
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "input_limit"
    (tmp_path / "bad.ts").write_bytes(b"\xff")
    run = cli("symbols", "--base-dir", tmp_path, "--file", "bad.ts", "--json")
    assert run.returncode == 2
    assert json.loads(run.stdout)["error"]["code"] == "invalid_encoding"


def test_missing_promisor_blobs_fail_locally_without_invoking_a_configured_remote_helper(tmp_path, needs_git):
    directory = tmp_path / "repo"
    directory.mkdir()
    git(directory, "init", "-q")
    write(directory / "a.ts", "function allowed(){return 1;}\n")
    git(directory, "add", "a.ts")
    commit(directory)
    blob = git(directory, "rev-parse", "HEAD:a.ts").strip()
    marker = directory / "helper-invoked"
    helper = directory / "remote-helper.sh"
    helper.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 1\n", encoding="utf-8")
    helper.chmod(0o755)
    git(directory, "config", "protocol.ext.allow", "always")
    git(directory, "config", "remote.origin.url", f"ext::{helper}")
    git(directory, "config", "remote.origin.promisor", "true")
    git(directory, "config", "extensions.partialClone", "origin")
    (directory / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    run = cli("symbols", "--repo", directory, "--base", "HEAD", "--file", "a.ts", "--json")
    assert run.returncode == 2, run.stdout + run.stderr
    assert json.loads(run.stdout)["error"]["code"] == "git_baseline"
    assert not marker.exists()


def test_nine_real_git_lf_crlf_no_newline_patches_reconstruct_exact_candidate_bytes(tmp_path, needs_git):
    directory = tmp_path / "repo"
    directory.mkdir()
    git(directory, "init", "-q")
    for ending in ("", "\n", "\r\n"):
        source = "function allowed(){return 1;}" + ending
        write(directory / "a.ts", source)
        git(directory, "-c", "core.autocrlf=false", "add", "a.ts")
        commit(directory, "EOF baseline")
        for target in ("", "\n", "\r\n"):
            candidate = "function allowed(){return 3;}" + target
            write(directory / "a.ts", candidate)
            patch = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(directory), "diff", "--no-ext-diff",
                                    "--no-textconv", "HEAD"], capture_output=True, check=True,
                                   timeout=30).stdout.decode("utf-8")
            assert apply_exactly(source, parse_candidate(patch)[0]) == candidate


def test_git_sha256_object_trees_are_valid_immutable_baselines_when_supported(tmp_path, needs_git):
    directory = tmp_path / "repo"
    directory.mkdir()
    init = subprocess.run(["git", "-C", str(directory), "init", "--object-format=sha256", "-q"],
                          capture_output=True, text=True, timeout=30)
    if init.returncode != 0:
        pytest.skip("Installed Git does not support SHA-256 object repositories")
    source = "function allowed(){return 1;}\n"
    write(directory / "a.ts", source)
    git(directory, "add", "a.ts")
    commit(directory, "SHA-256 baseline")
    write(directory / "a.ts", source.replace("1", "2"))
    patch = git(directory, "diff", "--no-ext-diff", "--no-textconv", "HEAD")
    run = cli("check", "--repo", directory, "--base", "HEAD", "--patch", "-", "--allow", "a.ts::allowed", "--json",
              input=patch)
    assert run.returncode == 0, run.stdout + run.stderr
    tree = json.loads(run.stdout)["evidence"]["baseline"]["tree"]
    assert len(tree) == 64 and all(char in "0123456789abcdef" for char in tree)


def test_linked_git_worktree_metadata_files_work_as_baseline_repositories(tmp_path, needs_git):
    main = tmp_path / "main"
    linked = tmp_path / "linked worktree"
    main.mkdir()
    git(main, "init", "-q")
    write(main / "a.ts", "function allowed(){return 1;}\n")
    git(main, "add", "a.ts")
    commit(main, "worktree baseline")
    git(main, "worktree", "add", "--detach", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    run = cli("symbols", "--repo", linked, "--base", "HEAD", "--file", "a.ts", "--json")
    assert run.returncode == 0, run.stdout + run.stderr
    assert json.loads(run.stdout)["symbols"][0]["selector"] == "allowed"

# Third-party sources and attribution

agentwrench is licensed under Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE)).
Most of its commands were previously separate projects (some MIT, some Apache-2.0)
written by the same authors and are relicensed here under Apache-2.0; where a
section below says so, the original MIT notice is kept in [licenses/](licenses/). Unless a
section below says otherwise, all code, synthetic fixtures, tests and documentation
are original work; they contain no production data, real user sessions, tokens,
API responses, model weights or copied issue attachments.

agentwrench is not affiliated with or endorsed by OpenAI, Anthropic, LangChain,
Cline, Continue, Microsoft, the Model Context Protocol project, Harbor, the in-toto
project, Tree-sitter or the shrinkfit authors. Product
names identify interoperable file formats only. This file is attribution; it does
not relicense any dependency or upstream material.

## Dependencies (installed separately, never vendored)

Dependencies are installed by your package manager under their own licenses and
keep their own notices in their distributions. The agentwrench wheel contains only
the `agentwrench` package and these project notices, including the license texts in
[licenses/](licenses/).

| Package | License | Used by | Kind |
| --- | --- | --- | --- |
| [tomlkit](https://github.com/python-poetry/tomlkit) | MIT, copyright Sébastien Eustace and contributors | `mcp-config` | runtime |
| [ruamel.yaml](https://yaml.dev/doc/ruamel.yaml/) | MIT, copyright Anthon van der Neut and contributors; incorporates work by Kirill Simonov | `mcp-config` | runtime |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | `gate` (safe YAML parsing with additional duplicate-key / alias rejection) | runtime |
| [regex](https://github.com/mrabarnett/mrab-regex) | Apache-2.0 AND CNRI-Python | `gate` (regular expressions with a per-match timeout) | runtime |
| [shrinkfit](https://github.com/timesler/shrinkfit) | Apache-2.0 | `sift` (selection proposals; hosted scoring disabled) | optional extra `sift` |
| [tiktoken](https://github.com/openai/tiktoken) | MIT, Copyright (c) 2022 OpenAI, Shantanu Jain | `sift` (exact token counts; downloads a public vocabulary on first use) | optional extra `sift` |
| [json-source-map](https://github.com/open-alchemy/json-source-map) | MIT, Copyright (c) 2021 open-alchemy (LICENSE file; the 1.0.5 wheel's classifier says Apache) | `sift` (JSON Pointer to source offsets) | optional extra `sift` |
| [jsonpointer](https://github.com/stefankoegl/python-json-pointer) | BSD-3-Clause | `sift` (RFC 6901 pointer parsing) | optional extra `sift` |
| [strip-ansi](https://github.com/gwennlbh/python-strip-ansi) | MIT (repository LICENSE; the 0.1.1 wheel declares no license metadata) | `sift` (ANSI removal for log classification only) | optional extra `sift` |
| [py-tree-sitter](https://github.com/tree-sitter/py-tree-sitter) (`tree-sitter`) | MIT | `fence` (parser runtime) | optional extra `fence` |
| [tree-sitter-typescript](https://github.com/tree-sitter/tree-sitter-typescript) | MIT, Max Brunsfeld, Amaan Qureshi and contributors | `fence` (TypeScript and TSX grammars) | optional extra `fence` |
| [in-toto](https://github.com/in-toto/in-toto) | Apache-2.0 | `handoff` (Link metadata, artifact hashing, command execution, signing) | optional extra `handoff` |
| [securesystemslib](https://github.com/secure-systems-lab/securesystemslib) | MIT | `handoff` (key loading and signing; installed with in-toto) | optional extra `handoff` (transitive) |
| [cryptography](https://github.com/pyca/cryptography) | Apache-2.0 OR BSD-3-Clause | `handoff` (PEM key loading; installed with in-toto) | optional extra `handoff` (transitive) |
| [langgraph-checkpoint](https://github.com/langchain-ai/langgraph) | MIT, Copyright (c) 2024 LangChain, Inc. | `checkpoints` | optional extra `langgraph` |
| [langgraph-checkpoint-sqlite](https://github.com/langchain-ai/langgraph) | MIT, Copyright (c) 2024 LangChain, Inc. | `checkpoints` | optional extra `langgraph` |
| [agentevals](https://github.com/langchain-ai/agentevals) | MIT | `gate` (optional local deterministic trajectory matching; required by its integration test) | optional extra `agentevals` |
| [langgraph](https://github.com/langchain-ai/langgraph) | MIT, Copyright (c) 2024 LangChain, Inc. | `checkpoints` tests and `examples/checkpoints/make_fixture.py` | test |
| [mcp](https://github.com/modelcontextprotocol/python-sdk) (official MCP Python SDK) | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE), Copyright (c) 2024 Anthropic, PBC | `cassette` end-to-end tests and `examples/cassette/echo_client.py` | test |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT | `export` tests | test |
| [Pydantic](https://github.com/pydantic/pydantic) / pydantic-core | MIT | `handoff` (strict manifest models and JSON Schema); `export` tests (loading the Harbor models) | optional extra `handoff`; test |
| [pytest](https://github.com/pytest-dev/pytest) | MIT, pytest developers | test runner; host for the `cassette` pytest plugin | test / optional plugin host |
| [Hatchling](https://github.com/pypa/hatch) | MIT, Ofek Lev and contributors | build backend | build |

Transitive dependencies (for example iniconfig, packaging, pluggy, Pygments,
LangChain and LangSmith packages, requests, urllib3, certifi, idna and
charset-normalizer through tiktoken, and Windows-only colorama) retain their own
licenses, which can differ from MIT. Python itself is distributed under the PSF
license. System Git (GPL-2.0-only) is used as an external executable: at runtime by
`agentwrench handoff` and by `agentwrench fence --repo`, and by some `patch`,
`fence` and `handoff` tests. It is never bundled or linked.

The LangGraph libraries are used under the following MIT notice:

> MIT License
>
> Copyright (c) 2024 LangChain, Inc.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## `agentwrench doctor` (formerly rollout-doctor)

No upstream implementation code, real user session, issue attachment, model, or
media asset was copied into the tool.

### OpenAI Codex

- Repository: https://github.com/openai/codex
- License: Apache License 2.0, https://github.com/openai/codex/blob/main/LICENSE
- Format reference: https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs
- Use: response-item names and field shapes for independent JSONL interoperability.
  Source was read to distinguish client pairing from self-contained server tools.
  No Rust implementation was ported. The upstream license does not imply affiliation.

### Anthropic Claude Code

- Repository: https://github.com/anthropics/claude-code
- License reference: https://github.com/anthropics/claude-code/blob/main/LICENSE.md
- The repository's license notice reserves rights to Anthropic PBC; it is **not**
  treated as Apache-2.0 or as permission to reuse implementation code.
- Use: observable persisted JSONL field conventions and public mismatch/corruption
  reports. Independent file-format interoperability only; no proprietary code was
  read or copied.

## `agentwrench checkpoints` (formerly lgckpt-scope)

An independent tool, not an official LangChain product. No upstream implementation
source was vendored. SQL operations and checks are our own; the expected schema is
obtained by running the installed upstream saver on an in-memory database.
Serialization calls the installed upstream library through a data-only subclass.
LangGraph source: https://github.com/langchain-ai/langgraph (including
`libs/checkpoint`, `libs/checkpoint-sqlite`, and `libs/langgraph`). The LangGraph
MIT notice is reproduced above.

## `agentwrench agentsmd` (formerly agentsmd-scope)

### OpenAI Codex — Apache-2.0

Repository: https://github.com/openai/codex

Pinned revision: `54b90f98a5724b688bf78793de5c21aca29ecec2` (main HEAD resolved
with `git ls-remote`, 2026-11-11). Source paths and SHA-256 digests of fetched
files are recorded in [`docs/agentsmd/upstream-sources.json`](docs/agentsmd/upstream-sources.json).

`src/agentwrench/agentsmd/core.py` is a newly written Python behavioral port of
instruction discovery, raw-byte budgeting, lossy decoding, and single-environment
string assembly. It is not a vendored Rust implementation. The OpenAI copyright
attribution is preserved in NOTICE; the Apache-2.0 license text is in LICENSE.
Upstream's unrelated Ratatui notice does not apply to this command: no UI or
Ratatui code is used.

Primary implementation references:

- `codex-rs/core/src/agents_md.rs`: discovery, fallback validation, budget, assembly.
- `codex-rs/codex-home/src/instructions/mod.rs`: global precedence and trimming.
- `codex-rs/config/src/project_root_markers.rs`: marker configuration/default.
- `codex-rs/file-system/src/find_up.rs`: nearest-ancestor marker search.
- `codex-rs/core/src/config/mod.rs`: config values and fallback-name normalization.
- `codex-rs/core/src/agents_md_manager.rs`: project-only budget and host boundaries.

### Claude Code — public documentation, format interoperability only

https://code.claude.com/docs/en/memory (consulted 2026-11-11).

`src/agentwrench/agentsmd/claude.py` is independently written from public
descriptions of CLAUDE.md discovery and @import behavior. No proprietary Claude
Code source, binaries, prompts, or tests are copied. This interoperability mode is
explicitly labeled docs-derived and does not claim exact runtime prompt
reproduction. Claude Code's proprietary licensing is not this project's license.

## `agentwrench mcp-config` (formerly mcpconf-bridge)

No client implementation, logos, or proprietary code were copied. Public schema
facts and documentation were consulted; source URLs, retrieval date and exact
mapping boundaries are in [`docs/mcp-config/FORMATS.md`](docs/mcp-config/FORMATS.md).

| Source | Upstream license / relationship |
|---|---|
| [openai/codex](https://github.com/openai/codex) | Apache-2.0; config format interoperability only |
| [cline/cline](https://github.com/cline/cline) | Apache-2.0; format/docs and issue evidence only |
| [continuedev/continue](https://github.com/continuedev/continue) | Apache-2.0; schema facts only |
| [microsoft/vscode](https://github.com/microsoft/vscode) | MIT for repository source; Microsoft documentation/product terms remain upstream; format interoperability only |
| [anthropics/claude-code](https://github.com/anthropics/claude-code) | Proprietary, all rights reserved; public file-format documentation only; no source reuse |
| [MCP specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) | Transport terminology only; no implementation or specification text bundled. Upstream notes a license transition; no relicensing claim is made here. |

## `agentwrench export` (formerly trajport)

The runtime adapter, redaction, rendering and validation code is original work and
has no third-party runtime dependencies.

### Redistributed Harbor specification and validation models (tests only)

- Upstream: https://github.com/harbor-framework/harbor
- Revision: `500af91374dcc43b59fac54cf90295646617b14d`
- License: Apache-2.0; full text at `tests/export/vendor/harbor/LICENSE`.
- Attribution: Harbor contributors; ATIF RFC maintainer Boxuan Li, with further
  contributor credits retained verbatim in the RFC and source files.
- Files: `tests/export/vendor/harbor/*.py` and `ATIF-RFC.md` are unmodified
  upstream files. Source paths: `src/harbor/models/trajectories/` and
  `rfcs/0001-trajectory-format.md`. File checksums are in
  `tests/export/vendor/harbor/SOURCES.json`.
- `tests/export/atif-v1.8.schema.json` is generated from these models with
  Pydantic 2.11.7 by `tests/export/harbor_schema.py`. It is a derived validation
  artifact under the same Apache-2.0 license.
- These files are test resources, are not imported by the installed runtime and
  are not included in the wheel (they ship only in the source distribution).

### Format interoperability only; no redistributed implementation

- OpenAI Codex: https://github.com/openai/codex (Apache-2.0).
- Cline: https://github.com/cline/cline (Apache-2.0).
- Anthropic Claude Code: https://github.com/anthropics/claude-code;
  proprietary software, copyright Anthropic PBC, all rights reserved.
  Only observable session file formats and supplied format descriptions are
  used. No Anthropic implementation, real session, or protected source was
  copied or redistributed.

Synthetic fixtures contain invented text and fake credentials.

## `agentwrench cassette` (formerly mcp-cassette)

An independent implementation, not an official MCP project. Its runtime uses only
the Python standard library. No upstream implementation source, Inspector assets,
or issue-body text is vendored. Synthetic fixtures and recorded synthetic
conversations are original project material.

| Source | Use | Upstream license / attribution |
| --- | --- | --- |
| [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) (`mcp`, test dependency) | Official SDK client API used in real subprocess end-to-end tests and examples | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE), Copyright (c) 2024 Anthropic, PBC |
| [MCP specification](https://github.com/modelcontextprotocol/modelcontextprotocol) | Newline framing, JSON-RPC and lifecycle interoperability reference; no copied implementation | [LICENSE](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/LICENSE) reports MIT to Apache-2.0 transition for code/specifications; non-specification documentation CC-BY-4.0 |
| [MCP Inspector](https://github.com/modelcontextprotocol/inspector) | Problem evidence only; not a runtime dependency | [LICENSE](https://github.com/modelcontextprotocol/inspector/blob/main/LICENSE) reports MIT to Apache-2.0 transition for code; documentation CC-BY-4.0 |
| [MCP TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk) | Linked bug/discussion and framing PR only; no code copied | Not redistributed |
| [pytest](https://github.com/pytest-dev/pytest) | Optional plugin host and test framework | MIT; pytest developers |

License descriptions were checked on 2026-11-11. Upstream's current repository
license can differ from historical contribution licenses.

## `agentwrench gate` (formerly trajgate)

No upstream implementation code is vendored or copied.

| Source | License | Use |
| --- | --- | --- |
| [openai/codex](https://github.com/openai/codex) | Apache-2.0 | Public exec event definitions and documentation consulted for file-format interoperability. |
| [Anthropic Claude Code](https://github.com/anthropics/claude-code/blob/main/LICENSE.md) | Proprietary, © Anthropic PBC, all rights reserved | Public [headless](https://code.claude.com/docs/en/headless), [SDK types](https://code.claude.com/docs/en/agent-sdk/typescript), and [streaming](https://code.claude.com/docs/en/agent-sdk/streaming-output) documentation consulted only. No proprietary source, binaries, or real transcripts included. |
| [langchain-ai/agentevals](https://github.com/langchain-ai/agentevals) | MIT | Optional local deterministic trajectory matching. |

All JSONL fixtures and the independent AgentEvals reference are hand-authored,
synthetic examples.

## `agentwrench patch` (formerly v4a-patch-bridge)

### OpenAI Codex — Apache-2.0

- Project: https://github.com/openai/codex
- Inspected revision: `54b90f98a5724b688bf78793de5c21aca29ecec2`
- Copyright 2025 OpenAI (upstream NOTICE).
- Grammar and matching semantics from `codex-rs/apply-patch/src/parser.rs`,
  `streaming_parser.rs`, `seek_sequence.rs`, `file_update.rs`, and `text_file.rs`
  informed the independent Python implementation. The Python implementation is
  new and modified in design: bounded input, confined paths, BOM handling,
  two-stage EOF matching, rollback, diagnostics and unified-diff conversion.
- LICENSE contains the Apache-2.0 license text; NOTICE preserves the upstream
  notice verbatim.
- Upstream NOTICE also mentions Ratatui/MIT. No Ratatui code is used by this tool;
  that statement is retained verbatim as part of the upstream notice.

All test text and JavaScript fixtures are synthetic. Evidence issue titles/URLs
were recorded for traceability; issue reports are evidence of user experiences,
not independently reproduced upstream defect claims.

## `agentwrench sift` (formerly tool-sift)

`agentwrench sift` was previously published as the standalone project tool-sift
(distribution `tool-sift-evidence`) under the MIT License, "Copyright (c) 2026
tool-sift contributors". It was written by the same authors and is relicensed under
Apache-2.0 within agentwrench. The original MIT notice is retained in
[licenses/tool-sift-LICENSE.txt](licenses/tool-sift-LICENSE.txt).

It is an adapter around [shrinkfit](https://github.com/timesler/shrinkfit)
(Apache-2.0), used as an installed dependency for selection proposals only. No
shrinkfit code is copied. Token counts come from tiktoken; JSON Pointer source
offsets come from json-source-map; pointer handling uses jsonpointer; strip-ansi is
used only to classify log lines. All are installed by the optional `sift` extra and
are not vendored. The `tool-sift/v1` schema id of the evidence envelope is kept
unchanged for compatibility.

[docs/sift/json-source-map-1.0.5-rfc6901.patch](docs/sift/json-source-map-1.0.5-rfc6901.patch)
is a two-line draft patch against json-source-map 1.0.5 (MIT, Copyright (c) 2021
open-alchemy), kept only as documentation for an unsubmitted upstream report. It is
not applied or shipped in the wheel. The json-source-map license text is retained in
[licenses/json-source-map-LICENSE.txt](licenses/json-source-map-LICENSE.txt).

Related tools consulted when scoping the command (no code used): MCP Compressor
(Atlassian Labs), contextlens-mcp, tool-output-truncate and Repomix.

The test fixtures in `tests/sift/fixtures/` are original output of intentionally
failing synthetic test suites (the producer sources are in
`tests/sift/fixtures/producers/`). They were generated with pytest 9.2.0 and 7.4.4
plus pytest-reportlog 1.0.0, Python unittest, and Node's `node:test` TAP reporter.
The absolute path of the generating directory was normalized to
`/work/sift-fixtures` afterwards. They contain no third-party data.

## `agentwrench fence` (formerly symbol-fence)

`agentwrench fence` was previously a separate Node.js project, symbol-fence, by the
same authors, released under the MIT License ("Copyright (c) 2026 symbol-fence
contributors"). It is relicensed under Apache-2.0 as part of agentwrench; the
original MIT notice is retained in
[licenses/symbol-fence-LICENSE.txt](licenses/symbol-fence-LICENSE.txt). The Python
code is a port of the original JavaScript; behavioural differences are listed in
[docs/fence.md](docs/fence.md#differences-from-the-node-original).

- **Tree-sitter** (py-tree-sitter, MIT) and **tree-sitter-typescript** (MIT, Max
  Brunsfeld, Amaan Qureshi and contributors) parse TypeScript and TSX. They are
  optional runtime dependencies (`agentwrench[fence]`) and are not vendored. The
  original used the Node bindings `tree-sitter` 0.21.1 and `tree-sitter-typescript`
  0.23.2 (both MIT); the same grammar release is used here.
- **jsdiff** (npm `diff` 9.0.0, BSD-3-Clause, Copyright (c) 2009-2015, Kevin Decker):
  the original depended on it for unified-patch parsing, in-memory application and
  the Myers array diff. Its behaviour is reimplemented in Python for the parts used:
  `parsePatch` (Git extended headers, quoted file names, hunk parsing and its
  line-count checks), `applyPatch` restricted to `fuzzFactor: 0` without line-ending
  conversion (including its hunk placement), and `diffArrays` (Myers diff with
  diagonal pruning, `maxEditLength`, `timeout`), in
  `src/agentwrench/fence/jsdiff.py`. The test helper `tests/fence/support.py`
  translates `createTwoFilesPatch` (`structuredPatch`/`formatPatch`) to generate
  jsdiff-identical patches. The BSD-3-Clause notice is retained in
  [licenses/fence-jsdiff-LICENSE.txt](licenses/fence-jsdiff-LICENSE.txt).
- Test fixtures under `tests/fence/fixtures/` are unmodified upstream sources, kept
  with their licenses next to them and copied to `licenses/`:
  - Microsoft TypeScript v5.9.3 `src/compiler/path.ts` (Apache-2.0):
    [licenses/fence-fixture-TypeScript-LICENSE.txt](licenses/fence-fixture-TypeScript-LICENSE.txt);
  - ReactiveX RxJS 7.8.2 `src/internal/util/pipe.ts` (Apache-2.0):
    [licenses/fence-fixture-RxJS-LICENSE.txt](licenses/fence-fixture-RxJS-LICENSE.txt);
  - @tanstack/react-query 5.90.7 `QueryClientProvider.tsx` and `HydrationBoundary.tsx`
    (MIT, Copyright (c) 2021-present Tanner Linsley):
    [licenses/fence-fixture-TanStack-LICENSE.txt](licenses/fence-fixture-TanStack-LICENSE.txt);
  - `compiler-prettier.patch`: a patch generated from the TypeScript fixture after
    formatting with Prettier 3.6.2 (no Prettier code retained; derived TypeScript code
    under the TypeScript license).

  Provenance: `tests/fence/fixtures/upstream/SOURCES.md`,
  `tests/fence/fixtures/formatter/SOURCES.md` and
  `tests/fence/fixtures/formatter/provenance.json`.
- Prior art surveyed for the original design (not used or vendored): ast-grep
  (structural search), Difftastic and diffsitter (structural diff), CodeTruss,
  SpecLock and Codify/Hyperfide (task contracts, file zones, declared-symbol drift).
  The original's development-only TypeScript compiler and fast-check dependencies
  are not used.

## `agentwrench handoff` (formerly anchored-handoff)

Previously the standalone Apache-2.0 project anchored-handoff by the same authors.
The command composes three existing components and vendors none of them: in-toto
(Link metadata, artifact hashing, command execution, signing and DSSE envelopes),
Git (native binary diffs and NUL-delimited index/file enumeration, run as an
external executable) and Pydantic (strict manifest models and JSON Schema). The
in-toto `environment` key `anchored_handoff` and the manifest fields are kept
unchanged for compatibility.

- in-toto (https://github.com/in-toto/in-toto, Apache-2.0) is a runtime dependency
  (`in_toto.runlib`, `in_toto.models`). Receipts are standard in-toto Links; the
  source-state receipt is an extra object in the Link's `environment`.
- securesystemslib (MIT) and cryptography (Apache-2.0 OR BSD-3-Clause) are installed
  through in-toto and used directly for PEM key loading and signing.
- Test fixtures in `tests/handoff/fixtures/` are metadata generated by running
  in-toto 3.0.0 (`in_toto_run`, `in-toto-run`, `in-toto-record`) and in-toto 2.3.0
  with securesystemslib 0.31.0 against a synthetic one-file repository, with
  throwaway keys (public halves only). See `tests/handoff/fixtures/README.md`. They
  contain no third-party code.
- Related projects (design context only, no code or schema used): Portable Handoff
  (https://github.com/legoambarish/portable-handoff), AgentCarry
  (https://github.com/Jettlin927/agentcarry), Agents Handoff
  (https://github.com/Ryujin-Labs/agents-handoff) and Proofline
  (https://pypi.org/project/proofline/).
  `examples/handoff/portable_handoff_recipe.py` drives Portable Handoff through its
  own CLI when the user installs it.

# agentwrench mcp-config: native format contract

Field mappings used by [`agentwrench mcp-config`](../mcp-config.md). Retrieved **2026-11-11 (UTC)**. These are documentation/schema observations,
not a claim to support every client version or to validate the complete client
configuration. No upstream implementation was copied. Tests use synthetic data.
The URLs below are the field-level provenance; a retrieval date is used because
these documentation URLs and `main` source files are mutable.

| Format | Fields verified | Primary source (retrieved 2026-11-11) | `mcp-config` behavior |
|---|---|---|---|
| Claude Code | `mcpServers`, `type`, `command`, `args`, `env`, `url`, `headers` | [MCP configuration and project scope](https://code.claude.com/docs/en/mcp) | Native JSON; stdio/HTTP/SSE vocabulary, native `ws` also retained. Map command/args/env/url/headers. `${VAR}` and `${VAR:-default}` remain literal text. |
| Codex | `mcp_servers`, `command`, `args`, `env`, `cwd` | [Official MCP documentation, config.toml / STDIO](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) | TOML tables. No transport `type` is emitted: command selects stdio, URL selects HTTP. |
| Codex | `url`, `bearer_token_env_var`, `http_headers`, `env_http_headers` | [Official MCP documentation, Streamable HTTP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) | `http_headers` maps to common headers. The two environment-variable authentication fields remain Codex-only, without reading the environment. |
| Codex | `startup_timeout_sec`, `tool_timeout_sec`, `enabled`, `enabled_tools`, `disabled_tools` | [Official MCP documentation, other configuration options](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) | Timeout values are seconds. Preserve native values. Only `enabled` has a cross-client equivalent (`!disabled` in Cline). Tool availability is not approval. |
| Cline | `mcpServers`, `command`, `args`, `env`, `disabled`, `autoApprove`, `type`, `url`, `headers` | [Cline MCP manual configuration and examples](https://docs.cline.bot/mcp/mcp-overview) | JSON. `streamableHttp` ↔ common HTTP; absent type with URL defaults to SSE, as documented. Preserve native `autoApprove`; report loss elsewhere. |
| Cline | `timeout`, `metadata`, `oauth` | [Maintainer-repository issue #12680](https://github.com/cline/cline/issues/12680), [timeout change #12546](https://github.com/cline/cline/pull/12546) ([patch](https://github.com/cline/cline/pull/12546.patch)) | `timeout` is a per-request limit in seconds, broader than Codex's tool-call-only limit. No false equivalence: retain all three as native passthrough; report cross-format loss. PR evidence is not a guarantee that every installed version has the change. |
| Continue | root `name`, `version`, `schema`; `mcpServers` list; per-item `name`, `command`, `args`, `env`, `cwd`, `connectionTimeout`, `requestOptions` | [config.yaml reference](https://docs.continue.dev/reference#mcpservers) | Emit a minimal standalone YAML document with name/version/schema. Preserve unrelated models/rules on merge. Preserve connection timeout as native passthrough, without assuming units. |
| Continue | `type`: `stdio`, `sse`, `streamable-http`; `url`; `requestOptions.headers` | [Native MCP schema](https://github.com/continuedev/continue/blob/main/packages/config-yaml/src/schemas/mcp/index.ts), [requestOptions schema](https://github.com/continuedev/continue/blob/main/packages/config-yaml/src/schemas/models.ts), [project issue documenting transport/header examples](https://github.com/continuedev/continue/issues/7595) | Nested headers ↔ common headers. Preserve other requestOptions entries only in native passthrough. Require explicit type for cross-format conversion of a remote Continue server because the schema alone does not establish its runtime default. Hub `uses`/`override` entries error; no fetching or resolution. |
| VS Code editor-managed | `servers`, `inputs`, `type`, `command`, `args`, `env`, `cwd`, `url`, `headers`; unknown native `envFile`, `dev`, `sandbox`, `oauth` | [MCP configuration reference, Local compatibility](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#editor-managed-configuration-for-local-harness-compatibility) | JSONC, including comments and trailing commas. `inputs` stays document-level. Non-string env values (number/null) can round-trip natively, but error when converting to clients requiring strings. |

## Interpretation and fidelity

The IR stores the original native server object, common fields, canonical
transport, and an origin-keyed `passthrough` bag. A native A → IR → A operation
retains every supported JSON-compatible value, including unknown keys. The CLI
returns original bytes for an unchanged native conversion. Library IR values are
not redacted or encrypted; they are not a safe logging format.

A → B transfers only fields with an explicitly verified mapping. Native unknown
keys are **not injected into an unrelated client's schema**. They stay in the
source and in the in-memory passthrough bag; the report lists each omitted leaf
(or empty container). B → A cannot restore those lost fields without the original
A. Keep the source and inspect `report` before converting. Native comments cannot
be carried into another syntax: cross-format conversion creates a new document.
Document-level non-server fields are reported and retained only in their source
or in an existing merge target.

`disabled: true` ↔ `enabled = false` is boolean inversion. Missing enablement
means enabled for semantic checks. Missing stdio args/env and remote headers
mean empty collections. Cline approval (`autoApprove`) and Codex tool filtering
(`enabled_tools`/`disabled_tools`) are intentionally distinct. Cline request
`timeout`, Codex startup/tool limits and Continue `connectionTimeout` describe
different lifecycles; this version never guesses conversions between them.

Variables are not resolved, rewritten, or read from the process environment.
Claude references, VS Code `${env:NAME}` / `${input:id}` / `${command:id}` and
Continue `${{ secrets.NAME }}` have different runtimes. Cross-format expressions
produce a warning even if their literal text survives; a successful `check`
verifies configuration fields, not client-side variable evaluation or connectivity.
Codex `bearer_token_env_var` and `env_http_headers` are variable *names*, not
literal secrets, and never become fabricated HTTP header expressions elsewhere.

As of retrieval, VS Code describes its editor-managed `servers` format as Local
harness compatibility, deprecated for removal in February 2027. This tool's
`vscode` identifier deliberately implements the requested editor format, **not**
the newer Copilot `mcpServers` format. The [reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)
explains the distinction; do not assume the target file is active in every harness.

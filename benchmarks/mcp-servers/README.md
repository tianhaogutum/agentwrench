# MCP tool-definition size: before and after `agentwrench slim`

An MCP client sends every tool's name, description and input schema with every
request, so the model sees these definitions on every turn. This benchmark
measures how many tokens that takes for 11 widely used MCP servers, and how much
`agentwrench slim` removes without hiding any tool.

- Date: 2026-11-15. agentwrench 0.1.0. Linux x86_64, Node v24.22.2, uv 0.11.19, tiktoken 0.14.0.
- Raw numbers: [`results.json`](results.json). Reproduce: [`run.py`](run.py).
- Each cell below is **slim estimate / tiktoken `o200k_base`**, with the percent saved by each.
  The slim estimate is `ceil(UTF-8 bytes / 4)`. It is an estimate, not a model token count.

## Results

| Server | Version | Tools | Before | Trim-only (160 chars) | Strip-only | `plan` @50% budget | Floor (no descriptions) |
|---|---|--:|--:|--:|--:|--:|--:|
| Filesystem (MCP reference) | `@modelcontextprotocol/server-filesystem@2.3.0` | 16 | 3,444 / 2,925 | 2,486 / 2,184 (−28% / −25%) | 2,382 / 2,081 (−31% / −29%) | 1,717 / 1,562 (−50% / −47%) cap 253 | 1,006 / 999 (−71% / −66%) |
| Memory (MCP reference) | `@modelcontextprotocol/server-memory@2.1.1` | 9 | 2,010 / 1,736 | 1,649 / 1,436 (−18% / −17%) | 1,326 / 1,188 (−34% / −32%) | 989 / 909 (−51% / −48%) cap 231 | 760 / 741 (−62% / −57%) |
| Sequential Thinking (MCP reference) | `@modelcontextprotocol/server-sequential-thinking@2.1.1` | 1 | 557 / 489 | 320 / 295 (−43% / −40%) | 438 / 390 (−21% / −20%) | 224 / 214 (−60% / −56%) cap 526 | 182 / 184 (−67% / −62%) |
| Everything (MCP reference test server) | `@modelcontextprotocol/server-everything@2.3.0` | 16 | 1,858 / 1,721 | 1,439 / 1,330 (−22% / −23%) | 1,563 / 1,476 (−16% / −14%) | 928 / 933 (−50% / −46%) cap 6 | 905 / 894 (−51% / −48%) |
| Fetch (MCP reference, Python) | `mcp-server-fetch@2026.11.11` | 1 | 336 / 296 | 279 / 249 (−17% / −16%) | 179 / 170 (−47% / −43%) | 159 / 153 (−53% / −48%) cap 306 | 102 / 108 (−70% / −64%) |
| Git (MCP reference, Python) | `mcp-server-git@2026.11.11` | 14 | 1,540 / 1,435 | 1,503 / 1,406 (−2% / −2%) | 959 / 886 (−38% / −38%) | 765 / 733 (−50% / −49%) cap 8 | 744 / 699 (−52% / −51%) |
| Context7 (Upstash) | `@upstash/context7-mcp@4.7.0` | 2 | 1,236 / 1,062 | 643 / 562 (−48% / −47%) | 774 / 662 (−37% / −38%) | 540 / 474 (−56% / −55%) cap 1861 | 126 / 123 (−90% / −88%) |
| Playwright MCP (Microsoft) | `@playwright/mcp@0.0.85` | 25 | 4,614 / 3,934 | 4,496 / 3,850 (−3% / −2%) | 2,507 / 2,320 (−46% / −41%) | 2,304 / 2,165 (−50% / −45%) cap 123 | 2,011 / 1,917 (−56% / −51%) |
| Chrome DevTools MCP (Google) | `chrome-devtools-mcp@1.13.0` | 33 | 7,563 / 6,751 | 6,968 / 6,272 (−8% / −7%) | 4,312 / 4,076 (−43% / −40%) | 3,761 / 3,633 (−50% / −46%) cap 165 | 3,046 / 3,031 (−60% / −55%) |
| Notion MCP (Notion) | `@notionhq/notion-mcp-server@2.6.1` | 24 | 18,661 / 17,161 | 18,610 / 17,110 (−0% / −0%) | 16,648 / 15,506 (−11% / −10%) | 16,170 / 15,026 (−13% / −12%) cap 0, budget not met | 16,170 / 15,026 (−13% / −12%) |
| GitHub MCP (GitHub, default toolsets) | `github/github-mcp-server@2.1.0` | 46 | 12,341 / 11,131 | 11,604 / 10,501 (−6% / −6%) | 6,471 / 6,215 (−48% / −44%) | 6,162 / 5,945 (−50% / −47%) cap 304 | 4,796 / 4,804 (−61% / −57%) |
| GitHub MCP (GitHub, --toolsets all) | `github/github-mcp-server@2.1.0` | 91 | 26,053 / 23,138 | 24,747 / 22,048 (−5% / −5%) | 13,094 / 12,360 (−50% / −47%) | 13,023 / 12,294 (−50% / −47%) cap 884 | 10,049 / 9,868 (−61% / −57%) |
| **Typical stack: filesystem + github + playwright + context7** | | 89 | **21,635 / 19,052** | 19,229 / 17,097 (−11% / −10%) | 12,134 / 11,278 (−44% / −41%) | **10,723 / 10,146 (−50% / −47%)** | 7,939 / 7,843 (−63% / −59%) |

The typical stack uses GitHub's default toolsets. No column removes a tool: every
server keeps all its tools in every column.

### What each column is

| Column | Config | What the model loses |
|---|---|---|
| Before | none | nothing |
| Trim-only | `{"maxDescriptionChars": 160}` | tool descriptions longer than 160 characters are cut, at the last sentence end (else word boundary) that fits |
| Strip-only | `{"stripSchemaAnnotations": ["description", "title", "examples", "$comment"]}` | the `description`/`title`/`examples`/`$comment` keywords inside input schemas (per-parameter help text). Tool descriptions, parameter names, types, enums, `required` and `default` stay |
| `plan` @50% | output of `agentwrench slim plan <catalogue> --budget B`, with B = 50% of that server's "before" estimate | strip-only, plus every tool description capped at the shown number of characters ("cap") |
| Floor | strip-only + `{"maxDescriptionChars": 0}` | all descriptions. This is the smallest result possible without hiding tools. `plan` writes it when a budget can't be met |

**Why a 50% budget.** One budget in absolute tokens can't fit both a 1-tool server
and a 91-tool server, so the budget is relative: half of each server's own
size. The cap column shows how hard `plan` had to cut to reach it. For the large
servers (GitHub, Playwright, Chrome DevTools, Filesystem) a 50% budget leaves
descriptions of 123–884 characters, which keeps them readable. For Git and Everything the
budget is met only by cutting descriptions to 6–8 characters (for example
`"Shows the working tree status"` becomes `"Shows"`). Don't use those configs. Strip-only
is the sensible stopping point for those servers. Notion can't reach 50% without
denying tools, because its schemas are structural OpenAPI `$defs` and not prose.
`plan` then writes `maxDescriptionChars: 0`, which removes every description to save only 2.5
points more than strip-only, and prints manual deny suggestions to stderr.

## Estimate versus a real tokenizer

`run.py` also counts the exact JSON text that `slim measure` sizes (compact
`{"name","description","inputSchema"}` per tool) with tiktoken `o200k_base` and
`cl100k_base`. Both are in `results.json`.

- On unmodified catalogues the bytes/4 estimate is **7–18% higher** than `o200k_base`
  (9–20% higher than `cl100k_base`). For the typical stack it's 21,635 versus 19,052 (+13.6%).
- Slimming removes English prose, which tokenizes efficiently, and keeps JSON
  punctuation, which doesn't. After slimming the estimate is therefore closer to the tokenizer
  (stack: +5.7% at `plan` @50%, +1.2% at the floor). As a result, **savings measured with
  `o200k_base` are up to 6 points lower than the estimate shows** (per cell, the tokenizer shows between 0.5 points more and 6.4 points less saving). For the stack it's 47% versus 50% at `plan` @50%,
  and 41% versus 44% for strip-only. Quote the tokenizer number when you can.
- Claude, Gemini and other models use their own tokenizers. There's no offline Claude tokenizer, so
  OpenAI's public BPEs are only a stand-in. Treat all numbers as about ±15%.

## Method

For each server, `run.py`:

1. installs the pinned version into `<workdir>/servers/<id>`. npm packages are installed with `--ignore-scripts`. Python packages go into a
   `uv venv`. GitHub's server is the release tarball, checked against the SHA-256 in the release's
   `checksums.txt`. Only top-level packages are pinned. npm and PyPI resolve transitive dependencies
   at install time.
2. runs `agentwrench slim discover --output <workdir>/catalogues/<id>.json -- <server>`.
   The process gets a minimal environment: a fresh `HOME` inside the workdir, `PATH` limited to node,
   npm and uv plus `/usr/bin:/bin`, and `DO_NOT_TRACK=1`. Servers that need a token only to start
   get the dummy value `dummy-not-a-real-token` (GitHub: `GITHUB_PERSONAL_ACCESS_TOKEN`, Notion:
   `NOTION_TOKEN`). Listing tools makes no API call, so no real credential is used or
   needed. Chrome DevTools MCP runs with `--no-usage-statistics`. Filesystem is given one empty
   allowed directory. The exact `discover` command for each server is in `results.json`.
3. runs `slim measure --json` for the "before" column, `slim measure --config` for each
   variant, and `slim plan --budget` to generate the `plan` config. It checks that the bytes it
   tokenizes match `slim measure` exactly.

The `plan` @50% config was also applied live through `agentwrench slim proxy` to Filesystem.
The proxied `tools/list` measured 6,866 B / ~1,717 tokens, the same as `measure --config` predicts.

Third-party catalogues aren't committed. They stay in the workdir.

### Reproduce

From the repository root (needs Node.js with npm, uv, and network access to npm, PyPI, GitHub
releases and the tiktoken BPE download):

```sh
uv run --with tiktoken==0.14.0 python benchmarks/mcp-servers/run.py --workdir /tmp/aw-bench
```

The script overwrites `benchmarks/mcp-servers/results.json` and prints the table above. Use
`--output` to write elsewhere and `--only <id>...` to run a subset. A run from an empty workdir
took about 20 seconds here. Two runs from separate empty workdirs gave identical numbers.

## Caveats

- **Scope.** The number covers the JSON tool definitions only (`name`, `description`,
  `inputSchema`), which is what clients forward to the model. It excludes `outputSchema`,
  `annotations` and `title`. Those make the full `tools/list` objects much larger for some
  servers (GitHub 2.6x, Everything 3.5x), but clients don't normally send them to the model. It
  also excludes the provider's tool-use wrapper prompt and any per-tool overhead a provider adds.
- **Savings depend on the budget.** Strip-only and trim-only are fixed configs. `plan` results
  scale with the budget you choose. Look at the resulting description cap before you
  adopt a config.
- **Lost help text can cost accuracy.** Strip-only removes per-parameter descriptions, and `plan`
  shortens tool descriptions. This benchmark measures size, not how well an agent picks and calls
  tools afterwards.
- **Hidden tools are unavailable.** None of the configs here deny tools. If you add `allow`/`deny`
  (or follow `plan`'s deny suggestions), the hidden tools can't be called by the agent at all.
- **Prompt caching** reduces the cost of resending definitions, but not the context-window space they take.
- **Versions move.** Tool lists change between releases. GitHub's numbers depend on the
  toolsets you enable (46 tools by default, 91 with `--toolsets all`).

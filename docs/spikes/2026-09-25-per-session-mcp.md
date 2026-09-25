# Spike: per-session MCP servers over ACP

- **Date:** 2026-09-25
- **Answers:** open question 1 of the
  [architecture spec](../specs/2026-09-25-roost-architecture-design.md#16-open-questions)
- **Method:** measured, not read from docs. Harness in
  [`spikes/2026-09-25-mcp-per-session/`](../../spikes/2026-09-25-mcp-per-session/).
- **Versions:** `claude-agent-acp` 0.81.0 (Claude Code 2.1.283), `codex-acp`
  1.13.0 (codex-cli 0.155.1), macOS arm64.

## Question

Can roost give each session only its own hat's MCP servers? Concretely:

1. Do Claude and Codex accept MCP servers passed per session in ACP
   `session/new` (`mcpServers`)?
2. Do they still load servers from global (per-user) config alongside?
3. If so, can the global sources be switched off per session?

## Setup

Three logging MCP probe servers (streamable HTTP), each exposing one tool that
returns a unique marker:

| Probe | Configured where |
|---|---|
| `session` | ACP `session/new.mcpServers` (HTTP) |
| `global` | per-user config: `claude mcp add --scope user`, `codex mcp add` |
| `project` | project config: Claude local scope (`projects[path].mcpServers`), Codex `<project>/.codex/config.toml` |

A small ACP client drove one session per scenario and asked the agent to call
every probe tool. Evidence is the probe's own request log (`initialize`,
`tools/list`, `tools/call`), not the model's prose; the model's reply was used
only as a cross-check.

## Results

### Baseline: both agents load everything

| Scenario | `session` | `global` | `project` |
|---|---|---|---|
| Claude, project dir with project config | loaded, called | loaded, called | loaded, called |
| Claude, other project dir | loaded, called | loaded, called | — |
| Codex, project dir with project config | loaded, called | loaded, called | loaded, called |
| Codex, other project dir | loaded, called | loaded, called | — |

- **Per-session injection works on both.** `claude-agent-acp` advertises
  `mcpCapabilities {http: true, sse: true}`; `codex-acp` advertises
  `{http: true, sse: false}` — **Codex rejects SSE-transport servers**, so the
  gateway must serve streamable HTTP.
- **Both also load every global source.** For Claude that includes, besides
  the per-user `~/.claude.json` servers, **the account's claude.ai connectors**
  (a server set the machine does not even store locally). A per-session list is
  therefore additive, never exclusive, by default.
- Codex loads `<project>/.codex/config.toml` because `codex-acp` marks every
  session root as trusted.

### Claude: `--strict-mcp-config` via `_meta` isolates the session

`claude-agent-acp` spreads `session/new._meta.claudeCode.options` into the
Claude Code SDK options, including `extraArgs`. Passing

```json
{"_meta": {"claudeCode": {"options": {"extraArgs": {"strict-mcp-config": ""}}}}}
```

left **only** the session-injected server. The global and project probes
received no request at all, and a listing of all MCP servers visible to the
session went from sixteen (account connectors, user servers, the probes) to
exactly one.

**It is not persisted across resume.** `session/load` with the same `_meta`
stayed isolated; `session/load` without it brought the global and project
servers back. roost must send the same `_meta` (and the same `mcpServers`) on
every `session/new` **and** every `session/load`.

### Codex: no per-session switch; `CODEX_HOME` per adapter process works

`codex-acp` builds the per-session servers as a config override layered on top
of the user's config, and exposes no per-session way to remove configured
servers. It forwards its environment to the `codex app-server` it spawns, so
`CODEX_HOME` pointing at a roost-owned directory (containing only a symlink to
the user's `auth.json` and an empty `config.toml`) removed the global probe:

| Scenario | `session` | `global` | `project` |
|---|---|---|---|
| Isolated `CODEX_HOME`, other project dir | loaded, called | — | — |
| Isolated `CODEX_HOME`, project dir with project config | loaded, called | — | loaded, called |

Consequences, all measured:

- **Session history lives in `CODEX_HOME`.** With an empty isolated home,
  resuming with the same home worked; resuming the same session id with the default home failed with
  `no rollout found for thread id …`. The home chosen for a session must stay
  fixed for its whole life.
- `CODEX_HOME` is per **process**, so isolation is per adapter process: roost
  needs at least one Codex adapter process per hat (or per session).
- An empty home also drops the user's own Codex setup (AGENTS.md, skills,
  model defaults) and hides roost sessions from the terminal's resume list.
  The composed home below removes both costs.

### Codex: a composed `CODEX_HOME` keeps the user's setup

Measured on 2026-09-26 against a synthetic user home (so the real one was not
touched) containing: a symlinked `auth.json`, an `AGENTS.md` with a marker
instruction, a skill, `model`/`model_reasoning_effort` settings, and a global
MCP probe. The composed home
([`compose_codex_home.py`](../../spikes/2026-09-25-mcp-per-session/compose_codex_home.py))
symlinks every top-level entry except `config.toml`, which is copied with all
`mcp_servers` tables removed.

| Home used by the adapter | AGENTS.md | skill | model settings | global MCP | session MCP |
|---|---|---|---|---|---|
| user home | followed | used | applied (`luna[high]`) | loaded, called | loaded, called |
| composed home | followed | used | applied (`luna[high]`) | — | loaded, called |

Resume across homes, possible because `sessions/` is shared through the
symlink:

| Session created under | Loaded under | Result | MCP servers after load |
|---|---|---|---|
| composed home | user home | resumed | global + session |
| user home | composed home | resumed | session only |

So:

- The earlier "history is lost with an isolated home" cost disappears: roost
  sessions stay in the user's normal history and can be resumed from a
  terminal.
- **The MCP set follows the adapter process that loads the session, not the
  session.** Resuming a roost session from the terminal gives it the user's
  global servers; that is the operator's own choice, outside roost.
- **Divergence risk, observed:** during the run Codex created a new real
  directory (`mcp-oauth-locks`) inside the composed home instead of the user's.
  Anything Codex creates at the top level after composition lands in the
  roost copy. A product implementation must re-compose on every adapter start
  and decide per entry whether a newly appearing path is shared or private.
- The line-based TOML filter is spike quality; the product needs a real TOML
  parser (inline `mcp_servers = {…}` tables, dotted keys).

### Codex: name collisions silently drop the session server

A session server named like an already configured server (`global`) was
dropped without error; the configured one was used. `codex-acp` filters
session servers whose names already exist in config unless
`DISABLE_MCP_CONFIG_FILTERING=true`. With an isolated home this can only
happen against project config, but roost should still namespace its server
names.

### Project-scoped config is not a cross-hat leak

Project-level servers (Claude local/project scope, Codex
`.codex/config.toml`) live inside the project's path, which by construction
belongs to the same hat as the session. Only per-user and account-level sources
cross hat boundaries. Note that Claude's strict mode drops project-scoped
servers too; Codex's isolated home does not.

### Side findings

- **Claude keys project config by resolved path.** Registering local scope in
  `/tmp/…` stored it under `/private/tmp/…`. Input for open question 5
  (symlinked paths in hat rules): the agent itself resolves symlinks.
- **`codex mcp add/remove` rewrites `config.toml` and drops comments.** A
  renderer cannot use TOML comments as ownership markers; ownership must live
  in the data (e.g. a naming convention or a sidecar record).
- Codex 0.155 exposes MCP tools through its code-mode `exec` tool rather than
  as top-level tools, and runs an automatic review step before each MCP call.
  Irrelevant to isolation, relevant to how transcripts render tool calls.
- New MCP clients probe with a `server/discover` request before `initialize`;
  the gateway must answer unknown methods with a JSON-RPC error, not an HTTP
  error.

## What this means for the design

1. **roost-driven sessions should get their mounts through ACP
   `session/new` / `session/load`, not through the agent's global config.**
   Writing a gateway entry into `~/.claude.json` or `~/.codex/config.toml`
   would expose it to every hat on that host and to every terminal session.
   Renderers remain useful for standalone mode and for sessions started outside
   roost, where the operator chooses that trade-off explicitly.
2. **Claude:** full per-session isolation is available now (strict flag in
   `_meta`, re-sent on every load). It relies on an adapter pass-through that
   is not part of ACP, so the live e2e gate on every adapter pin bump must
   assert it (global probe must receive nothing). Trade-off: strict mode also
   hides the user's own servers and account connectors from roost sessions,
   which is consistent with the gateway being the single place MCP is managed.
3. **Codex:** isolation requires a roost-owned, **composed** `CODEX_HOME`
   (user's files shared by symlink, `config.toml` minus `mcp_servers`),
   re-composed on every adapter start, with one adapter process per hat (or
   per session). It preserves the user's setup and history. With this, the
   spec's §8.5 fallback is not needed for either agent on the measured
   versions; it stays as the rule for any agent without an isolation path.
4. **Gateway transport:** streamable HTTP only (Codex has no SSE transport).
5. **Server names:** namespace roost-injected servers to avoid collisions.

## Not measured

- Claude project `.mcp.json` scope (strict mode documents it as ignored).
- Plugin-provided MCP servers under strict mode (none were connected).
- Concurrent Codex app-servers from the user home and a composed home writing
  the same shared SQLite state at the same time.
- Several concurrent sessions sharing one adapter process with different
  mounts.
- Linux hosts.

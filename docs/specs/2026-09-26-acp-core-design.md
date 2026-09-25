# roost — ACP core (subsystem spec)

- **Date:** 2026-09-26
- **Status:** Draft, awaiting review
- **Refines:** [architecture spec](2026-09-25-roost-architecture-design.md)
  §5 (protocol), §6 (sessions), §11 (browser API) and §13 (frontend data
  flow). Where this document and the umbrella disagree, the umbrella wins until
  it is amended; every known disagreement is listed in §15.
- **Evidence:** [per-session MCP spike](../spikes/2026-09-25-per-session-mcp.md)
  and a behaviour catalogue of the predecessor's session machinery. Each hard
  rule below names the failure it prevents ("P-n" = predecessor incident, see
  §14).

The ACP core is everything that makes a browser-driven agent session work:
the host process and its adapters, the host↔collector protocol, the session
state machine, the collector's session storage, and the session REST/SSE API.
It does not cover the MCP gateway (own spec), auth and pairing mechanics (kept
in the umbrella §7, detailed in the distribution spec where they touch
install), or the frontend's rendering (own spec).

---

## 1. Crates and processes

The Cargo workspace (Rust, umbrella §9.1):

| Crate | Kind | Owns |
|---|---|---|
| `roost-proto` | lib | Every control frame, envelope and REST payload type. Generates JSON Schema and TypeScript. No I/O. |
| `roost-host` | lib | The host: connection manager, outbox, session actors, adapter supervisor, ACP client, capability profiles, projects/git probes. |
| `roost-sessions` | lib | Collector-side session module: ingest, state machine, storage, REST/SSE handlers, push triggers. |
| `roost-kernel` | lib | Operator auth, hats and path rules, SQLite pool and migrations, config, HTTP server scaffolding, push delivery. |
| `roost-gateway` | lib | MCP gateway (own spec). Depends on `roost-kernel`, never on `roost-sessions`. |
| `roost` | bin | CLI, supervisor, wiring. |

The frontend lives in `web/` and consumes the TypeScript generated from
`roost-proto` plus the official ACP TypeScript SDK types.

`roost-proto` uses `agent-client-protocol-schema` types only where roost
itself constructs ACP data (e.g. prompt content blocks). ACP payloads inside
envelopes are `serde_json::Value` / `RawValue`, so unknown fields survive
untouched (§3.2); the typed schema drops unknown fields on re-serialization.

---

## 2. Host architecture

```
roost host run
├── connection task      WS to collector: hello, auth proof, ping/deadline,
│                        reconnect with backoff+jitter, ack handling
├── outbox               on-disk (SQLite, host data dir); all session frames
│                        pass through it; drained in (session, seq) order
├── session actors       one tokio task per attached session; owns its adapter
│   └── adapter process  one OS process per session (umbrella §6.9),
│                        own process group, stdio JSON-RPC
├── probes               projects/browse, git state, agent availability
└── mcp renderer         standalone/terminal use only (gateway spec)
```

### 2.1 Lifetimes

- **Adapters belong to the host process, not to the WebSocket connection.** A
  dropped connection never touches a session actor. *(P-1: in the predecessor
  every adapter was spawned under the connection's context and torn down when
  the socket dropped, so a laptop sleeping, a collector restart or a network
  blip killed every session on the machine.)*
- A session actor lives from `start_session`/`resume_session` until close,
  idle reap, or adapter exit. It is the only code that talks to its adapter.

### 2.2 Session actor

Each actor is a single task with a mailbox. Everything that concerns one
session — frames from the collector, ACP messages from the adapter, timers —
is serialised through it. Consequences:

- **The connection task never blocks on a session.** Spawning an adapter,
  `initialize`, `session/new`, `session/load` and config switches run inside the
  actor. *(P-2: the predecessor ran start/resume/config on the connection read
  loop, so one slow `session/load` stalled permission answers and cancels for
  every other session on the host.)*
- **At most one turn is in flight per session** (§4.4).
- Per-session ordering of ACP notifications is preserved end to end: the
  adapter's stdout reader forwards notifications to the actor in arrival order,
  and the actor stamps `seq` in that order.

### 2.3 Adapter supervisor

- Spawned with `setsid` / a new process group, stdin/stdout piped for JSON-RPC,
  stderr captured into a 64 KiB ring buffer.
- **Environment:** the host's environment minus variables that make an agent
  refuse to start or double-report: `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
  `CLAUDE_CODE_SSE_PORT` *(P-3: "cannot be launched inside another Claude Code
  session")*, plus profile-specific additions (§6).
- **Exit watcher:** the supervisor awaits the child. On exit (any cause other
  than a requested close) it:
  1. fails every outstanding JSON-RPC call to that adapter;
  2. ends an in-flight turn with outcome `interrupted`;
  3. cancels every pending permission/elicitation with reason `adapter_lost`;
  4. emits `adapter_exited {code, signal, stderr_tail}`;
  5. moves the session to `parked`.

  *(P-4: the predecessor never watched the child. After a mid-turn crash the
  prompt call blocked until the socket died, the session showed `running`
  forever and the reaper could not free it.)*
- **Close and reap kill the whole process group** (SIGTERM, then SIGKILL after
  5 s). *(P-5: killing only the direct child left the agent CLI subtree alive;
  accumulated trees once exhausted a host's memory.)*

### 2.4 JSON-RPC client

- Requests to the adapter carry a host-local id; a table maps ids to waiters.
- **Inbound notifications are handed to the actor synchronously and in order.**
  Inbound requests from the adapter (permission, elicitation, fs, terminal) are
  dispatched without blocking the reader, since a permission request can wait
  hours for an answer that arrives through the same actor.
- On EOF the client fails all waiters (the exit watcher's step 1).
- **Crate usage (checked against `agent-client-protocol` 2.2.0 / schema 1.9.1
  and the adapters' bundled TypeScript SDK 1.5.0, both protocol version 1):**
  - Use the crate's **connection engine** (id routing, cancellation, batching)
    and its typed `send_request` for the host's own calls: `initialize`,
    `session/new`, `session/load`, `session/set_config_option`,
    `session/cancel`.
  - Register **`UntypedMessage`** handlers for `session/update` and for every
    adapter→client request that is forwarded to the browser, and forward
    `params` verbatim. Deserialize a *copy* into schema types only to extract
    indexed fields; a parse failure means "no indexed fields", never "drop".
    A typed notification handler that fails to parse is logged and dropped by
    the crate, so a new update kind from an adapter bump would silently
    vanish — the predecessor's lost-plan failure (umbrella §5.2).
  - **Spawn the adapter with `tokio::process`** (process group, composed
    environment, stderr capture, exit watcher) and connect it through the
    crate's byte-stream transport. The crate's own spawner uses a second async
    runtime and does not expose what §2.3 needs.
  - Methods absent from the Rust schema (e.g. Codex's legacy
    `session/set_model`) are sent as `UntypedMessage`.

### 2.5 ACP client-side capabilities

The host implements, for adapters that ask:

| Method | Behaviour |
|---|---|
| `fs/read_text_file` | Whole file, or `limit` lines from 1-based `line`. Line endings preserved. |
| `fs/write_text_file` | Implemented (mkdir -p, atomic write) but only **advertised** when the adapter profile says so (§6). |
| `terminal/create` | No `args` → `sh -c <command>`; with `args` → direct exec. Env appended to the host env. Own process group. Output ring buffer honouring `outputByteLimit` (default 1 MiB) with a `truncated` flag. A spawn failure is recorded as exit −1 with the reason in the output, never an empty success. *(P-6: a missing shell looked like "tools silently never run".)* |
| `terminal/output`, `wait_for_exit`, `kill`, `release` | Standard. `kill`/`release` signal the terminal's process group. |
| `session/request_permission` | §4.6. |
| `elicitation/create` | §4.6. |
| anything else | JSON-RPC `-32601 Method not found`. |

---

## 3. Host ↔ collector protocol

Transport, hello, versioning, schema source, seq/ack and liveness are fixed in
the umbrella §5. This section fills in the catalogue and the rules the umbrella
leaves open.

### 3.1 Frame envelope

```jsonc
{
  "v": 1,                       // protocol major, every frame
  "type": "session_event",      // tagged enum discriminator
  "request_id": "…",            // on requests and their responses only
  "session_id": "…",            // on session-scoped frames
  "seq": 1234,                  // on session-scoped host→collector frames
  // type-specific fields
}
```

- Session-scoped host→collector frames carry `seq` and go through the outbox.
  Connection-scoped frames (`hello`, probes, responses to probes) do not.
- Maximum frame size **32 MiB**. A frame that would exceed it is rejected at
  the sender with a visible error; it never closes the socket. *(P-7: an
  oversized update closed the predecessor's socket, which tore down every
  session on the host.)*

### 3.2 `session_event` and indexed fields

```jsonc
{ "type": "session_event", "session_id": "…", "seq": 42,
  "kind": "acp_update",          // acp_update | acp_request | host_note
  "indexed": { "activity": "running", "title": null, "turn_id": "t7" },
  "payload": { /* ACP message, verbatim */ } }
```

- `payload` is the ACP message exactly as received from the adapter, including
  unknown fields and `_meta`. The collector stores it as opaque JSON.
- `indexed` is the only thing the collector reads. The host extracts it
  because only the host understands ACP. Allowed keys are a closed set defined
  in `roost-proto`: `activity`, `title`, `turn_id`, `pending_request`,
  `agent_failure`, `plan_present`, `commands_changed`, `catalog_changed`.
- `kind: host_note` carries roost's own events that are not ACP messages
  (`adapter_exited`, `transcript_gap`, `turn_interrupted`), still stored on the
  session timeline.

### 3.3 Frame catalogue

**Collector → host (requests; all carry `request_id`, all get exactly one
response or a timeout):**

| Type | Key fields | Response |
|---|---|---|
| `hello_ack` | accepted protocol, collector version, server time | (reply to `hello`) |
| `start_session` | session_id (collector-minted), agent, cwd, model?, mode?, axes{}, first_prompt?, mcp_servers[], hat | `session_started` / `error` |
| `resume_session` | session_id, agent, cwd, model?, mode?, axes{}, mcp_servers[], hat | `session_started` / `error` |
| `prompt` | session_id, turn_id, content[] (ACP ContentBlocks) | `turn_accepted` / `error{code: turn_in_progress \| not_attached \| invalid}` |
| `cancel_turn` | session_id, turn_id | `ok` (the turn then ends with `cancelled`) |
| `close_session` | session_id | `ok` |
| `set_config` | session_id, config_id, value | `config_applied{config_options}` / `error` |
| `answer_permission` | session_id, request_id (ACP-side id), option_id | `answer_result{delivered}` |
| `answer_elicitation` | session_id, request_id, action, content? | `answer_result{delivered}` |
| `list_projects` | roots[] | `projects{items[], partial}` |
| `browse_directory` | path | `directory{entries[]}` / `error` |
| `resolve_path` | path | `resolved_path{canonical, exists, is_dir}` / `error` (kernel spec §5.4) |
| `apply_mcp_mounts` | manifest (gateway spec) | `ok` |
| `probe_agents` | — | `agents{…}` (same shape as in `hello`) |

**Host → collector:**

| Type | Key fields | Notes |
|---|---|---|
| `hello` | protocol, host_version, host_id, proof, capabilities[], agents[{id, version, available, auth}], attached_sessions[{session_id, last_seq, turn_id?}], outbox_from{session_id: seq} | First frame. Umbrella §5.3. |
| `session_started` | request_id, session_id, agent_session_id, config_options, commands | Catalogue is the **post-switch** one (§4.3). |
| `turn_accepted` | request_id, session_id, turn_id | The prompt reached the adapter. |
| `session_event` | as §3.2 | Outboxed. |
| `turn_ended` | session_id, seq, turn_id, outcome, stop_reason?, error? | Outboxed. Exactly one per accepted turn (§4.4). |
| `config_applied` | request_id, session_id, config_options | Authoritative read-back. |
| `answer_result` | request_id, session_id, delivered | Umbrella §6.8. |
| `session_parked` | session_id, seq, reason (`idle` \| `adapter_exited` \| `closed_by_host`) | Outboxed. |
| `session_closed` | session_id, seq | Outboxed. |
| `git_state` | session_id, seq, branch, dirty, worktree, head, base_commit? | After start and after each turn; bounded to 3 s. |
| `error` | request_id, code, message | Always correlated when answering a request. |
| `ack` | — | *(host never acks; collector→host `ack{session_id, ack_seq}` is the ack.)* |

Unknown frame types in either direction are logged (rate-limited) and
ignored; a frame of a known type that fails validation is answered with
`error{code: invalid}` if it had a `request_id`. Adding a type without a handler
does not compile (umbrella §5.4). *(P-8: the predecessor dropped unknown frames
silently and once shipped a result type with no collector handler; its tests
passed on a timeout.)*

### 3.4 Timeouts

| Request | Collector timeout |
|---|---|
| `start_session`, `resume_session` | 90 s (adapter spawn + `session/load` of a large session) |
| `prompt` (acceptance only) | 15 s |
| `set_config`, `cancel_turn`, `close_session` | 15 s |
| `answer_*` | 15 s |
| probes | 15 s |

Every error, including a failed resume, is correlated by `request_id` alone and
returns immediately. *(P-9: the predecessor's resume errors carried a session id
that its waiter did not match, so every failed resume cost the full 30 s
timeout.)*

### 3.5 Host authentication

`hello.proof` is an Ed25519 signature over `collector_nonce || host_id ||
protocol` with the key generated at pairing; the collector sends the nonce in
the WebSocket upgrade response header before the first frame. The host id is
never self-asserted without the proof. A revoked host gets
`hello_error{code: revoked}`. Pairing itself is in the umbrella §7.6.

*Rejected:* mTLS. It breaks behind TLS-terminating reverse proxies, which are
one of the three supported topologies (umbrella §7.5).

---

## 4. Sessions

### 4.1 Identity and immutables

- The collector mints `session_id` (UUIDv7) before the host is asked to start
  anything; the adapter's own id is stored as `agent_session_id`.
- **Immutable after creation:** host, agent, hat (re-assignment is an explicit
  logged operation, umbrella §8.2), cwd. Nothing derives them from events.
  *(P-10: an event with an empty machine field once blanked a session's host
  and made it permanently unresumable.)*

### 4.2 State machine

Lifecycle × activity as in the umbrella §6.3. Transitions (collector-side,
applied on ingest in `seq` order):

| From | Trigger | To |
|---|---|---|
| — | `start_session` sent | `starting` |
| `starting` | `session_started` | `active/idle` (or `active/running` if a first prompt was accepted) |
| `starting` | `error` / timeout | `failed` (reason stored) |
| `parked`, `closed`, `failed` | `resume_session` sent | `starting` |
| `active/idle` | `turn_accepted` | `active/running` |
| `active/running` | `indexed.pending_request` opened | `active/blocked` |
| `active/blocked` | last pending request resolved | `active/running` |
| `active/running`, `active/blocked` | `turn_ended` | `active/idle` |
| `active/*` | `session_parked` | `parked` |
| `active/*` | host offline > threshold | `parked` (presumed; §5.3) |
| any except `closed` | operator close | `closed` |

`starting` blocks a second resume: the collector answers 409 to a resume or
prompt while the session is `starting`. The host also refuses to attach a
session it already has attached and answers with the current state.
*(P-11: two concurrent resumes both passed the predecessor's "not live" check
and orphaned an adapter process.)*

### 4.3 Start and resume

**One request starts a session.** `start_session` carries agent, cwd, model,
mode, other config axes, the MCP servers for the session's hat and an optional
first prompt. The host:

1. spawns the adapter with the agent profile (§6);
2. `initialize`;
3. `session/new {cwd, mcpServers, _meta}` (§6 for `_meta`);
4. applies **model first, then other axes, then mode**, each via
   `session/set_config_option`, keeping the catalogue returned by the last
   successful switch;
5. sends `session_started` with that catalogue;
6. if a first prompt was supplied, runs it as turn 1.

*(P-12: the predecessor's browser issued five sequential calls with the
ordering rules living in the UI. A model switch can clamp the mode, so mode goes
last.)*

**Resume** is the same except step 3 is `session/load {sessionId, cwd,
mcpServers, _meta}` with replay suppression (§4.5), and step 4 re-applies the
stored model/axes/mode. The catalogue announced is the post-switch one.
*(P-13: announcing the pre-switch catalogue made the collector overwrite a
stored mode with the adapter default, and the next resume applied the default
for real.)*

A failed re-apply is logged on the timeline as a `host_note` and does not fail
the resume.

**Known load failures:**

- `-32002 Resource not found` means the agent has no record of the session,
  typically because it never completed a turn. The session becomes `failed`
  with reason `agent_has_no_record`, and the UI offers "start a new session in
  the same project". *(P-14.)*
- `-32000 Authentication required` means the agent CLI on that host is not
  logged in. Reason `agent_not_logged_in`; `roost doctor` names the fix.

### 4.4 Turns

- The collector mints `turn_id` for each accepted prompt.
- **One turn at a time.** A prompt while a turn is in flight is rejected with
  409 `turn_in_progress`; the UI disables Send while `running`/`blocked`.
  *(P-15: the predecessor allowed overlapping prompts; the first to finish
  cleared the "running" flag while the second was in flight, and the reaper
  could then reap mid-turn.)*
- **Empty prompts are rejected at the API** (400): no text and no image.
  *(P-16: an empty prompt reached the adapter and produced a `-32602 Invalid
  params` error on the session.)*
- **Every accepted turn ends with exactly one `turn_ended`**, outcome one of:
  - `completed` — the adapter returned a stop reason (stored as `stop_reason`);
  - `cancelled` — after `cancel_turn`;
  - `failed` — `session/prompt` returned an error (stored);
  - `interrupted` — adapter exit, host restart, or session close mid-turn.

  The host emits it for the first three; the collector synthesises
  `interrupted` when a host's `hello` does not list a session whose turn was
  open (§5.2). *(P-17: an error, a lost result or a disconnect left the
  predecessor's sessions `running` forever.)*
- Turn-in-flight covers `blocked`: a pending permission is inside a turn.

### 4.5 Replay suppression

While a `session/load` call is outstanding, the actor drops these update kinds
from the adapter instead of emitting them:

- `user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`
- `tool_call`, `tool_call_update`
- `plan`

and passes these through, because they describe the adapter's current state
rather than history:

- `available_commands_update`, `config_option_update`, `current_mode_update`,
  `usage_update`, `session_info_update`

Unknown update kinds received during load are dropped and counted in a
`host_note` so a new history-bearing kind cannot silently duplicate history.

*(P-18: without suppression every resume re-persisted the whole transcript
and a new "session started" row; ~50 sessions were affected before a fix and a
database cleanup. The predecessor's fix, in the collector, also dropped the
state kinds, which lost the adapter's corrective config update and forced the
post-switch-catalogue rule of §4.3.)*

### 4.6 Permission and elicitation

- The host answers the adapter only when the operator answers, the turn is
  cancelled, the session closes, or the adapter is lost. **No timeout.**
  *(Umbrella §6.5. The predecessor had a 5-minute permission timeout and none
  for elicitation; roost removes the asymmetry. Cost: an unanswered question
  pins one adapter process. The idle reaper never touches a turn in flight.)*
- The host assigns each request a `request_id` (`perm_…` / `elicit_…`),
  forwards the ACP request verbatim as `session_event{kind: acp_request,
  indexed.pending_request: {id, kind, opened: true}}`, and keeps the waiter.
- **The elicitation client capability is advertised as `{"form": {}}`**, never
  a boolean. *(P-19: a boolean is silently discarded by the adapter's schema
  validator and looks exactly like not advertising the capability; the agent
  then drops its "ask the user" tool.)*
- Answers: the collector validates `action ∈ {accept, decline, cancel}` and the
  option id against the stored request before forwarding. The host replies
  `answer_result{delivered}`: `true` if a waiter was still registered.
- **The pending request set is canonical in the collector** (`pending_request`
  table). The frontend drives actionability from it, not from timeline
  position. States: `open → delivered | dropped | cancelled(reason)`, reasons
  `turn_cancelled`, `session_closed`, `adapter_lost`, `host_restarted`.
- Several clients may answer; the first delivered answer wins and a later
  `delivered: false` never overwrites it (umbrella §6.8).

### 4.7 Idle reaper

Host-side, default 30 minutes, configurable, off with `0`. Reaps only sessions
with no turn in flight and no activity for the window. Emits
`session_parked{reason: idle}` and kills the process group.

### 4.8 Close

`close_session` → host cancels any turn (outcome `interrupted`), cancels
pending requests (`session_closed`), kills the process group, emits
`session_closed`. For a session that is not attached (parked, failed, host
offline) the collector closes it without contacting the host.

---

## 5. Reconnect and recovery

### 5.1 WebSocket drop, host survives

Session actors keep running and keep writing to the outbox. On reconnect,
`hello.attached_sessions` lists them with their last `seq` and open `turn_id`;
the host resends unacked frames; the collector reconciles and nothing is
parked.

### 5.2 Host restart

The host starts with no attached sessions. Its outbox still holds unacked
frames from before the restart, which it sends first. On `hello` the collector
compares `attached_sessions` with its own view of that host:

- sessions it believed `active` but not listed → `parked`
  (`reason: host_restarted`);
- if such a session had an open turn → synthesise `turn_ended{interrupted}`
  and cancel its pending requests with `host_restarted`.

Resume is explicit (operator or UI action). **No eager re-spawn on
reconnect.** *(P-20: the predecessor re-spawned every recently active session
on each reconnect, so a flapping connection churned adapters, and each
re-spawn risked the replay duplication of P-18.)*

### 5.3 Host offline

When a host's connection is gone for longer than the offline threshold
(default 10 minutes) the collector marks its `active` sessions `parked` with
`presumed: true` and a visible "host offline" note. Pending requests stay
`open` — the host may still hold them. On reconnect `hello` is authoritative:
listed sessions return to `active` with their pending requests intact.

### 5.4 Collector restart

Indistinguishable from 5.1 from the host's side. The collector rebuilds its
view from SQLite; sessions stay in whatever state was last committed until the
host's `hello` arrives.

### 5.5 Outbox

- SQLite file in the host data dir: `(session_id, seq, frame, created_at)`.
- Written before sending; deleted on `ack`. Drained per session in `seq` order.
- Bounded (default 64 MiB). On overflow the oldest unacked frames of the
  largest session are dropped and a `host_note{transcript_gap, from, to}` is
  enqueued in their place.
- `seq` counters are persisted with the outbox, so a restarted host continues
  a session's numbering instead of reusing seqs the collector has already seen.

---

## 6. Adapter profiles

A profile is data compiled into the host, selected by `agent`:

| | `claude` | `codex` | `generic` |
|---|---|---|---|
| Launch | managed Node + pinned `claude-agent-acp` | managed Node + pinned `codex-acp` | command from config |
| `fs.readTextFile` | yes | yes | yes |
| `fs.writeTextFile` | **no** *(P-21: advertising it disables the adapter's native write tools without enabling a replacement; the model then has no write tool and invents edits)* | no (writes in-process) | yes |
| `terminal` | yes | yes | yes |
| `elicitation` | `{"form": {}}` | `{"form": {}}` | `{"form": {}}` |
| Extra capabilities | — | `_meta.jetbrains.air = {version: 1, capabilities: ["sessionFailure"]}` inside `clientCapabilities` | — |
| Per-session MCP isolation | `_meta.claudeCode.options.extraArgs["strict-mcp-config"] = ""` on **every** `session/new` and `session/load` | composed `CODEX_HOME` for the adapter process (below) | none; mixed hosts get default-hat mounts only (umbrella §8.5) |
| MCP transport | `http` | `http` (no SSE) | from `initialize` |
| Extra env | `MAX_THINKING_TOKENS` when set | — | from config |

**Composed `CODEX_HOME`** (spike): at every Codex adapter spawn the host
builds `<host-data>/codex-home/<hat>/` by symlinking every top-level entry of
the user's `CODEX_HOME` except `config.toml`, and writing `config.toml` as the
user's file with all `mcp_servers` tables removed (parsed with a real TOML
parser, including inline and dotted forms). Entries that appear in the composed
directory but not in the user's are moved into the user's directory on the next
composition, so state Codex creates at runtime is not stranded.

**Server naming:** roost-injected MCP servers are named `roost-<slug>` so they
cannot collide with the user's own server names (the spike showed Codex
silently drops a session server whose name exists in config).

**Agent availability** in `hello.agents[]` and `probe_agents`:
`available` = the adapter can be launched; `auth` = `ok | missing | unknown`.
Auth is taken, in order, from the adapter's `_auth/status_update` notification
sent right after `initialize` (an underscore-prefixed extension both pinned
adapters emit: `kind: "account"` or `kind: "none"`), then from the bundled CLI
(`claude auth status`, `codex login status`; exit 0 = logged in), then
`unknown`. Account details in those payloads (email, organisation) are never
forwarded; only the boolean and the method. A `-32000` on first use still maps
to `agent_not_logged_in`.

**The adapters bundle their own agent CLI** (a platform-specific native
package resolved from the adapter's `node_modules`); they do not use the
`claude`/`codex` on the user's PATH, only the user's login state
(`~/.claude`, the macOS keychain, `~/.codex/auth.json`). The adapter pin
therefore decides the CLI version. `CLAUDE_CODE_EXECUTABLE` / `CODEX_PATH` are
exposed as an advanced override in the profile.

---

## 7. Content: images, commands, projects, git

- **Prompt content** is an array of ACP ContentBlocks built by the frontend
  (text and image blocks, in order). The collector validates: image MIME in
  {png, jpeg, gif, webp}, ≤ 5 MiB decoded each, ≤ 20 images, ≤ 24 MiB decoded
  total per prompt.
- **Images are stored** in the collector as attachments referenced by the
  user-turn event, so the transcript can show them later (retention follows the
  session). *(P-22: the predecessor never stored sent images; transcripts kept
  orphaned "[Image #N]" markers.)*
- **Slash commands** arrive as `available_commands_update` (passed through);
  the collector keeps the latest list per session and serves it from the
  catalogue endpoint, never in the session list.
- **Projects:** `list_projects` enumerates git repositories under the host's
  workspace roots (dot-dirs skipped, missing roots tolerated, 500-entry cap per
  root). `browse_directory` accepts only absolute paths that, **after symlink
  resolution**, lie under a workspace root or the user's home. The collector
  caches enumerations for 60 s and filters recents by hat.
  - Symlinks are resolved before matching everywhere (browse fence, hat path
    rules) because the agents themselves resolve them: the spike showed Claude
    keys project config by resolved path. This closes umbrella open question 5.
- **Git state** is reported after start and after every turn, bounded to 3 s;
  `base_commit` is recorded once per session.

---

## 8. Collector storage

SQLite, WAL, one writer task. Every table carries `owner_id`.

```sql
sessions(
  id TEXT PK, owner_id, host_id, hat_id, source_kind, agent, cwd,
  agent_session_id, title, lifecycle, activity, presumed_parked BOOL,
  failure_reason, model, mode, config_axes JSON,
  git_branch, git_dirty, git_worktree, base_commit,
  open_turn_id, created_at, last_event_at, last_event_id)
session_catalog(session_id PK, config_options JSON, commands JSON, updated_at)
host_agent_catalog(host_id, agent, config_options JSON, updated_at, PK(host_id, agent))
events(
  event_id INTEGER PK AUTOINCREMENT,   -- global SSE cursor
  session_id, host_seq, kind, indexed JSON, payload JSON, ts,
  UNIQUE(session_id, host_seq))
attachments(id PK, session_id, event_id, mime, bytes BLOB, created_at)
pending_requests(request_id PK, session_id, kind, payload JSON, state, reason, opened_at, resolved_at)
turns(turn_id PK, session_id, accepted_at, ended_at, outcome, stop_reason, error)
plans(session_id PK, payload JSON, updated_at)
```

- **Idempotent ingest:** `INSERT … ON CONFLICT(session_id, host_seq) DO
  NOTHING`, then ack. The ack is sent only after the transaction commits.
- **Collector-originated events** (user turns, operator actions, synthesised
  `turn_ended`) get `host_seq = NULL` and are still ordered by `event_id`.
- **Heavy blobs never ride the list:** the session list reads only `sessions`.
  Catalogues, commands and plans have their own endpoint. A test asserts the
  serialised list item stays under 1 KiB for a realistic session. *(P-23: the
  predecessor's session list grew to 5.6 MB for ~600 sessions because
  per-session command and config catalogues rode along, and every event
  refetched it.)*
- `host_agent_catalog` is filled from each `session_started` and powers the
  New-session pickers before a session exists.

---

## 9. REST and SSE API (sessions)

All endpoints require an operator session (umbrella §7.4). Types come from
`roost-proto`.

| Method & path | Purpose |
|---|---|
| `GET /api/sessions?cursor&limit&q&hat&lifecycle` | Paginated list, newest `last_event_at` first. `q` searches title, cwd, branch, id across all sessions regardless of filters except hat. |
| `POST /api/sessions` | Start: `{host_id, agent, cwd, model?, mode?, axes?, first_prompt?}` → 202 `{session_id}`. |
| `GET /api/sessions/{id}` | Session detail (list item + pending requests + open turn). |
| `GET /api/sessions/{id}/events?after=<event_id>&limit` | Timeline page. |
| `GET /api/sessions/{id}/catalog` | Config options, commands, plan. |
| `POST /api/sessions/{id}/resume` | 202; 409 if `starting`/`active`. |
| `POST /api/sessions/{id}/prompt` | `{content[]}` → 202 `{turn_id}`; 409 `turn_in_progress`; 400 empty. |
| `POST /api/sessions/{id}/cancel` | Cancel the open turn. |
| `POST /api/sessions/{id}/close` | Close (works when parked/offline). |
| `POST /api/sessions/{id}/config` | `{config_id, value}`; result via SSE `catalog_changed`. |
| `POST /api/sessions/{id}/requests/{request_id}/answer` | Permission or elicitation answer → 202; verdict via SSE. |
| `PATCH /api/sessions/{id}` | Rename; explicit hat re-assignment (logged). |
| `GET /api/attachments/{id}` | Image bytes, cache-immutable. |
| `GET /api/hosts/{id}/projects` / `…/browse?path=` | Project picker. |

**SSE** (umbrella §11.2), both resuming from `Last-Event-ID = event_id`, both
sending a comment keepalive every 15 s:

- `GET /api/stream/sessions` — list deltas: `session_upsert` (the full list
  item) and `session_removed`. Clients apply them to a keyed store and never
  refetch the list because of an event.
- `GET /api/stream/sessions/{id}` — every timeline event for one session plus
  `catalog_changed`, `pending_changed`, `turn_changed`.
- If `Last-Event-ID` is older than the retained catch-up window (default: the
  last 10 000 events), the server sends `resync_required` and closes; the
  client refetches the snapshot.
- Slow subscribers are disconnected with `resync_required`, never silently
  skipped. *(P-24: the predecessor's hub dropped messages for slow subscribers
  with no signal; the UI stayed wrong until the next unrelated event.)*

---

## 10. Push triggers

Evaluated on ingest, edge-triggered only:

| Edge | Default title / body |
|---|---|
| activity → `blocked` | `<session title>` / "needs your answer" |
| `turn_ended{completed}` | `<session title>` / "finished" |
| `turn_ended{failed}` or `agent_failure` (severity ≠ warning) | `<session title>` / "failed" |

- Title is the session title, falling back to the project directory name. No
  tool names, prompt text or transcript excerpts by default; per-hat settings
  may opt into more (umbrella §8.3). Hats can be muted.
- The payload carries `url: /sessions/<id>`; the service worker navigates an
  existing window there (frontend spec).
- Recovery and reconciliation never push. *(P-25: synthesising a turn end on
  reconnect would have pushed once per session per reconnect.)*
- Codex advisory notices (`sessionFailure` with `severity: "warning"`) are
  recorded but do not push; missing or unknown severities escalate (fail-safe).

---

## 11. Size and resource limits

| Limit | Default |
|---|---|
| WebSocket frame | 32 MiB |
| Prompt images | 20 × 5 MiB, 24 MiB total |
| Terminal output buffer | 1 MiB per terminal (or `outputByteLimit`) |
| Adapter stderr tail | 64 KiB |
| Outbox | 64 MiB |
| SSE catch-up window | 10 000 events |
| Idle reap | 30 min |
| Host offline threshold | 10 min |

All configurable; none silent when hit.

---

## 12. Testing

Beyond the umbrella §14:

**Fake adapter** (`roost-host` test support): a scripted ACP agent binary
driven by a scenario file. Scenarios that must exist, each run over the
in-memory pipe and a real WebSocket:

1. start with model+mode+axes; announced catalogue is post-switch; mode applied last.
2. resume: replayed history kinds dropped, state kinds kept; no duplicate events.
3. resume of a never-prompted session → `-32002` → `failed/agent_has_no_record`.
4. prompt during a turn → 409; empty prompt → 400.
5. adapter killed mid-turn → `turn_ended{interrupted}`, pending cancelled
   `adapter_lost`, `adapter_exited` with stderr tail, `parked`.
6. WS drop mid-turn → no session parked; outbox resent; no gap, no duplicate.
7. host restart mid-turn → collector synthesises `interrupted`, parks, cancels
   pending with `host_restarted`; no eager re-spawn.
8. host offline past threshold → `presumed` parked; reconnect with the adapter
   alive → `active`, pending intact.
9. permission answered by two clients → first `delivered`, second `dropped`,
   collector state `delivered`.
10. elicitation with no answer for a simulated 12 h → still open.
11. concurrent resume ×2 → one attach, one 409.
12. outbox overflow → `transcript_gap` event.
13. oversized frame → sender-side error, connection intact.
14. collector restart → host reconnects, nothing parked.
15. idle reap never during a turn or while blocked.
16. terminal spawn failure → exit −1 with reason; kill reaches grandchildren.

**Live gates** (real adapters, logged-in CI account, every pin bump):

- shell command actually executes (file created on disk);
- form elicitation round trip with `answer_result{delivered: true}`;
- model switch read-back: the adapter's reported `currentValue` equals the
  requested model; a bogus id is never reported as current;
- resume re-applies mode (bypass-type mode survives a host restart);
- Codex session writes a file with no client fs/terminal handlers;
- per-session MCP isolation: a global probe server receives nothing
  (the spike harness, automated).

---

## 13. Out of scope here

Observed sessions (the `source_kind` column reserves the room), worktree per
session, Changes tab, config explorer, auto-naming, memory. Gateway internals
(own spec). Install and service management (distribution spec). Rendering
(frontend spec).

---

## 14. Predecessor incidents referenced

| Id | Incident | roost rule |
|---|---|---|
| P-1 | Adapters died with the WebSocket | §2.1 |
| P-2 | Start/resume on the read loop stalled other sessions | §2.2 |
| P-3 | Nesting guard env vars made the agent refuse to start | §2.3 |
| P-4 | Adapter exit never detected; session `running` forever | §2.3 |
| P-5 | Orphaned agent process trees exhausted memory | §2.3 |
| P-6 | Terminal spawn failure looked like silent success | §2.5 |
| P-7 | Oversized frame closed the socket | §3.1 |
| P-8 | Unknown frames dropped silently; missing handler shipped | §3.3 |
| P-9 | Failed resume always waited the full timeout | §3.4 |
| P-10 | Event with empty host field made a session unresumable | §4.1 |
| P-11 | Concurrent resumes orphaned an adapter | §4.2 |
| P-12 | Start ordering lived in the browser | §4.3 |
| P-13 | Pre-switch catalogue overwrote the stored mode | §4.3 |
| P-14 | `session/load` of a never-prompted session fails | §4.3 |
| P-15 | Overlapping prompts broke the running flag | §4.4 |
| P-16 | Empty prompt produced an adapter error | §4.4 |
| P-17 | Turns without a terminal event left `running` forever | §4.4 |
| P-18 | Resume replay duplicated transcripts | §4.5 |
| P-19 | Boolean elicitation capability silently discarded | §4.6 |
| P-20 | Eager re-spawn on every reconnect | §5.2 |
| P-21 | Advertising fs write removed the model's write tools | §6 |
| P-22 | Sent images never stored | §7 |
| P-23 | Session list payload blew up to megabytes | §8 |
| P-24 | SSE slow subscribers silently skipped | §9 |
| P-25 | Reconnect-time synthetic events would push | §10 |

---

## 15. Changes to the umbrella spec

- §6.5: confirmed — no timeout for permission **and** elicitation (the
  predecessor had one for permissions only).
- §6.6 "host restart": sessions with an open turn get a *synthesised*
  `turn_ended{interrupted}` from the collector (the host cannot emit it after
  restarting).
- §4 data model: `turns`, `attachments`, `session_catalog`,
  `host_agent_catalog`, `plans` added.
- Open question 5 (symlinks): resolved — resolve before matching (§7).
- Open question 4 (host proof): resolved — Ed25519 signature over a collector
  nonce in `hello` (§3.5).
- Open question 2 (ACP Rust SDK coverage): resolved — crate engine plus raw
  payload handlers (§2.4).

## 16. Open questions

1. **Attachment retention.** Images live as long as their session. Is a size
   cap per installation needed in v1?

# roost — ACP core (subsystem spec)

- **Date:** 2026-09-26
- **Status:** Draft, awaiting review
- **Refines:** [architecture spec](2026-09-25-roost-architecture-design.md)
  §5 (protocol), §6 (sessions), §11 (browser API) and §13 (frontend data
  flow). This document is the authoritative home of the frame catalogue
  (§3.3) and the hello fields; the umbrella states the principles and points
  here.
- **Evidence:** [per-session MCP spike](../spikes/2026-09-25-per-session-mcp.md)
  and a behaviour catalogue of the predecessor's session machinery. Each hard
  rule below names the failure it prevents ("P-n" = predecessor incident, see
  §14).

The ACP core is everything that makes a browser-driven agent session work:
the host process and its adapters, the host↔collector protocol, the session
state machine, the collector's session storage, and the session REST/SSE API.
It does not cover the MCP gateway (own spec), operator auth, pairing and hats
([kernel spec](2026-09-26-kernel-design.md)), install and service management
([distribution spec](2026-09-26-distribution-design.md)), or the frontend's
rendering (own spec).

---

## 1. Crates and processes

The Cargo workspace (Rust, umbrella §9.1):

| Crate | Kind | Owns |
|---|---|---|
| `roost-proto` | lib | Every control frame, session body kind, collector event kind and REST payload type. Generates JSON Schema and TypeScript. No I/O. |
| `roost-host` | lib | The host: connection manager, outbox, session actors, adapter supervisor, ACP client, capability profiles, projects/git probes. |
| `roost-sessions` | lib | Collector-side session module: ingest, state machine, storage, REST/SSE handlers, push triggers. Depends on `roost-kernel` and on the `SessionMcp` trait of `roost-gateway`. |
| `roost-kernel` | lib | Operator auth, hosts and pairing, hats and path rules, SQLite pool and migrations, config, HTTP server scaffolding, outbound HTTP policy, push delivery, the hat purge hook. |
| `roost-gateway` | lib | MCP gateway (own spec), including the config renderers. Depends on `roost-kernel`, never on `roost-sessions`. |
| `roost` | bin | CLI (including `roost mcp apply`, which wires the gateway's renderers), supervisor, wiring. |

**Sessions → gateway interface.** `roost-sessions` obtains a session's MCP
servers through a trait that `roost-gateway` defines and implements:

```rust
trait SessionMcp {
    /// Mints the per-session gateway token and returns the servers to pass
    /// in `session/new` / `session/load` for this session.
    fn servers_for(&self, host_id: HostId, hat_id: HatId, session_id: SessionId)
        -> Result<Vec<McpServerSpec>>;
    /// Revokes the session's token (park, close, adapter exit, host revoke).
    fn revoke(&self, session_id: SessionId);
}
```

The dependency points sessions → gateway; the gateway never sees session
types. In `roost gateway` (standalone) the trait is simply unused.

The frontend lives in `web/` and consumes the TypeScript generated from
`roost-proto` plus the official ACP TypeScript SDK types.

`roost-proto` uses `agent-client-protocol-schema` types only where roost
itself constructs ACP data (e.g. prompt content blocks). ACP payloads inside
frames are `serde_json::Value` / `RawValue`, so unknown fields survive
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
└── probes               projects/browse, path resolution, git state, agent
                         availability
```

The host data directory layout (key, config, outbox, runtimes, lock) is
fixed in the distribution spec §8. A host holds an exclusive lock on
`host.lock`; a second `roost host run` against the same data directory refuses
to start.

### 2.1 Lifetimes

- **Adapters belong to the host process, not to the WebSocket connection.** A
  dropped connection never touches a session actor. *(P-1: in the predecessor
  every adapter was spawned under the connection's context and torn down when
  the socket dropped, so a laptop sleeping, a collector restart or a network
  blip killed every session on the machine.)*
- A session actor lives from `start_session`/`resume_session` until close,
  park, idle reap, or adapter exit. It is the only code that talks to its
  adapter.

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
- **Idempotency.** The host dedupes `start_session`/`resume_session` by
  `session_id` (already attached → re-emit the current state as a
  `session_started` carrying the new `request_id`; never a second adapter),
  `prompt` by `turn_id` (already seen → no second turn), and answers by
  `pending_id` (already resolved → `answer_result{delivered: false}`).

### 2.3 Adapter supervisor

- Spawned with `setsid` / a new process group, stdin/stdout piped for JSON-RPC,
  stderr captured into a 64 KiB ring buffer.
- **Environment:** the host's environment minus variables that make an agent
  refuse to start or double-report: `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`,
  `CLAUDE_CODE_SSE_PORT` *(P-3: "cannot be launched inside another Claude Code
  session")*, plus profile-specific additions (§6).
- **Exit watcher:** the supervisor awaits the child. On exit (any cause other
  than a requested close or park) it:
  1. fails every outstanding JSON-RPC call to that adapter;
  2. ends an in-flight turn with `turn_ended{outcome: interrupted}`;
  3. resolves every pending permission/elicitation with
     `pending_resolved{resolution: cancelled, reason: adapter_lost}`;
  4. emits `adapter_exited {code, signal, stderr_tail}`;
  5. emits `session_parked{reason: adapter_exited}`.

  *(P-4: the predecessor never watched the child. After a mid-turn crash the
  prompt call blocked until the socket died, the session showed `running`
  forever and the reaper could not free it.)*
- **Close, park and reap kill the whole process group** (SIGTERM, then SIGKILL
  after 5 s). *(P-5: killing only the direct child left the agent CLI subtree
  alive; accumulated trees once exhausted a host's memory.)*
- **Scrubbing.** Stderr tails and `host_note` text are scrubbed of token-like
  patterns (`Bearer …`, `sk-…`, `ghp_…`, `github_pat_…`, `xox[abp]-…` and
  similar) before they are emitted. ACP payloads are **not** scrubbed: they
  stay verbatim (§3.2), which means tool output that contains a secret is
  stored as-is in the collector. The documentation says so.

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
    `params` verbatim. Deserialize a *copy* into schema types only to fill the
    extracts (§3.2); a parse failure means "no extracts", never "drop".
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

Transport, versioning policy, schema source and liveness are fixed in the
umbrella §5. This section is the authoritative catalogue and the rules the
umbrella leaves to it.

### 3.1 Frames

Every frame is a JSON object with a `type` discriminator. The protocol version
appears only in `hello` / `hello_ack`, never per frame.

- **Requests** (collector → host) carry a `request_id`, used for correlation
  only.
- **Session frames** (host → collector) have the single type `session`:

  ```jsonc
  { "type": "session", "session_id": "…", "seq": 42,
    "body": { "kind": "acp_update", /* kind-specific fields */ } }
  ```

  Every session frame goes through the outbox and carries a per-session
  monotonic `seq` (§3.6).
- **Correlated responses** (host → collector, not outboxed, no `seq`) exist
  only for rejections (`error`) and connection-scoped probes.

**State-bearing facts always travel as session frames**, never only as a
correlated response. A request that changes session state is completed by the
outboxed fact that carries its `request_id` (or names its turn or session);
the collector resolves the waiting HTTP call when it **ingests** that fact.
*(If the answer to "did my prompt start?" travels only on the connection, a
socket drop at the wrong moment leaves the collector guessing; on the outbox it
is resent until acknowledged.)*

Maximum frame size **32 MiB**. A frame that would exceed it is rejected at the
sender with a visible error; it never closes the socket. *(P-7: an oversized
update closed the predecessor's socket, which tore down every session on the
host.)*

### 3.2 Session bodies and extracts

| `body.kind` | Fields | Notes |
|---|---|---|
| `acp_update` | `indexed`, `payload` | One ACP `session/update`, verbatim. |
| `session_started` | `request_id`, `agent_session_id`, `indexed` (catalogue extracts) | Catalogue is the **post-switch** one (§4.3). |
| `start_failed` | `request_id`, `code`, `reason`, `message` | Any failure after the host accepted a start/resume (spawn, `initialize`, `session/new`/`load`). |
| `turn_started` | `turn_id`, `request_id?` | The prompt reached the adapter. |
| `turn_ended` | `turn_id`, `outcome`, `stop_reason?`, `error?` | Exactly one per started turn (§4.4). |
| `pending_opened` | `pending_id`, `kind` (`permission` \| `elicitation`), `indexed`, `payload` | ACP request verbatim in `payload`. |
| `pending_resolved` | `pending_id`, `resolution` (`delivered` \| `cancelled`), `reason?` | |
| `answer_result` | `pending_id`, `request_id`, `delivered` | Umbrella §6.8. |
| `config_applied` | `request_id`, `indexed` (catalogue extracts) | Authoritative read-back after `set_config`. |
| `session_parked` | `reason` (`idle` \| `adapter_exited` \| `operator`) | |
| `session_closed` | — | |
| `transcript_gap` | `from_seq`, `to_seq` | Its own `seq` is `to_seq` (§5.5). |
| `adapter_exited` | `code`, `signal`, `stderr_tail` | Stderr tail scrubbed (§2.3). |
| `git_state` | `branch`, `dirty`, `worktree`, `head`, `base_commit?` | After start and after each turn; bounded to 3 s. |
| `host_note` | `note`, `text` | roost's own diagnostics that are not state (failed re-apply, dropped unknown kinds during load). Text scrubbed. |

- `payload` is the ACP message exactly as received from the adapter, including
  unknown fields and `_meta`. The collector stores it as opaque JSON and
  **never parses it**.
- `indexed` holds **typed extracts** the host fills because only the host
  understands ACP. The allowed keys are a closed set in `roost-proto`:

  | Extract | Meaning |
  |---|---|
  | `activity` | `running` \| `idle` |
  | `title` | Session title reported by the agent |
  | `turn_id` | Turn the update belongs to |
  | `pending` | `{id, kind, option_ids?}` (`option_ids` for permissions) |
  | `commands` | Available slash commands (full list) |
  | `config_options` | Config catalogue (full) |
  | `current_model`, `current_mode` | Current values |
  | `plan` | Plan entries (latest snapshot) |
  | `agent_failure` | `{severity}` |
  | `usage` | Context/token usage |
  | `text_projection` | Plain text of message chunks, for future search |

- `session_catalog`, `plans` and the `model`/`mode` columns (§8) are filled
  **from extracts only**. The collector validates answers against the stored
  `option_ids`, never against the payload.

### 3.3 Frame catalogue

**Collector → host requests** (all carry `request_id`):

| Type | Key fields | Completed by |
|---|---|---|
| `start_session` | session_id (collector-minted), committed_seq, agent, cwd (canonical), model?, mode?, axes{}, first_prompt?{turn_id, content[]}, mcp_servers[], hat | `session_started` \| `start_failed` |
| `resume_session` | session_id, committed_seq, agent, cwd, model?, mode?, axes{}, mcp_servers[], hat | `session_started` \| `start_failed` |
| `prompt` | session_id, turn_id, content[] (ACP ContentBlocks) | `turn_started` \| `error{turn_in_progress \| not_attached \| invalid}` |
| `cancel_turn` | session_id, turn_id | `turn_ended{cancelled}` for that turn |
| `park_session` | session_id | `session_parked{reason: operator}` (only to hosts with the `park` capability) |
| `close_session` | session_id | `session_closed` |
| `set_config` | session_id, config_id, value | `config_applied` \| `error` |
| `answer_permission` | session_id, pending_id, option_id | `answer_result` |
| `answer_elicitation` | session_id, pending_id, action, content? | `answer_result` |
| `list_projects` | — (roots come from the host's config, §7) | `projects{items[], partial}` |
| `browse_directory` | path | `directory{entries[]}` \| `error` |
| `resolve_path` | path | `resolved_path{canonical, exists, is_dir}` \| `error` (kernel spec §5.4) |
| `probe_agents` | — | `agents{…}` (same shape as in `hello`) |

**Collector → host, not requests:**

| Type | Key fields | Notes |
|---|---|---|
| `hello_ack` | protocol_version, collector_version, server_time, committed{session_id: seq} | Reply to `hello`; `committed` holds the collector's highest committed seq for every session listed in `attached_sessions` (§5.1). |
| `hello_error` | code (`incompatible` \| `revoked` \| `already_connected` \| `bad_proof`), message | Then the socket closes. |
| `ack` | session_id, ack_seq | Highest seq committed for that session (§3.6). |
| `forget_hat` | hat_id | Sent after each handshake for recently purged hats; the host deletes that hat's composed agent home once no process of the hat runs. Idempotent (kernel spec §5.5). |

**Host → collector:**

| Type | Key fields | Notes |
|---|---|---|
| `hello` | protocol_version, host_version, host_id, proof, capabilities[], agents[], workspace_roots[], attached_sessions[{session_id, last_seq, turn_id?}] | First frame. |
| `resend_complete` | — | All unacknowledged outbox frames have been resent (§5.1). |
| `session` | session_id, seq, body | Outboxed (§3.2). |
| `error` | request_id, code, message | Rejection of a request. |
| `projects`, `directory`, `resolved_path`, `agents` | request_id, … | Probe responses. |

`hello` fields:

- `capabilities` is a closed list: `projects` (project enumeration and
  browsing), `images` (image content blocks in prompts), `park` (explicit
  park). The collector never sends a frame, or a prompt containing images, to a
  host that lacks the capability; the UI hides the feature for that host.
- `agents[]`: per agent `{id, version, available, auth, catalog}` where
  `catalog` is the profile's **static default catalogue** (§6), so the
  New-session pickers work before the first session on a host exists.
- `workspace_roots[]`: from the host's config (§7).

Unknown frame types and unknown body kinds in either direction are logged
(rate-limited) and ignored; a frame of a known type that fails validation is
answered with `error{code: invalid}` if it had a `request_id`. Adding a type
without a handler does not compile (umbrella §5.4). *(P-8: the predecessor
dropped unknown frames silently and once shipped a result type with no
collector handler; its tests passed on a timeout.)*

### 3.4 Timeouts and disconnects

| Request | Collector timeout |
|---|---|
| `start_session`, `resume_session` | 90 s (adapter spawn + `session/load` of a large session) |
| `prompt` (acceptance only) | 60 s |
| `set_config`, `cancel_turn`, `park_session`, `close_session` | 60 s |
| probes | 15 s |

- Every timeout for a state-changing request is **at least the WebSocket read
  deadline** (45 s, umbrella §5.8), so while the connection is alive the fact
  or a rejection arrives first.
- **When the host connection drops**, the collector immediately fails every
  in-flight HTTP waiter for that host with "host disconnected; delivery
  unknown" and marks the affected start or turn as **awaiting
  reconciliation** — never as failed. Reconciliation happens after the host's
  resend and `hello.attached_sessions` (§5.1). A timeout on a live connection
  is reported the same way.
- A `starting` session found in SQLite after a **collector restart** is
  reconciled the same way when its host next connects.
- Every rejection, including a failed resume, is correlated by `request_id`
  alone and returns immediately. *(P-9: the predecessor's resume errors carried
  a session id that its waiter did not match, so every failed resume cost the
  full 30 s timeout.)*

Answers have no waiter: they are queued durably (§4.6).

### 3.5 Host authentication

`hello.proof` is an Ed25519 signature over `collector_nonce || host_id ||
protocol_version` with the key generated at pairing; the collector sends the
nonce in the WebSocket upgrade response header before the first frame. The
connection is unauthenticated until a valid `hello` arrives. The host id is
never self-asserted without the proof. A revoked host gets
`hello_error{revoked}` and then stops all its adapters; until it connects, its
adapters keep running (it cannot be reached). Pairing itself is in the kernel
spec §4.

**One live connection per host.** A second connection for a `host_id` that is
already connected is rejected with `hello_error{already_connected}`; the
collector never supersedes a connection silently. It closes the older
connection only if that one has missed its liveness deadline.

*Rejected:* mTLS. It breaks behind TLS-terminating reverse proxies, which are
one of the three supported topologies (umbrella §7.5).

### 3.6 Sequence numbers and acks

- The host stamps every session frame with a per-session monotonic `seq` when
  it enters the outbox; `seq` counters are persisted with the outbox.
- **Idempotent ingest on `(session_id, seq)`.** A duplicate with the same
  payload hash is acknowledged and discarded. A duplicate with a **different**
  payload hash is stored as a `conflict` event (§8) and acknowledged; it is
  never silently dropped.
- **Ack = the highest seq the collector has committed for that session** (not
  necessarily contiguous). The host deletes outbox rows with `seq ≤ ack_seq`.
- The collector groups ingest commits (at most every 50 ms) and acks after the
  commit.
- `seq` exists only for host → collector delivery; the browser's cursor is the
  collector's `event_id` (§9).

---

## 4. Sessions

### 4.1 Identity and immutables

- The collector mints `session_id` (UUIDv7) before the host is asked to start
  anything; the adapter's own id is stored as `agent_session_id`.
- **Immutable after creation:** host, agent, cwd (canonical). The hat is
  changed only by explicit re-assignment (§4.9). Nothing derives them from
  events. *(P-10: an event with an empty machine field once blanked a
  session's host and made it permanently unresumable.)*

### 4.2 State machine

Lifecycle × activity as in the umbrella §6.3. Transitions are applied on
ingest in `seq` order (host facts) or when the collector writes its own event
(§8); every transition writes an events row first.

| From | Trigger | To |
|---|---|---|
| — | `POST /api/sessions` (after hat resolution, §4.3) | `starting` |
| `starting` | `session_started` | `active/idle` (or `active/running` once a first prompt's `turn_started` arrives) |
| `starting` | `start_failed` | `failed` (reason stored) |
| `starting` | reconciliation finds no trace of the start | `failed` (`start_not_delivered`) |
| `parked`, `closed`, `failed` | resume requested | `starting` |
| `active/idle` | `turn_started` | `active/running` |
| `active/running` | `pending_opened` | `active/blocked` |
| `active/blocked` | last pending resolved | `active/running` |
| `active/running`, `active/blocked` | `turn_ended` | `active/idle` |
| `active/*` | `session_parked` | `parked` |
| `active/*` | host offline > threshold | `parked` (presumed; §5.3) |
| `active/*` | host revoked (kernel spec §4.3) | `parked` (presumed) |
| `active/*` | host restarted (§5.2) | `parked` |
| `active/*` (attached) | `session_closed` after operator close | `closed` |
| `parked`, `failed`, unattached | operator close | `closed` (immediately) |

`starting` blocks a second resume: the collector answers 409 to a resume or
prompt while the session is `starting`. The host also refuses to attach a
session it already has attached and re-emits the current state instead (§2.2).
*(P-11: two concurrent resumes both passed the predecessor's "not live" check
and orphaned an adapter process.)*

**Prompt or config on a session that is not attached** (parked, closed,
failed, or its host offline) → 409 `not_attached`; the UI offers resume.

### 4.3 Start and resume

**Hat resolution comes first.** `POST /api/sessions` → `resolve_path` on the
host (canonical cwd) → path-rule match (kernel spec §5.2) → the hat is stored
with the canonical cwd → `start_session`. A host that is offline cannot start
a session.

**One request starts a session.** `start_session` carries agent, cwd, model,
mode, other config axes, the MCP servers for the session (from
`SessionMcp::servers_for`, §1) and an optional first prompt
`{turn_id, content[]}`. The host:

1. spawns the adapter with the agent profile (§6);
2. `initialize`;
3. `session/new {cwd, mcpServers, _meta}` (§6 for `_meta`);
4. applies **model first, then other axes, then mode**, each via
   `session/set_config_option`, keeping the catalogue returned by the last
   successful switch;
5. emits `session_started` with that catalogue;
6. if a first prompt was supplied, runs it as turn 1 (`turn_started` carries
   its `turn_id`).

*(P-12: the predecessor's browser issued five sequential calls with the
ordering rules living in the UI. A model switch can clamp the mode, so mode goes
last.)*

**Resume** is the same except that the hat is **re-resolved** first, step 3 is
`session/load {sessionId, cwd, mcpServers, _meta}` with replay suppression
(§4.5), and step 4 re-applies the stored model/axes/mode. If the re-resolved
hat differs from the stored one, the resume is refused (409 `hat_mismatch`)
with a message naming both hats; the operator must re-assign explicitly
(§4.9). Every resume mints a fresh gateway token, superseding the previous
one. The catalogue announced is the post-switch one. *(P-13: announcing the
pre-switch catalogue made the collector overwrite a stored mode with the
adapter default, and the next resume applied the default for real.)*

A failed re-apply is logged on the timeline as a `host_note` and does not fail
the resume.

**Known load failures** (reported as `start_failed`):

- `-32002 Resource not found` means the agent has no record of the session,
  typically because it never completed a turn. The session becomes `failed`
  with reason `agent_has_no_record`, and the UI offers "start a new session in
  the same project". *(P-14.)*
- `-32000 Authentication required` means the agent CLI on that host is not
  logged in. Reason `agent_not_logged_in`; `roost doctor` names the fix.

### 4.4 Turns

- The collector mints `turn_id` for each prompt and records the turn as `sent`
  together with its content. **The user-turn event (and its attachments'
  references) is written only when `turn_started` is ingested**, so the
  timeline never shows a prompt the agent did not receive.
- A turn left `sent` by a disconnect is reconciled after the host's resend: if
  no `turn_started` arrived, the turn becomes `not_delivered` and the UI
  offers to send its content again. It is never shown as failed.
- **One turn at a time.** A prompt while a turn is in flight is rejected with
  409 `turn_in_progress`; the UI disables Send while `running`/`blocked`.
  *(P-15: the predecessor allowed overlapping prompts; the first to finish
  cleared the "running" flag while the second was in flight, and the reaper
  could then reap mid-turn.)*
- **Empty prompts are rejected at the API** (400): no text and no image.
  *(P-16: an empty prompt reached the adapter and produced a `-32602 Invalid
  params` error on the session.)*
- **Every started turn ends with exactly one `turn_ended`**, outcome one of:
  - `completed` — the adapter returned a stop reason (stored as `stop_reason`);
  - `cancelled` — after `cancel_turn`;
  - `failed` — `session/prompt` returned an error (stored);
  - `interrupted` — adapter exit, session close or park mid-turn, or a host
    restart.

  The host emits it in every case it can observe, including adapter exit and
  close mid-turn. The collector synthesises `turn_ended{interrupted}` only when
  a host restart is detected (§5.2). A `turn_ended` for a turn that has already
  ended is stored but not applied and never pushes. *(P-17: an error, a lost
  result or a disconnect left the predecessor's sessions `running` forever.)*
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
  cancelled, the session closes or parks, or the adapter is lost. **No
  timeout.** *(Umbrella §6.5. The predecessor had a 5-minute permission
  timeout and none for elicitation; roost removes the asymmetry. Cost: an
  unanswered question pins one adapter process. The idle reaper never touches
  a turn in flight.)*
- The host assigns each request a **`pending_id`** (random UUID, globally
  unique), emits `pending_opened{pending_id, kind, payload}` with
  `indexed.pending = {id, kind, option_ids}`, and keeps the waiter. The name
  `request_id` is reserved for request/response correlation.
- **The elicitation client capability is advertised as `{"form": {}}`**, never
  a boolean. *(P-19: a boolean is silently discarded by the adapter's schema
  validator and looks exactly like not advertising the capability; the agent
  then drops its "ask the user" tool.)*
- **Answers.** The collector validates `action ∈ {accept, decline, cancel}`
  and the option id against the stored `option_ids`, writes an
  `answer_submitted` event and stores the answer in a **durable answer queue**
  keyed by `pending_id`, then returns 202. The queue is drained to the host
  immediately if it is connected, otherwise on its next connection (after
  `resend_complete`). The host dedupes by `pending_id` and replies with
  `answer_result{pending_id, request_id, delivered}`: `true` if a waiter was
  still registered; a delivered answer is followed by
  `pending_resolved{delivered}`.
- An answer for a pending that is not `open`, or that already has a queued
  answer, → 409 (`not_open` / `already_answered`).
- **The pending set is canonical in the collector** (`pending` table). The
  frontend drives actionability from it, not from timeline position. States:
  `open → delivered | cancelled(reason)`, reasons `turn_cancelled`,
  `session_closed`, `session_parked`, `adapter_lost`, `host_restarted`.
- Verdicts from several clients are folded monotonically: `delivered` sticks
  and a later `delivered: false` never overwrites it (umbrella §6.8).

### 4.7 Idle reaper

Host-side, default 30 minutes, configurable, off with `0`. Reaps only sessions
with no turn in flight and no activity for the window. Emits
`session_parked{reason: idle}` and kills the process group.

### 4.8 Park and close

- **Park** (`POST …/park`, `park_session`): the host ends any turn
  (`interrupted`), cancels pending requests (`session_parked`), kills the
  process group and emits `session_parked{reason: operator}`.
- **Close, attached session:** the collector writes `operator_closed` and sends
  `close_session`; the host ends any turn (`interrupted`), cancels pending
  requests (`session_closed`), kills the process group and emits
  `session_closed`. The lifecycle becomes `closed` when that fact is
  ingested.
- **Close, unattached session** (parked, failed, host offline): closed
  immediately collector-side. On the host's next `hello`, any attached session
  that the collector has closed — or whose host or hat was revoked or
  re-assigned — receives `close_session`.
- The session's gateway token is revoked on host-reported `session_parked`
  and `session_closed`, on adapter exit, on a close of an unattached session
  and on host revoke (`SessionMcp::revoke`). A presumed park
  (`presumed_parked{host_offline}`) does **not** revoke it: the host may still
  be running the session, which would then return with a dead token and no
  resume to mint a new one.

### 4.9 Hat re-assignment

Allowed for any session with no running adapter (`parked`, `closed` or `failed`), with a warning in the UI that
the agent's stored history came from the old hat; the change writes a
`hat_reassigned{from, to}` event. The next resume re-resolves the path and must
agree with the new hat, or the operator must also change the path rules.

### 4.10 Delete

`DELETE /api/sessions/{id}` (step-up required, kernel spec §3.4) closes an
attached session first, then deletes its events, turns, pending rows and
answer queue entries; attachment files no longer referenced by any event are
removed. A `session_deleted` tombstone event (no content) remains so the list
stream can emit `session_removed` with an `event_id`. Per-hat purge (kernel
spec §5.5) deletes every session of the hat the same way.

---

## 5. Reconnect and recovery

### 5.1 Handshake

1. The host sends `hello` as its first frame, with the proof and the list of
   attached sessions.
2. The collector verifies the proof and replies `hello_ack`, whose
   `committed` map carries its highest committed seq for every session in
   `attached_sessions`.
3. The host deletes outbox rows at or below those seqs, resends every other
   unacknowledged row in `(session, seq)` order (rows of sessions no longer
   attached included; duplicates are discarded by idempotent ingest), and
   sends `resend_complete`.
   - If `hello_ack` reports a higher seq for a session than the host's own
     counter (the outbox was lost or reset), the host **fast-forwards** its
     counter past it, so new frames are never mistaken for duplicates.
     `start_session` and `resume_session` carry the same `committed_seq` for
     their session, so a session that was not attached at handshake time (an
     old session resumed after the outbox was lost) is fast-forwarded too: the
     host continues from the larger of its own counter and `committed_seq`.
4. **Only after `resend_complete`** the collector reconciles:
   - sessions it believes `active` on that host but not in
     `attached_sessions` are parked as described in §5.2;
   - sessions `presumed` parked but listed return to `active`;
   - starts and turns left awaiting reconciliation (§3.4) are resolved: a
     start with neither `session_started` nor `start_failed` ingested becomes
     `failed{start_not_delivered}`; a turn with no `turn_started` becomes
     `not_delivered`;
   - attached sessions the collector has closed, or whose hat was re-assigned,
     receive `close_session` (§4.8);
   - the answer queue for that host is drained.

   Reconciling earlier would park sessions whose facts were still in flight.

### 5.2 Host restart

The host starts with no attached sessions. Its outbox still holds unacked
frames from before the restart, which it resends first (§5.1). After
`resend_complete` the collector compares `attached_sessions` with its own
view of that host and, for sessions it believed `active` but not listed,
writes a `host_restarted` event that:

- parks the session;
- if it had an open turn, synthesises `turn_ended{interrupted}`
  (`turn_ended_synthesized` event);
- cancels its pending requests with `host_restarted`.

Resume is explicit (operator or UI action). **No eager re-spawn on
reconnect.** *(P-20: the predecessor re-spawned every recently active session
on each reconnect, so a flapping connection churned adapters, and each
re-spawn risked the replay duplication of P-18.)*

### 5.3 Host offline

When a host's connection is gone for longer than the offline threshold
(default 10 minutes) the collector writes `presumed_parked` for its `active`
sessions: lifecycle `parked`, `presumed: true`, and a visible "host offline"
note. Pending requests stay `open` — the host may still hold them. After the
next handshake, listed sessions return to `active` (`reattached` event) with
their pending requests intact.

### 5.4 Collector restart

Indistinguishable from 5.1 from the host's side. The collector rebuilds its
view from SQLite; sessions stay in whatever state was last committed until
the host's handshake completes. `starting` sessions are reconciled then
(§3.4).

### 5.5 Outbox

- SQLite file in the host data dir: `(session_id, seq, frame, created_at)`.
- Written before sending; deleted on `ack` (`seq ≤ ack_seq`). Drained per
  session in `seq` order.
- **Coalescing.** Before enqueuing, the host merges consecutive
  `agent_message_chunk` / `agent_thought_chunk` updates of the same kind and
  message for up to ~100 ms into one `acp_update` whose text is the
  concatenation of theirs. This cuts write load for fast token streams; the
  merged update is what the adapter would have sent as one chunk.
- **Bounded** (default 64 MiB). On overflow the host **never drops
  state-bearing frames** (every body kind except `acp_update`). It drops, oldest
  first, `acp_update` frames carrying message or thought chunks, then
  `acp_update` frames carrying large tool output. Each contiguous dropped range
  is replaced by a `transcript_gap{from_seq, to_seq}` frame whose own `seq` is
  the last dropped seq, so acks keep advancing. If only state-bearing frames
  remain, the outbox grows past the bound and the host reports it (Hosts view,
  `doctor`); loss is allowed only if it is visible.

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
| Per-session MCP isolation | `_meta.claudeCode.options.extraArgs["strict-mcp-config"] = ""` on **every** `session/new` and `session/load` | composed `CODEX_HOME` (below); on a mixed host, fallback until measured | none; fallback |
| MCP transport | `http` | `http` (no SSE) | from `initialize` |
| Default catalogue | static, shipped with the profile | static | from config or empty |
| Extra env | `MAX_THINKING_TOKENS` when set | — | from config |

**Fallback** (umbrella §8.5): on a mixed host, a session of that agent in a
non-default hat gets no gateway MCP servers, and sessions in the default hat
get only default-hat mounts; the UI says so at session start and on the mount
grid.

**Composed `CODEX_HOME`** (spike): the host builds
`<host-data>/codex-home/<hat>/` from an **allowlist** of shared entries,
symlinked to the user's `CODEX_HOME`: `auth.json`, `AGENTS.md`, `skills/`, and
`sessions/` (so a session can be resumed from the terminal). `config.toml` is
written as the user's file with all `mcp_servers` tables removed (parsed with
a real TOML parser, including inline and dotted forms). **Every other entry is
private to that hat's composed home.** The home is composed once per hat under
a lock, and never while a Codex process of that hat is running; entries are
never moved back into the user's directory.

Whether several Codex processes can safely share the symlinked state
concurrently is **unmeasured**. Until a live gate measures it, Codex on a
mixed host uses the fallback; on a single-hat host Codex runs with the
composed home and full mounts.

**Adapter overrides.** Any override (a custom adapter command,
`CLAUDE_CODE_EXECUTABLE`, `CODEX_PATH`) drops that agent to the fallback,
visibly, unless the operator explicitly accepts unverified isolation in the
host's config.

**Server naming:** roost-injected MCP servers are named `roost-<slug>` so they
cannot collide with the user's own server names (the spike showed Codex
silently drops a session server whose name exists in config).

**What isolation does not cover.** Strict mode and the composed home isolate
**MCP servers only**. Every Claude session still loads the user's
`~/.claude` configuration — `CLAUDE.md` and its imports, auto-memory, hooks,
skills and plugins (whether plugin MCP servers load under strict mode is
unmeasured). Codex loads the global `AGENTS.md` and any `notify` command.
These are accidental cross-hat channels outside roost's control in v1
(umbrella §8.4; §15, open question 2).

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
therefore decides the CLI version.

---

## 7. Content: images, commands, projects, git

- **Prompt content** is an array of ACP ContentBlocks built by the frontend
  (text and image blocks, in order). The collector validates: image MIME in
  {png, jpeg, gif, webp}, ≤ 5 MiB decoded each, ≤ 20 images, **≤ 16 MiB
  decoded in total** per prompt.
- **Images are stored** in the collector as content-addressed files in the
  data directory (named by SHA-256) and referenced from the user-turn event,
  so the transcript can show them later (retention follows the session).
  *(P-22: the predecessor never stored sent images; transcripts kept orphaned
  "[Image #N]" markers.)*
- **Slash commands** arrive as `available_commands_update` (passed through,
  with a `commands` extract); the collector keeps the latest list per session
  and serves it from the catalogue endpoint, never in the session list.
- **Projects:** workspace roots are configured **on the host** (`host.toml`
  in the host data dir, or `--workspace-root` flags) and reported in `hello`.
  `list_projects` enumerates git repositories under them (dot-dirs skipped,
  missing roots tolerated, 500-entry cap per root). `browse_directory` accepts
  only absolute paths that, **after symlink resolution**, lie under a
  workspace root or the user's home. The collector caches enumerations for
  60 s and filters recents by hat.
  - Symlinks are resolved before matching everywhere (browse fence, hat path
    rules) because the agents themselves resolve them: the spike showed Claude
    keys project config by resolved path.
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
session_catalog(session_id PK, config_options JSON, commands JSON, usage JSON, updated_at)
host_agent_catalog(host_id, agent, config_options JSON, updated_at, PK(host_id, agent))
events(
  event_id INTEGER PK AUTOINCREMENT,   -- global SSE cursor
  session_id, host_seq NULL, payload_hash, kind, indexed JSON, payload JSON, ts,
  UNIQUE(session_id, host_seq))
attachments(sha256 PK, owner_id, mime, size, created_at)   -- file: <data>/attachments/<sha256>
event_attachments(event_id, sha256, position)
pending(pending_id PK, session_id, kind, option_ids JSON, payload JSON, state, reason, opened_at, resolved_at)
answer_queue(pending_id PK, session_id, request_id, answer JSON, state, submitted_at, delivered BOOL NULL)
turns(turn_id PK, session_id, request_id, state, content JSON, sent_at, started_at, ended_at, outcome, stop_reason, error)
plans(session_id PK, entries JSON, updated_at)
```

- **Idempotent ingest:** `INSERT … ON CONFLICT(session_id, host_seq) DO
  NOTHING`; a conflicting row whose `payload_hash` differs is recorded as a
  `conflict` event. The ack is sent only after the transaction commits.
- **Collector-originated events** have `host_seq = NULL` and are ordered by
  `event_id`. Their kinds are a closed enum in `roost-proto`:

  | Kind | Written when |
  |---|---|
  | `operator_started`, `operator_resumed` | A start or resume is requested (→ `starting`). |
  | `user_turn` | `turn_started` is ingested for an operator prompt. |
  | `operator_renamed`, `operator_closed`, `operator_parked` | Operator actions. |
  | `hat_reassigned` | §4.9. |
  | `presumed_parked{reason: host_offline \| host_revoked}`, `reattached` | Host offline or revoked / back (§5.3). |
  | `host_restarted` | §5.2. |
  | `turn_ended_synthesized` | §5.2. |
  | `start_not_delivered`, `turn_not_delivered` | Reconciliation (§5.1). |
  | `answer_submitted` | §4.6. |
  | `conflict` | Same `(session_id, seq)` with a different payload. |
  | `session_deleted` | Tombstone after delete (§4.10). |

- **`sessions`, `session_catalog`, `plans` and the model/mode columns are
  filled from extracts** and from the fields of typed bodies, never by parsing
  ACP payloads.
- **Heavy blobs never ride the list:** the session list reads only `sessions`.
  Catalogues, commands and plans have their own endpoint. A test asserts the
  serialised list item stays under 1 KiB for a realistic session. *(P-23: the
  predecessor's session list grew to 5.6 MB for ~600 sessions because
  per-session command and config catalogues rode along, and every event
  refetched it.)*
- `host_agent_catalog` starts from the static catalogues in `hello` /
  `probe_agents` and is refined from each `session_started`; it powers the
  New-session pickers before a session exists.
- Gateway tokens and the `headers` of `mcp_servers` entries are never written
  to the events table and never sent over SSE.

---

## 9. REST and SSE API (sessions)

All endpoints require an operator session (kernel spec §3). Types come from
`roost-proto`.

| Method & path | Purpose |
|---|---|
| `GET /api/sessions?cursor&limit&q&hat&lifecycle` | Paginated list, newest `last_event_at` first. `q` searches title, cwd, branch, id across all sessions regardless of filters except hat. |
| `POST /api/sessions` | Start: `{host_id, agent, cwd, model?, mode?, axes?, first_prompt?{content[]}}` → 202 `{session_id, turn_id?}`. |
| `GET /api/sessions/{id}` | Session detail (list item + pending + open turn). |
| `GET /api/sessions/{id}/events?before=<event_id>&limit` | Timeline page ending before an event; without `before`, the tail. The frontend opens at the tail. |
| `GET /api/sessions/{id}/events?after=<event_id>&limit` | Timeline page after an event. |
| `GET /api/sessions/{id}/catalog` | Config options, commands, plan, usage. |
| `POST /api/sessions/{id}/resume` | 202; 409 if `starting`/`active`; 409 `hat_mismatch` (§4.3). |
| `POST /api/sessions/{id}/prompt` | `{content[]}` → 202 `{turn_id}` once `turn_started` is ingested; 409 `turn_in_progress` / `not_attached`; 400 empty; 503 "host disconnected; delivery unknown". |
| `POST /api/sessions/{id}/cancel` | Cancel the open turn. |
| `POST /api/sessions/{id}/park` | Explicit park (hosts with the `park` capability). |
| `POST /api/sessions/{id}/close` | Close (works when parked/offline). |
| `DELETE /api/sessions/{id}` | Delete (§4.10); step-up required. |
| `POST /api/sessions/{id}/config` | `{config_id, value}`; 409 `not_attached`; result via SSE `catalog_changed`. |
| `POST /api/sessions/{id}/pending/{pending_id}/answer` | Permission or elicitation answer → 202 once queued; 409 `not_open` / `already_answered`; verdict via SSE. |
| `PATCH /api/sessions/{id}` | Rename; hat re-assignment (no running adapter, §4.9). |
| `GET /api/attachments/{sha256}` | Image bytes, cache-immutable. |
| `GET /api/hosts/{id}/projects` / `…/browse?path=` | Project picker. |
| `GET /api/hosts/{id}/agents` | Agents, availability, auth and catalogues for that host. |

**SSE** (umbrella §11.2). **Every state change first writes an events row, and
every SSE message carries the `event_id` that caused it** as its `id:`; both
streams send a comment keepalive every 15 s.

- `GET /api/stream/sessions/{id}` — every timeline event for one session plus
  `catalog_changed`, `pending_changed`, `turn_changed`. It resumes from
  `Last-Event-ID` **directly from the events table**; there is no catch-up
  window.
- `GET /api/stream/sessions` — list deltas: `session_upsert` (the full list
  item) and `session_removed` (only on delete). Clients apply them to a keyed
  store and never refetch the list because of an event. It resumes from
  `Last-Event-ID` within a **24-hour** window; beyond it the server sends
  `resync_required` and closes, and the client refetches the snapshot.
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
  may opt into more (umbrella §8.3), or into a **generic title** ("Session
  needs your answer", with no session title at all). Hats can be muted.
- The payload carries `url: /sessions/<id>`; the service worker navigates an
  existing window there (frontend spec).
- Recovery and reconciliation never push, and a `turn_ended` for an already
  ended turn never pushes. *(P-25: synthesising a turn end on reconnect would
  have pushed once per session per reconnect.)*
- Codex advisory notices (`sessionFailure` with `severity: "warning"`) are
  recorded but do not push; missing or unknown severities escalate (fail-safe).

---

## 11. Size and resource limits

| Limit | Default |
|---|---|
| WebSocket frame | 32 MiB |
| Prompt images | 20 × ≤ 5 MiB, ≤ 16 MiB decoded in total |
| Terminal output buffer | 1 MiB per terminal (or `outputByteLimit`) |
| Adapter stderr tail | 64 KiB |
| Outbox | 64 MiB (state-bearing frames kept beyond it, reported) |
| Chunk coalescing window | ~100 ms |
| Ingest commit batch | ≤ 50 ms |
| List-stream resume window | 24 h |
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
4. prompt during a turn → 409; empty prompt → 400; prompt to a parked session → 409 `not_attached`.
5. adapter killed mid-turn → `turn_ended{interrupted}`, pending cancelled
   `adapter_lost`, `adapter_exited` with scrubbed stderr tail, `parked`.
6. WS drop mid-turn → no session parked; outbox resent; no gap, no duplicate;
   the in-flight prompt's HTTP call fails "delivery unknown" and the turn is
   reconciled from the resent `turn_started`.
7. host restart mid-turn → after `resend_complete` the collector synthesises
   `interrupted`, parks, cancels pending with `host_restarted`; no eager
   re-spawn; reconciliation never runs before `resend_complete`.
8. host offline past threshold → `presumed` parked; reconnect with the adapter
   alive → `active`, pending intact.
9. answer queued while the host is offline → delivered after reconnect;
   a second answer for the same pending → 409 `already_answered`; a resent
   answer is deduped by `pending_id`.
10. elicitation with no answer for a simulated 12 h → still open.
11. concurrent resume ×2 → one attach, one 409; a duplicate `start_session`
    re-emits state without a second adapter.
12. outbox overflow → only chunk frames dropped, `transcript_gap` with
    `seq = to_seq`, acks advance, no state-bearing frame lost.
13. oversized frame → sender-side error, connection intact.
14. collector restart → host reconnects, nothing parked; a `starting` session
    is reconciled.
15. idle reap never during a turn or while blocked.
16. terminal spawn failure → exit −1 with reason; kill reaches grandchildren.
17. outbox lost on the host → `hello_ack` seq higher than the host's counter →
    counter fast-forwarded; new frames not discarded as duplicates. Same for
    a resume of an unattached session after outbox loss (`committed_seq` in
    `resume_session`).
18. same `(session_id, seq)` with a different payload → `conflict` event.
19. second connection for a connected `host_id` → `already_connected`; the
    older connection is closed only after it misses its deadline.
20. resume where the path now resolves to another hat → 409 `hat_mismatch`.

**Live gates** (real adapters, logged-in CI account, every pin bump):

- shell command actually executes (file created on disk);
- form elicitation round trip with `answer_result{delivered: true}`;
- model switch read-back: the adapter's reported `currentValue` equals the
  requested model; a bogus id is never reported as current;
- resume re-applies mode (bypass-type mode survives a host restart);
- Codex session writes a file with no client fs/terminal handlers;
- per-session MCP isolation: a global probe server receives nothing
  (the spike harness, automated);
- gateway token exposure: while a Claude session runs, the process list is
  searched for the session's token (gateway spec §3.2); the result is recorded
  per pin. Codex is measured the same way.

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

## 15. Open questions

1. **Attachment retention.** Images live as long as their session. Is a size
   cap per installation needed in v1?
2. **Per-hat "isolate agent user config".** Should a hat be able to stop its
   sessions from loading the user's own agent configuration (§6)? For Claude
   this might be done through the SDK's `settingSources` passed in `_meta`
   (unverified); for Codex by leaving `AGENTS.md` out of the composed home and
   stripping `notify`. Both cost the operator their personal agent setup in
   that hat.
3. **Concurrent Codex processes sharing state** through the composed home —
   needs a live gate before Codex leaves the fallback on mixed hosts (§6).

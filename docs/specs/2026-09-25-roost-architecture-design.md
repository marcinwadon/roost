# roost — product architecture (umbrella spec)

- **Date:** 2026-09-25
- **Status:** Draft, awaiting review
- **Scope:** the whole product at the level of processes, protocols, state
  ownership, trust boundaries and module seams. Each subsystem (ACP core, MCP
  gateway, distribution) gets its own spec that refines this one; where they
  disagree, this document wins until it is amended.
- **Language:** Rust (decided 2026-09-26, §9.1). The frontend is TypeScript.

Throughout, "the predecessor" means an earlier private prototype of the same
idea that ran for several months on one developer's machines. It is the source
of most of the hard rules below; each rule names the failure it prevents.

---

## 1. What roost is

roost is a self-hosted, open-source cockpit for coding agents. One developer
drives [Agent Client Protocol](https://agentclientprotocol.com) (ACP) sessions
running on any of their machines from a browser or phone: start a session in a
project, watch the transcript live, answer the agent's permission requests and
questions, resume it tomorrow on another device.

Two equally important modules ship in one binary:

1. **ACP core** — hosts, sessions, transcripts, permissions, push.
2. **MCP gateway** — authorize an MCP integration once, centrally, and decide
   which hosts and projects may use it. Also runnable on its own
   (`roost gateway`) with no hosts and no ACP.

### 1.1 Product principles

- **Self-hosted, single operator per installation (v1).** No SaaS, no
  multi-tenancy, no billing. Every persisted row nevertheless carries
  `owner_id`, so teams can be added later without a schema rewrite.
- **Bring your own agent.** Users log in to each agent CLI themselves
  (`claude /login`, `codex login`). roost never proxies, pools or stores agent
  licences.
- **Vendor-neutral.** ACP is an open protocol. Claude (`claude-agent-acp`) and
  Codex (`codex-acp`) are first-class; any ACP adapter can be added by config.
- **Small by default, scales out.** The minimum deployment is one machine
  running both roles. The same code serves one collector and many hosts.

### 1.2 v1 scope

In:

- Host pairing, listing, revocation.
- Starting a session: host → project (recents, plus browsing under configured
  workspace roots) → agent → model / mode / effort.
- Live transcript: markdown, tool calls with input and output, thinking,
  plan/steps.
- Permission requests and elicitation (multiple-choice forms, e.g. the agent's
  "ask the user" tool), with **delivery acknowledgement** (§6.8).
- Cancel, resume, park, close; recovery after a host reconnect.
- Image attachments in prompts; slash commands.
- Session list sorted by time with day headers; search across all sessions.
- Mobile-first PWA with **Web Push** — "the agent asks, I answer from my
  phone" is a core loop, not a nicety.
- Hats (§8) as an isolation boundary.
- The MCP gateway module (§10).

Later (the design must not preclude them — see §15): observed sessions,
memory, Changes tab, worktree per session, workflows, config explorer, LLM
auto-naming.

Never part of roost: the predecessor's chat-app orchestrator, task hub and
similar personal automation.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **Collector** | The central process. Canonical state, REST + SSE API, static UI, WebSocket endpoint for hosts, MCP gateway module. |
| **Host** | A process on a developer machine. Dials out to the collector, spawns ACP adapters, owns nothing canonical. |
| **Adapter** | An ACP agent subprocess (`claude-agent-acp`, `codex-acp`, …) speaking JSON-RPC over stdio. |
| **Session** | One agent conversation. Belongs to exactly one host and exactly one hat. |
| **Hat** | An isolation boundary (a client, a secret project, …). §8. |
| **Control frame** | A roost-defined message between host and collector. |
| **ACP payload** | An ACP message carried verbatim inside a roost envelope. |
| **Connection** | An upstream MCP server configured in the gateway, with its credential. |
| **Mount** | A grant that lets a principal (host × hat, or a standalone client) use a connection. |
| **Principal** | Whoever presents an MCP client token to the gateway. |

---

## 3. Topology and processes

### 3.1 Hub-and-spoke (chosen)

```
 browser / PWA ──HTTPS (REST + SSE)──┐
                                     ▼
                              ┌──────────────┐    upstream MCP servers
                              │  collector   │◄──►(OAuth / PAT, HTTPS)
                              │  SQLite      │
                              │  gateway mod │◄── agents' MCP clients
                              └──────▲───────┘    (/mcp/<token>/<slug>)
                                     │ WebSocket (host dials out)
                 ┌───────────────────┼───────────────────┐
             ┌───┴───┐           ┌───┴───┐           ┌───┴───┐
             │ host  │           │ host  │           │ host  │
             │adapters│          │adapters│          │adapters│
             └───────┘           └───────┘           └───────┘
```

- **Collector** holds canonical state in SQLite: session timelines, hosts,
  hats and path rules, gateway connections and grants, push subscriptions,
  accounts. It serves REST + SSE and the embedded static UI, accepts hosts over
  WebSocket, and contains the MCP gateway as a module with **no dependency on
  session code** (§9).
- **Host** dials *out* to the collector over WebSocket, so it works behind NAT
  and needs no inbound port. It spawns adapters as subprocesses, implements the
  client side of ACP (terminal, filesystem, permission and elicitation
  callbacks), enumerates projects under workspace roots, and renders MCP
  entries into agent configs.

### 3.2 Rejected topologies

- **Host-centric federation** (each host is its own server; the UI aggregates).
  Push notifications, the gateway, the unified session list and search still
  need a centre. You end up building hub-and-spoke anyway, with state scattered
  across machines and no single thing to back up.
- **Browser talks directly to each host.** Breaks behind NAT, needs TLS and auth
  on every host, has nowhere to send push from while the tab is closed, and
  leaves the gateway without a home.

### 3.3 All-in-one: one binary, two processes

`roost up` runs the collector and a host as **two child processes of a small
supervisor**, talking over a local WebSocket with exactly the same frames as a
remote host.

Why two processes: adapters live exactly as long as the host process that
spawned them. If collector and host shared a process, every collector upgrade
or crash would kill every running agent. With two processes, the collector can
restart freely; the host keeps running, buffers frames in its outbox (§5.6) and
reconnects.

Why the same frames, never in-process function calls: otherwise the single-
machine path becomes the only tested one and the multi-host path rots. One
code path, exercised by every installation.

Honest limit: **upgrading the binary eventually restarts the host too**, and a
host restart parks every session (§6.6). Sessions are resumable, not
uninterrupted. After an upgrade the supervisor can restart the collector child
on its own, and restarts the host child only when no session has a turn in
flight (or when the operator forces it); the mechanics belong to the
distribution spec.

The all-in-one host is paired automatically over the local channel (§7.6) —
the operator never sees an enrollment code for it.

---

## 4. Data model sketch

Indicative, not a schema. Every table has `owner_id`.

| Entity | Key fields | Notes |
|---|---|---|
| `owner` | id, created_at | Exactly one row in v1. |
| `password_credential` / `passkey` | owner_id, … | argon2 hash; WebAuthn credentials. |
| `host` | id, name, public_key, default_hat_id, last_seen, versions, capabilities, revoked_at | Per-host credential (§7.6). |
| `hat` | id, name, theme (colour, logo), push_policy | A default hat exists from setup (§8.2). |
| `hat_path_rule` | host_id, path_prefix, hat_id | Segment-matched, longest prefix wins. |
| `session` | id, host_id, hat_id, source_kind, agent, cwd, title, lifecycle, activity, created_at, last_event_at | `source_kind` = `acp` in v1; reserves room for `observed`. |
| `event` | event_id (collector-global, monotonic), session_id, host_seq, kind, indexed fields, payload (opaque JSON), ts | Unique on `(session_id, host_seq)`. |
| `pending_request` | session_id, request_id, kind (permission / elicitation), payload, state | State: open / delivered / dropped / cancelled. |
| `project_recent` | host_id, hat_id, path, last_used | Filtered by hat. |
| `gw_connection` | slug, label, url, hat_id, credential_kind, tool_allowlist | Belongs to exactly one hat. |
| `gw_credential` | connection_id, ciphertext, expires_at | Encrypted at rest (§10.1). |
| `gw_client` | id, token_hash, principal (host×hat or standalone client) | Token per (host, hat). |
| `gw_mount` | hat_id, host_id, connection_id | The hat × host grid. |
| `push_subscription` | endpoint, keys, device label | |
| `setting` | key, value | Includes `public_url`. |

---

## 5. Host ↔ collector protocol

### 5.1 Transport and encoding

- **WebSocket, JSON frames.** Protobuf was rejected: ACP itself is JSON-RPC
  described by a JSON Schema, so a binary encoding would add a second
  serialization world and a translation layer for no measurable gain at these
  message rates. JSON is also debuggable with `websocat`.

### 5.2 Two layers: control frames and verbatim ACP

1. **Control frames** are roost's own, with roost's own schema: `hello`,
   `hello_ack`, `start_session`, `session_started`, `prompt`, `cancel`,
   `close_session`, `resume_session`, `permission_response`,
   `elicitation_response`, `answer_delivered` / `answer_dropped`,
   `list_projects`, `browse_directory`, `apply_mcp_mounts`, `ack`, `error`, and
   so on. The full catalogue lives in the ACP-core spec.
2. **ACP payloads** (`session/update`, `session/request_permission`,
   `elicitation/create`, …) travel **verbatim** inside a `session_event`
   envelope.

ACP *requests* from the adapter to the host (permission, elicitation) are
forwarded the same way, tagged with a host-assigned `request_id`; the
operator's answer comes back as a control frame (`permission_response`,
`elicitation_response`) carrying that id, and the host completes the pending
ACP call.

The envelope carries only what the collector needs to store, index and route:
`session_id`, `seq`, `kind`, and a small set of **indexed fields** the host
extracts (e.g. activity change, title change, "a permission is pending"). The
collector never parses the ACP payload itself.

Only two components understand ACP shapes: the **host**, which runs ACP, and the
**frontend**, which renders it using the official ACP SDK types.

*Rejected:* a collector that interprets ACP updates. In the predecessor the
collector decoded ACP updates into its own event types; every adapter bump risked
a middle layer silently dropping something new. A whole update kind (the
agent's plan/step list) was dropped for months before anyone noticed, because
nothing failed — it simply never arrived.

### 5.3 Hello and versioning

The first frame from a host is `hello`:

```json
{
  "type": "hello",
  "protocol_version": "1.0",
  "host_version": "0.3.1",
  "host_id": "…",
  "proof": "…",
  "capabilities": ["runtimes", "projects", "mcp_mounts", "images"],
  "adapters": [{"id": "claude", "version": "…"}, {"id": "codex", "version": "…"}],
  "attached_sessions": [{"session_id": "…", "last_seq": 1234}],
  "outbox_from_seq": {"<session_id>": 1201}
}
```

- `protocol_version` is `MAJOR.MINOR`. Minor versions are additive only.
- The collector accepts hosts whose protocol **major** is its own (M) or the
  previous one (M−1). Anything else is **rejected loudly**: a `hello_error`
  frame naming both versions, a log line, and a visible "incompatible host"
  state in the Hosts view. Never a silent half-working connection.
- **Unknown frame types** are logged with a warning (rate-limited) and ignored.
  They are never silently lost and never crash the connection.
- `capabilities` gate features: the collector does not send a frame kind the
  host did not advertise, and the UI hides features no connected host supports.

### 5.4 One source of truth for message types

Every control frame and REST payload is defined once, as Rust types (tagged
`serde` enums for frame kinds). From them are generated: a JSON Schema
(`schemars`), the frontend's TypeScript types (`ts-rs` or `specta`), and
round-trip contract tests. The generated artefacts are checked in, and CI fails
if regenerating them produces a diff.

Dispatch is **exhaustive by construction**: frames are an enum and handlers a
`match` without a wildcard arm, so adding a frame type without a handler is a
compile error, not a test failure.

*Rejected:* schema-first (JSON Schema or TypeSpec as the source, Rust generated
from it). Generators for Rust handle tagged unions poorly, which is exactly the
shape most of these types have; making Rust the source keeps the unions native
and still gives every other consumer a schema.

*Rejected:* hand-written types per layer. In the predecessor one message shape
was hand-mirrored across four layers, and a result frame was missing from the
collector's dispatch while every test stayed green — the tests passed because
they got their expected error from a timeout, not from the missing handler.

### 5.5 Sequence numbers, acks, idempotent ingest

- The host stamps every session-scoped frame with a **per-session monotonic
  `seq`**, assigned when the frame enters the outbox.
- Collector ingest is **idempotent on `(session_id, seq)`**: a duplicate is
  acknowledged and discarded.
- The collector acknowledges with `ack {session_id, ack_seq}` (cumulative) after
  the event is committed to SQLite. The host drops acknowledged frames from its
  outbox.
- `seq` exists only for host → collector delivery. It is **not** the browser's
  cursor; the collector assigns its own global `event_id` on ingest (§11.2).

### 5.6 Outbox on disk

The host writes every outgoing session frame to a small on-disk outbox (append
log or embedded SQLite) before sending. A host crash or upgrade restart
therefore leaves no gap in the transcript: on reconnect, `hello` announces what
is unacknowledged and it is resent.

The outbox is bounded (default 64 MiB, configurable). On overflow the host
drops the oldest unacknowledged frames of the affected session and emits a
`transcript_gap {session_id, from_seq, to_seq}` frame, which the UI renders
visibly. Loss is allowed only if it is visible.

### 5.7 Request / response control frames

Control requests (`start_session`, `list_projects`, `browse_directory`,
`apply_mcp_mounts`, …) carry a `request_id`; the response echoes it. The
collector applies a per-kind timeout (default 15 s) and returns a readable error
to the UI on expiry. A response for an unknown or expired `request_id` is
logged and dropped.

### 5.8 Liveness

Both sides send WebSocket pings (default every 15 s) and enforce read deadlines
(default 45 s). A connection that misses its deadline is closed and the host
reconnects with exponential backoff and jitter.

*Rejected:* relying on TCP to notice a dead peer. In the predecessor the lack of
keepalives meant a laptop going to sleep left a half-open connection for
minutes; the host reconnected with fresh state while the collector still routed
to the dead connection, and every session on that machine was lost at once.

### 5.9 Host identity on the wire

The host authenticates each connection with its per-host credential (§7.6)
inside `hello` (`host_id` plus a proof of possession of its private key over a
collector-supplied nonce). A revoked host is rejected with a distinct error so
`roost doctor` can say "this host was revoked" rather than "connection failed".

---

## 6. Sessions

### 6.1 Who is canonical for what

| State | Canonical owner |
|---|---|
| Everything displayed: timeline, status, title, hat, pending requests | Collector |
| The agent's own conversation memory | The adapter (via its CLI's session store) |
| Attached sessions, in-flight requests, outbox | Host (operational only) |

`session/load` exists only to **re-attach the agent** to its memory. It is never
used to rebuild the timeline; the collector already has it.

### 6.2 Replay suppression lives in the host

When an adapter loads a session it replays the whole conversation as
`session/update` notifications. The host knows it initiated the load, so the
**host** suppresses those notifications until the load call returns; they never
enter the outbox.

*Rejected:* the collector detecting and discarding replays. In the predecessor
the collector had to guess; until it did, every resume duplicated the entire
transcript and appended a spurious "session started" event, and it took a
dedicated fix plus a database cleanup across dozens of sessions.

### 6.3 Status: two axes

**Lifecycle:** `starting → active → parked → closed`, plus `failed`.

- `starting` — adapter spawning / `session/new` in flight.
- `active` — adapter attached on a connected host.
- `parked` — no adapter the collector can reach; resumable. Reached by idle
  reap, host restart or explicit park (adapter known to be released), or by the
  host being offline longer than the threshold (adapter *presumed* gone). The
  presumed case is reconciled on reconnect: if the host still holds the
  adapter, the session returns to `active` and its pending requests stay open
  (§6.6).
- `closed` — **only by explicit operator action.** Nothing closes a session
  automatically. Closed is resumable too; it means "I am done with this".
- `failed` — could not start or re-attach; the reason is stored and shown. The
  timeline stays readable.

**Activity** (meaningful only when `active`): `running | idle | blocked`.

- `blocked` = at least one pending permission or elicitation. This is what
  triggers Web Push and the blocked marker in lists.

*Rejected:* one flat status enum. The predecessor mixed "what the adapter is
doing" with "whether the adapter exists" in one field, which produced states
like "running but not live" that no view could display honestly.

### 6.4 Session source kind

`session.source_kind` is `acp` in v1. Observed sessions (terminal sessions
captured via hooks and transcript tailing) are a later **second source kind**:
same session table, same timeline, same hats; a different producer that has no
adapter and therefore no prompt/permission capability. Anything that assumes
"every session can be prompted" must check the source kind's capabilities, not
the kind name.

### 6.5 Pending requests

- A permission or elicitation request has **no timeout**. A question asked at
  night waits until morning.
- It is cancelled only when the adapter is lost (host restart, adapter crash),
  and the cancellation is shown explicitly on the card ("the agent is no longer
  waiting — resume to continue").
- The idle reaper **never** parks a `blocked` session: a pending request is by
  definition mid-turn.

### 6.6 Failure scenarios

| Event | Behaviour |
|---|---|
| WebSocket drops, host process survives | Host reconnects; `hello` lists attached sessions and unacked seqs; outbox is resent; collector reconciles. Sessions stay `active`, pending requests stay open. |
| Collector restarts | Same as above from the host's side. Nothing is parked. |
| Host restarts (crash, upgrade, reboot) | Adapters die with it. On reconnect, sessions that had a turn in flight get a `turn_interrupted` event; all previously attached sessions become `parked`. Pending requests become `cancelled` with a visible reason. Resume = `session/load`. |
| Host offline longer than the offline threshold (default 10 min, configurable) | Collector marks its sessions `parked` with a visible "host offline" note (presumed, not reported). On reconnect, `hello.attached_sessions` is authoritative: sessions whose adapter is still attached go `parked → active` without a resume, pending requests intact; the rest stay `parked`. |
| Idle (default 30 min, configurable, never mid-turn, never `blocked`) | Host's reaper releases the adapter; session becomes `parked`. |
| `session/load` fails | Session becomes `failed` with a readable reason (e.g. the agent CLI has no record of that session). |
| Adapter crashes mid-turn | `turn_interrupted` + `parked`; stderr tail attached to the event for diagnosis. |

### 6.7 Resume and park

- **Resume** of a `parked`, `closed` or `failed` session: host spawns the
  adapter, calls `session/load` with replay suppression, re-applies the stored
  model then mode (model first: switching model can clamp the available modes),
  and reports the resulting configuration. The collector stores what the host
  reports after the switch, never the pre-switch catalogue.
- **Park** is available explicitly; it is what the reaper does.

### 6.8 Delivery acknowledgement for answers

An answer to a permission or elicitation travels collector → host → adapter.
The HTTP 202 to the browser proves only that the collector accepted it. The
host knows whether an adapter callback was actually waiting for it, and reports
`answer_delivered` or `answer_dropped`. Cards show "answered" only after
`answer_delivered`.

When several clients answer the same request (phone and desktop), the first
delivered answer wins and a later drop must not overwrite it: the fold over
acknowledgements is monotonic (`delivered` sticks).

### 6.9 One adapter process per session

The host spawns a separate adapter process for every session (decided
2026-09-26).

- A crash, hang or memory leak affects one session, never its neighbours.
- Per-hat isolation needs no extra machinery: Codex's isolation is per process
  (a roost-owned composed `CODEX_HOME`), Claude's is per session (a strict-MCP
  flag), and both fit a process-per-session model directly (see
  [the spike](../spikes/2026-09-25-per-session-mcp.md)).
- The idle reaper (§6.6) is what bounds memory: an idle session's process is
  released and the session parks.

*Rejected:* one adapter process per hat (or per agent) serving many sessions.
It saves memory but a single fault takes every session in the group down, and
it would need per-session isolation that Codex does not offer inside one
process.

---

## 7. Authentication and pairing

### 7.1 Why built in

Delegating everything to an external identity provider was rejected: a
single-machine install must work without one. Built-in auth plus an optional
trusted-proxy mode covers both ends.

### 7.2 Bootstrap

On first start with an empty database the collector prints a **one-time setup
link** (random token, expires, single use) to create the owner account. There is
no default password and no "first visitor becomes admin" window.

### 7.3 Operator login

- Password (argon2id) and **passkeys** (WebAuthn), passkeys as the primary path.
- Session cookie: `HttpOnly`, `Secure` (except on `localhost`),
  `SameSite=Strict`.
- **Trusted-proxy mode** (optional): the collector accepts an identity header
  (e.g. from Cloudflare Access, oauth2-proxy, Tailscale identity headers)
  **only** when the request's peer address matches an explicitly configured
  proxy address. From any other peer the header is rejected (and logged), never
  silently ignored and never honoured.
- OIDC is deferred.

### 7.4 Request authentication

Every API call is authenticated — there is no "open on the LAN" mode. SSE and
WebSocket endpoints check `Origin`. Every row carries `owner_id` and every query
filters by it.

*Rejected:* an unauthenticated LAN mode. The predecessor left the dashboard API
open on the local network; fine for one person's network, indefensible for a
product others install.

### 7.5 `public_url` and TLS

- `public_url` is a **required** setting: MCP OAuth callbacks and push
  notification links are built from it.
- Web Push on iOS works only from an installed PWA over HTTPS, and push is core,
  so **v1 requires TLS** for anything but `localhost`.
- Supported topologies, all documented:
  1. `localhost` — a secure context; desktop push works.
  2. Tailscale — `tailscale serve` provides HTTPS.
  3. Reverse proxy (Caddy, nginx) terminating TLS.
- No built-in ACME in v1.

### 7.6 Host pairing

1. Operator clicks **Add host**; collector creates a one-time code (short, single
   use, default TTL 10 min).
2. On the machine: `roost host join https://collector.example CODE`.
3. The host generates a keypair, sends the public key with the code; the
   collector registers a **per-host credential** and returns the host id.
4. Hosts are listable, renameable and **individually revocable**.

The all-in-one host is paired automatically over the local channel.

*Rejected:* one shared fleet token. In the predecessor a single token
authenticated every machine: impossible to revoke one laptop, and anyone holding
it could impersonate any host.

---

## 8. Hats — an isolation boundary

### 8.1 What a hat is

A hat is anything whose data must not leak out of it: a client, a single secret
project, a side project. It is **an isolation boundary, not an organisational
filter**. Declared in the collector: `{id, name, theme (colour, logo)}`.

*Rejected:* hats as a UI filter only (the predecessor's model). A filter hides
rows from the operator; it does nothing about a session in client A being handed
client B's integrations, which is the actual risk.

### 8.2 Resolving a session's hat

- Every host has a **default hat**, so no session is ever hat-less. Setup
  creates one hat (named by the operator, e.g. "Personal") and assigns the
  all-in-one host to it.
- Mixed hosts use **path rules**: `path prefix → hat`, per host.
  - Matched by **path segment**, not substring: `~/Projects/acme-infra` must not
    match a rule for `~/Projects/acme`.
  - **Longest prefix wins**: `~/Projects/acme/secret/**` beats
    `~/Projects/acme/**`.
  - Paths are normalised (home expansion, trailing slashes, `..` removed)
    before matching. Symlinks: open question for the ACP-core spec.
- The hat is **stored on the session at start**. Changing rules later does not
  re-bucket history. Re-assigning a session is allowed but explicit and logged
  as a timeline event.
- No "shared" hat in v1.

### 8.3 What isolation covers

Isolation applies to what sessions, agents and data flows can reach — **not** to
the operator's view. The single operator sees all hats; the "All" view stays.

- A session in hat A must not be able to use hat B's MCP connections (and, in
  v2, hat B's memory or context).
- A gateway connection belongs to **exactly one hat**. The same vendor in two
  hats is two connections with **separate OAuth grants**.
- MCP client tokens are per **(host, hat)**. The gateway derives scope from the
  token, never from anything the client claims.

Side channels that must respect the boundary:

- Project picker recents are filtered by hat.
- Push notification content is minimal by default (session name, no transcript
  excerpt), configurable per hat.
- v2 LLM features configure their LLM endpoint per hat.

Hats also drive UI filtering, theming and per-hat push muting.

### 8.4 Threat model and limits

Hats prevent **accidental** cross-hat exposure through roost's own channels:
which MCP connections an agent is offered, which recents and context it sees,
what a push notification reveals.

Hats do **not** sandbox agents. Every adapter on a host runs as the same OS user
as the host and has shell access. A misbehaving or prompt-injected agent in hat
A can read anything that user can read — including the host's data directory
(other hats' mount tokens, the outbox with other hats' transcripts, the host
credential) and other projects on disk. No config-injection mechanism (§8.5,
open question 1) changes that.

A hat that needs strong isolation belongs on **its own host**: a separate OS
user, container or machine, paired as a separate host whose default hat is that
hat. The documentation says this plainly, next to the hat settings.

### 8.5 Known dependency: mixed hosts and agent MCP config

Per-(host, hat) tokens make the gateway's own checks correct. But agents read
MCP servers from their own config, which is **one file per user** for both
Claude Code and Codex. If a mixed host's global config held URLs and tokens for
both hats, a hat-A session would be offered hat B's connections as ordinary
tools. Keeping roost's own channels hat-clean on a mixed host (within the limits
of §8.4) therefore needs a per-project or per-session injection path, which is
the subject of the spike in [Open questions](#16-open-questions).

**v1 fallback if no per-project/per-session path works for an agent:** on a
mixed host, that agent's config receives only the mounts of the host's
**default hat**; sessions in other hats on that host run without gateway MCP
servers, and the UI says so on the mount grid and at session start. Isolation is
never silently weakened to make a feature work.

---

## 9. Module boundaries

Three modules plus a shared kernel. Dependencies point one way.

```
        ┌──────────────┐     ┌──────────────┐
        │   ACP core   │     │ MCP gateway  │
        │ hosts,       │     │ connections, │
        │ sessions,    │     │ OAuth, proxy,│
        │ push         │     │ manifests    │
        └──────┬───────┘     └──────┬───────┘
               │   implements        │ defines
               │   ClientIdentity,   │ ClientIdentity,
               │   MountPolicy,      │ MountPolicy,
               │   Notifier  ───────►│ Notifier
               ▼                     ▼
        ┌─────────────────────────────────────┐
        │ kernel: auth, hats, storage, config, │
        │ message types, HTTP server           │
        └─────────────────────────────────────┘
        distribution: CLI, supervisor, runtime manager, renderers
```

- **ACP core** owns hosts, sessions, the host protocol, push, the session UI.
- **MCP gateway** owns connections, credentials, OAuth, the proxy and manifests.
  It imports nothing from ACP core. It needs exactly three interfaces, which it
  defines and others implement (§10.2).
- **Kernel** owns operator auth, hats and path rules, storage, configuration,
  generated types and the HTTP server.
- **Distribution** owns the CLI, the supervisor, the managed runtime, service
  installation and the config renderers.

The test that the boundary holds: `roost gateway` (standalone) builds and runs
with the ACP core absent from its wiring. In Rust this is a Cargo workspace
where the gateway crate does not depend on the ACP-core crate.

### 9.1 Implementation language: Rust

Decided 2026-09-26. The reasons are specific to roost, not general:

- **Both protocols have official Rust SDKs**: `agent-client-protocol` for ACP
  and `rmcp` for MCP.
- **Most domain types are tagged unions** — frames, credential kinds, the two
  status axes. Native enums plus exhaustive `match` turn the predecessor's
  worst class of bug (a frame with no handler, tests still green) into a
  compile error (§5.4).
- The rest is standard ground: `tokio` for subprocesses and I/O, `axum` for
  HTTP/SSE and the streaming proxy, a WebSocket crate, SQLite
  (`rusqlite` or `sqlx`), `webauthn-rs`, `argon2`, and embedded static assets.
  Static binaries for Linux (musl) and macOS arm64 are routine.

Costs accepted: slower compiles and a steeper start with async Rust
(cancellation, `Send` bounds, streaming lifetimes).

*Rejected:* Go. It fits the I/O-glue shape well and the predecessor is written
in it, but roost is a rewrite, not a port, and Go needs codegen plus a linter
to approximate the exhaustiveness Rust gives for free.

Crate choices above are indicative; subsystem specs and plans pin them.

---

## 10. MCP gateway

Refined in its own spec. This section fixes the boundary and the invariants.

**Operator experience: one place.** The operator adds every MCP integration
once, in the MCP view, and for each one ticks which hosts receive it. That is
the whole mental model. Everything below — the connection's hat, per-(host,
hat) client tokens, manifests, renderers — is machinery that turns those ticks
into agent config; the operator never handles tokens or config files by hand.

### 10.1 What it owns

- **Connections:** `slug`, label, upstream URL, hat, credential (PAT or OAuth),
  tool allowlist.
  - Static tokens (PATs, API keys) are preferred where a vendor offers them:
    no refresh, no expiry probe, fewer moving parts.
- **OAuth per the MCP authorization spec:** protected-resource metadata
  discovery, dynamic client registration, PKCE as a public client, single-flight
  refresh per connection, liveness probe via a real `initialize` handshake.
- **Proxy** at `/mcp/<client-token>/<slug>`:
  - One upstream session per (connection × client).
  - Streaming responses (SSE) are forwarded incrementally: the first chunk must
    reach the client before the upstream finishes writing. A buffering proxy
    works on every short response and fails on the first long one.
  - An upstream `401` becomes `502 upstream_auth`. A `401` is never passed
    through, and `WWW-Authenticate` is stripped; otherwise the agent's MCP
    client starts its own per-machine OAuth flow, which is exactly what the
    gateway exists to prevent.
  - `sampling` and `elicitation` are not forwarded in v1 (the gateway does not
    advertise them upstream).
  - Unknown token or unmounted connection → `404`, not `403` (don't confirm
    existence).
- **Credentials encrypted at rest** with a master key generated on first run
  (0600 file in the data directory, or supplied via an environment variable).

The documentation states plainly: **the gateway is a single point of compromise
for every integration it holds.** Hats limit what one host's token can reach;
they do not protect against compromise of the collector itself.

### 10.2 Interfaces it needs from the rest

| Interface | Full product | Standalone `roost gateway` |
|---|---|---|
| `ClientIdentity` (token → principal) | "host X, hat H" | a manually created client |
| `MountPolicy` (principal → connections) | the hat × host mount grid | a list pinned to the client |
| `Notifier` (credential expired / failing) | Web Push + UI badge | log line and optional webhook |

### 10.3 Manifests and renderers

The gateway does **not** write agent config files. It emits a **manifest** per
principal: `[{slug, url, transport}]`.

**Renderers** turn a manifest into agent config:

- host-side, automatically, when mounts change (`apply_mcp_mounts`);
- CLI, `roost mcp apply --client claude|codex`, in standalone mode.

Renderer rules:

- Write `~/.claude.json` / `~/.codex/config.toml` entries roost owns; **never
  touch entries it does not own** (ownership marked in a way that survives the
  agent rewriting the file).
- A semantic no-op leaves the file byte-identical.
- The token travels in the URL path, so the entry is just a URL — no environment
  variable has to be exported into future shells.

v1 renderers: **Claude Code and Codex**. Other agents get a generic "copy this
JSON snippet" export.

Mount changes take effect in the agent's **next** session, because agents read
MCP config at session start. The UI says so. `notifications/tools/list_changed`
is deferred.

### 10.4 Standalone mode

`roost gateway` = gateway + operator auth + a small UI for connections and
clients. No hosts, no ACP, no sessions. Same binary, same frontend with views
gated by collector capabilities.

---

## 11. Collector ↔ browser API

### 11.1 REST

Resource-oriented JSON over HTTPS, types generated from the Rust message types (§5.4).
Mutations that reach a host (prompt, cancel, answer, start) return `202` with an
operation id when the host round trip is asynchronous; the outcome arrives over
SSE.

### 11.2 SSE and resumption

- The collector assigns every ingested event a **global monotonic `event_id`**.
  SSE uses it as the event `id:`, and clients reconnect with
  `Last-Event-ID: <event_id>` for idempotent catch-up.
- Two stream shapes: a **list stream** (session metadata changes across all
  sessions, used by the session list) and a **session stream** (full events for
  one session). Both resume from `event_id`.
- If the requested `event_id` is older than retained catch-up history, the
  server tells the client to refetch the snapshot instead of silently skipping.

*Rejected:* using the host's per-session `seq` as the SSE cursor. It is only
unique within one session, so a multi-session stream cannot resume from it.

---

## 12. Distribution

Refined in its own spec.

### 12.1 One binary, subcommands

| Command | Purpose |
|---|---|
| `roost up` | All-in-one: supervisor + collector + host. |
| `roost collector` | Collector only. |
| `roost host join <url> <code>` | Pair this machine. |
| `roost host run` | Run a paired host. |
| `roost host adapters update` | Move to the adapter set pinned by the installed release. |
| `roost gateway` | Standalone MCP gateway. |
| `roost mcp apply --client <agent>` | Render a gateway manifest into an agent config. |
| `roost service install` | Write a launchd agent (macOS) or systemd user unit (Linux; warns about `loginctl enable-linger`). |
| `roost doctor` | Diagnose: agent CLIs present and logged in, adapters start, collector reachable, credentials valid. |

Platforms v1: Linux amd64/arm64, macOS arm64. Windows via WSL only.

### 12.2 Channels

GitHub Releases with signed checksums; `curl | sh` installer; Homebrew tap;
Docker image for the **collector only** (the host must run on the developer
machine, next to the repositories and the logged-in agent CLIs); Nix flake.

No self-update: package managers and images handle updates.

### 12.3 Managed Node runtime and pinned adapters

On `host join` (and `adapters update`) the host downloads a **pinned Node
runtime and pinned adapter versions** into its own data directory, verifying
checksums.

*Rejected:* requiring a system Node. Agent CLIs increasingly ship as native
binaries, so Node may not be installed at all, and version-manager PATH setups
(nvm, fnm, …) are frequently invisible to a service process.

- A roost release **is** a tested adapter set. The pin also decides what model
  aliases such as `opus` resolve to, so it is a product decision, not a detail.
- Every pin bump passes a **live e2e gate** in CI (§14).
- Advanced override: a custom adapter command in config — also the door for any
  other ACP agent.
- roost does not install agent CLIs or perform their logins. `roost doctor`
  checks them; "Authentication required" from an adapter always means the CLI on
  that machine is not logged in, and doctor says so in those words.

### 12.4 Backups

Backup = a copy of the collector data directory: SQLite database, the gateway
master key, and the Web Push (VAPID) key pair generated on first run. Without
the master key, encrypted gateway credentials are unrecoverable; without the
VAPID keys, every push subscription must be re-created. Both facts are
documented next to the backup instructions.

---

## 13. Frontend

- React + TypeScript + Vite, built to static files **embedded in the binary**
  (works offline, no CDN).
- Control-frame and REST types generated from the Rust message types (§5.4); session event
  payloads typed with the official ACP SDK.
- Realtime via SSE resuming from `event_id` (§11.2).
- **Views (v1):**
  - **Sessions** — time-sorted, day headers, search across all sessions,
    parked/closed filters, `blocked` marker.
  - **Session** — transcript, composer (text, images, slash commands),
    permission and elicitation cards, model/mode/effort bar, resume / cancel /
    close.
  - **New session** — host, project (recents + browse), agent, model/mode/effort.
  - **Hosts** — pair, rename, revoke, versions, doctor results, online/offline.
  - **MCP** — the single place to add integrations: connections, OAuth
    Connect, and per connection the hosts that receive it (backed by the
    hat × host mount grid; clients instead of hosts in standalone mode).
  - **Hats** — declare hats, path rules per host.
  - **Settings** — account, passkeys, push devices, `public_url`.
- `gateway` mode shows only MCP and Settings: one app, views gated by collector
  capabilities.
- Mobile-first PWA; Web Push on `blocked` and on turn end.
- Markdown pipeline order is load-bearing: parse raw HTML → sanitize →
  highlight (`rehype-raw → rehype-sanitize → rehype-highlight`), with unknown
  code languages ignored rather than thrown. Highlighting output is trusted only
  because it runs after sanitization.
- Neutral, original visual identity; hats supply colour and logo.

---

## 14. Testing strategy

| Layer | What | Why |
|---|---|---|
| Protocol | Contract tests generated from the schema: every frame round-trips. Exhaustive dispatch is a compile-time property; CI fails on a diff in regenerated schema/TS types. | Hand-mirrored shapes drift silently (§5.4). |
| Transport parity | The same scenarios run over the in-memory pipe and a real WebSocket. | One code path must stay tested in both deployments (§3.3). |
| Session behaviour | Deterministic **fake ACP adapter**: scripted replies, permissions, elicitation, replay on `session/load`. Scenarios: WS drop, collector restart, host restart, outbox resend, seq dedup, outbox overflow → gap, reaper vs. blocked, answer delivered vs. dropped, multi-client answer. | Every failure row in §6.6 gets a test. |
| Live e2e gate | Real `claude-agent-acp` and `codex-acp` on every adapter pin bump: start, prompt, tool call, permission, elicitation, resume. | In the predecessor only a live call caught a wrongly shaped capability that the SDK silently discarded; unit tests asserted our JSON against our own assumption. |
| Gateway | Streaming test (first chunk arrives before upstream finishes; must fail, not hang, on a buffering proxy). Fake OAuth server: discovery, DCR, PKCE, refresh, single-flight, 401 → 502. Renderer tests on real Claude and Codex config files: foreign entries untouched, no-op is byte-identical. | Each is a known failure class. |
| Hats | **Negative tests**: a hat-A principal requesting a hat-B connection gets 404; hat-B recents never appear for hat A. Table-driven path-rule resolution (segment match, longest prefix, normalisation). | Isolation is only real if its violation is tested. |
| Auth | Setup link single use and expiry; trusted-proxy header rejected from a non-proxy peer; revoked host rejected distinctly; `Origin` enforcement on SSE/WS. | |
| Frontend | Component tests (vitest). **Playwright** on main flows: setup → pair host → start session → permission → push. | jsdom cannot verify layout, focus or real rendering. |
| Guards | **Revert probe**: every guard is shown to fail its test when removed, with both runs recorded in the PR. | A test nobody watched fail may pass for the wrong reason. |

External-binary tests (git, adapters) skip cleanly when the binary is absent so
hermetic package builds stay green.

---

## 15. Deferred / out of scope

| Item | Status | What the v1 design reserves |
|---|---|---|
| Observed (terminal) sessions | Later | `session.source_kind`; capability checks by source (§6.4). |
| Memory / cross-session context | v2 | Per-hat scoping and per-hat LLM endpoint (§8.3). |
| Changes tab (git diff since session start) | Later | Host capability flag; base commit recordable at start. |
| Worktree per session | Later | Session `cwd` is already per session. |
| Workflows / saved kickoffs | Later | — |
| Config explorer | Later | Host capability flag. |
| LLM auto-naming | Later | `title` is already host- or operator-set. |
| OIDC login | Later | Auth module seam next to passkeys. |
| Teams / multiple operators | Later | `owner_id` on every row. |
| Built-in ACME | Not v1 | Reverse proxy / Tailscale documented instead. |
| Windows native | Not v1 | WSL. |
| MCP `tools/list_changed`, sampling, elicitation forwarding | Not v1 | — |
| Self-update | Never | Package managers. |
| Chat-app orchestrator, task hub | Never | — |

---

## 16. Open questions

1. **Per-project / per-session MCP config — measured 2026-09-25, see
   [the spike](../spikes/2026-09-25-per-session-mcp.md).** Summary: both
   adapters accept per-session servers and also load global ones; Claude can be
   isolated per session via a `_meta` strict flag, Codex via a roost-owned,
   composed `CODEX_HOME` per adapter process that keeps the user's setup. Original question: on
   a mixed host, Claude Code and Codex each have one global MCP config per user.
   Per-hat isolation needs a narrower injection path. Candidates, to be
   **measured, not assumed**:
   - Claude Code: `projects[<path>].mcpServers` in `~/.claude.json`.
   - Codex: some form of project-scoped config — unverified.
   - **ACP `session/new` `mcpServers`.** A first look at the currently pinned
     `claude-agent-acp` shows it advertises `mcpCapabilities: {http, sse}` and
     maps HTTP/SSE entries (including headers) from `session/new` into the
     session. If both adapters honour this, roost-driven sessions could get
     their hat's mounts per session without touching global config at all, and
     renderers would matter only for sessions started outside roost. The spike
     must also check whether globally configured servers are still loaded
     alongside (which would reintroduce the leak) and what `codex-acp` does.
   The outcome decides whether the §8.5 fallback is needed for either agent.
   It does **not** settle isolation in general: agents sharing an OS user can
   still read each other's data on disk (§8.4).
2. **ACP Rust SDK coverage.** The host uses the Rust ACP crate while adapters
   and the frontend use the TypeScript SDK. Unstable protocol parts (e.g.
   elicitation) may land later in Rust or sit behind a feature flag. The host
   passes ACP payloads through verbatim, so it can hold unknown fields as raw
   JSON, but this must be checked against the pinned adapters at the start of
   implementation.
3. **Name collision.** "roost" is not yet checked against existing projects
   (e.g. ROOST, an open-source online-safety tools organisation), package
   registries, or the Homebrew namespace.
4. **Host proof of possession.** Signed-nonce in `hello` is the working
   assumption (§5.9); the exact scheme (and whether mTLS is simpler behind
   reverse proxies) is for the ACP-core spec.
5. **Symlinked project paths** in hat resolution: resolve before matching, or
   match the path as the operator typed it? Affects both correctness and what
   the operator expects.

---

## 17. Next steps

1. Operator review of this spec.
2. Spike: open question 1.
3. Subsystem specs: ACP core, MCP gateway, distribution.
4. Implementation plans per subsystem.


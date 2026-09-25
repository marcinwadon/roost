# roost — MCP gateway (subsystem spec)

- **Date:** 2026-09-26
- **Status:** Draft, awaiting review
- **Refines:** [architecture spec](2026-09-25-roost-architecture-design.md) §8
  (hats) and §10 (gateway).
- **Evidence:** [per-session MCP spike](../spikes/2026-09-25-per-session-mcp.md)
  and a behaviour catalogue of the predecessor's gateway, which ran against
  real vendors (Notion, Atlassian, Miro, Datadog, Figma, Slack). "G-n" marks a
  predecessor incident or gap (§12).

The gateway lets the operator add an MCP integration **once, in one place**,
tick which hosts receive it, and have every agent session on those hosts use
it without a per-machine login. It owns connections, credentials, OAuth, the
streaming proxy and the manifests that tell agents where to connect.

---

## 1. Operator model

1. **Add a connection** in the MCP view: a name, the upstream URL, and how it
   authenticates (token, OAuth, or none). Each connection belongs to one hat;
   the default is the hat currently selected in the UI.
2. **Connect** (OAuth only): a popup completes consent once, on the collector.
3. **Tick hosts.** The grid lists **every host** for every connection (a mixed
   host isolates per session, so no host is out of a hat's scope); a tick is a
   mount. Where a host falls back to default-hat mounts for an agent (umbrella
   §8.5), the grid says so.
4. **Next session** on a ticked host, in the connection's hat, has the
   integration's tools. The UI says "applies to new and resumed sessions".

The operator never sees client tokens, never edits agent config files, and
never repeats consent per machine.

---

## 2. Data model

```sql
gw_connections(
  id TEXT PK, owner_id, slug UNIQUE, label, url, hat_id,
  cred_kind,               -- none | static | oauth_dcr | oauth_client
  tool_allowlist JSON,     -- null = all tools
  internal_network BOOL,   -- operator allows non-public upstream addresses (§5.7)
  status,                  -- not_connected | ok | needs_auth | error
  status_note, account_label NULL, status_at, created_at, updated_at)
gw_credentials(
  connection_id PK, key_version, ciphertext BLOB,   -- AEAD, §6
  expires_at, updated_at)
gw_mounts(connection_id, host_id, PRIMARY KEY(connection_id, host_id))
gw_session_tokens(                  -- one per session, minted at every start/resume
  session_id PK, owner_id, host_id, hat_id, token_hash,
  created_at, last_used_at, revoked_at)
gw_clients(                         -- standalone clients only
  id TEXT PK, owner_id, hat_id, label,
  token_hash, created_at, last_used_at, revoked_at)
gw_client_pins(client_id, connection_id, PRIMARY KEY(client_id, connection_id))
gw_oauth_clients(                   -- registered or pre-registered OAuth clients
  connection_id PK, token_endpoint, authorization_endpoint, issuer,
  client_id, client_secret_ciphertext NULL, redirect_uri, scopes JSON,
  resource, resource_param_accepted BOOL, registered_at)
gw_stdio_servers(                   -- §3.4
  id TEXT PK, owner_id, host_id, hat_id, name, command, args JSON,
  env_ciphertext NULL, created_at, updated_at)
```

`gw_mounts` has no hat column: the hat is the connection's. `gw_session_tokens`
holds session ids as opaque values; the gateway never reads session tables.

- **`cred_kind`:**
  - `none` — no credential (public or network-trusted upstreams).
  - `static` — a personal access token or API key, sent as a bearer token or a
    configured header name. Preferred where the vendor offers one: no refresh,
    no expiry probe.
  - `oauth_dcr` — OAuth with dynamic client registration as a public client.
  - `oauth_client` — OAuth with a **pre-registered** client the operator
    enters (client id, optional secret). *(G-1: vendors without dynamic
    registration or with confidential-client-only token endpoints — Slack at
    the time of measurement — could not be connected at all.)*
- **The secret payload** (access/refresh token, static token, client secret)
  lives only in `gw_credentials` / `gw_oauth_clients` ciphertext. List
  endpoints never read those tables.
- A connection belongs to **exactly one hat**. The same vendor in two hats is
  two connections with separate grants (umbrella §8.3).
- **Slugs:** `^[a-z0-9][a-z0-9-]{0,47}$`, unique per installation. They become
  MCP server names (`roost-<slug>`) and URL segments.
- **Hat purge** (kernel §5.5): the gateway's purge hook deletes the hat's
  connections with their credentials, OAuth clients and mounts, its session
  tokens, standalone clients and stdio servers.

---

## 3. Principals and delivery

### 3.1 Who presents a token

| Principal | Created | Scope |
|---|---|---|
| session | automatically, by `SessionMcp::servers_for` at every start and resume of a session (ACP core §1) | connections of the session's hat mounted on the session's host |
| `standalone` client | manually in standalone mode (`roost gateway`) or for tools outside roost | the connections pinned to the client (`gw_client_pins`) |

- Tokens are 32 random bytes, stored only as SHA-256 hashes. A standalone
  token is shown once; a session token exists in plaintext only inside the
  `start_session` / `resume_session` frame for that session's host.
- **One token per session.** It is revoked on park, close, adapter exit and
  host revoke (`SessionMcp::revoke`), and superseded by the token minted at the
  next resume. A presumed park while the host is merely offline does not revoke
  it (ACP core §4.8).
- **Scope is checked at request time** from the token's (host, hat) and the
  mounts as they are now, never from anything the request claims. An unknown
  token, a revoked token, an unmounted connection, or a connection of another
  hat all return **404** (not 403: do not confirm existence).
- Tokens are never logged, and the `headers` of `mcp_servers` entries never
  appear in timeline events or SSE (ACP core §8).

### 3.2 Delivery to roost sessions (primary path)

Per the spike, roost-driven sessions get their MCP servers **per session over
ACP**, not through agent config files:

1. When the collector prepares `start_session` / `resume_session` (ACP core
   §3.3) it calls `SessionMcp::servers_for(host, hat, session)`, which mints the
   session token and returns every connection of the session's hat mounted on
   the session's host, as
   `{type: "http", name: "roost-<slug>", url: "<public_url>/mcp/<slug>",
   headers: [{name: "Authorization", value: "Bearer <session token>"}]}`,
   followed by the stdio servers for that (host, hat) (§3.4).
2. The host passes them in `session/new` / `session/load`, with the agent's
   isolation mechanism (Claude strict flag, Codex composed home; ACP core §6),
   or applies the fallback (umbrella §8.5).
3. A mount change affects the **next** start or resume of a session on that
   host; a removed mount is refused at request time immediately.

**Token in a header, not the URL.** *(G-2: the predecessor put the token in
the URL path because one agent's CLI could not take a header from its config
at the time. URLs end up in logs and agent transcripts. Both agents accept
headers for HTTP MCP servers in ACP `session/new`, and Codex's config supports
`http_headers`.)*

**Measured limit: the token is visible in the process list.** The Claude
adapter's SDK passes `mcpServers` to the Claude CLI as `--mcp-config <JSON>` on
the command line, headers included, so another local user of that machine can
read a live session's token from the process list. Per-session tokens limit
the blast radius to live sessions of that host and hat. **Hosts shared with
untrusted local OS users are unsupported for hats with gateway connections**,
and the documentation says so. A live gate (ACP core §12) searches the process
list for the token on every adapter pin; for Codex the exposure is
unmeasured.

### 3.3 Delivery outside roost sessions (renderers)

For standalone mode, or for terminal sessions the operator starts without
roost, the gateway emits a **manifest** per standalone client
(`[{name, url, headers}]`) and a renderer writes it into agent config:

- `roost mcp apply --client claude|codex`, with the client token read from a
  file (`--token-file`), the `ROOST_MCP_TOKEN` environment variable or stdin —
  never a command-line flag (it would show in the process list).

Hosts never render MCP entries into agent config in v1. Renderer rules (§8).

### 3.4 Local stdio servers

roost's per-session isolation strips the user's own locally configured MCP
servers from roost sessions, and the gateway proxies only HTTP. So the MCP view
also holds, per **(host, hat)**, a list of local stdio servers
(`name`, `command`, `args`, `env`; names follow the slug rules and share
the slug namespace). The collector stores them (`env` encrypted
like credentials, §6) and passes them to sessions of that hat on that host as
ACP stdio `mcpServers` entries, named `roost-<name>`. They run on the host as
the agent's child processes and are not proxied. Editing them requires step-up
authentication (kernel §3.4), since they are commands the host will execute.

---

## 4. OAuth

Built on `rmcp`'s `auth` module (discovery, registration, PKCE, refresh), with
roost supplying the credential store, the flow state, the HTTP client and the
policies below. Every discovery, registration and token request goes through
the egress policy (§5.7): no redirects are followed, non-public addresses are
refused unless the connection is marked "internal network", and every
authorization-server metadata and endpoint URL must be `https` (loopback
`http` allowed).

### 4.1 Discovery

For a connection URL, find the protected-resource (PR) document in order:

1. the `resource_metadata` URL from a `WWW-Authenticate` challenge (one
   unauthenticated `initialize` POST);
2. the path-inserted well-known URL
   (`<origin>/.well-known/oauth-protected-resource<path>`);
3. the origin-level well-known URL.

Then, for each authorization server named by the PR document (falling back to
the resource origin), fetch AS metadata trying the RFC 8414 inserted form
before the appended form, then OpenID configuration. The first document with
authorization and token endpoints wins.

*(G-3: a vendor served its PR document only at the path-inserted URL; trying
only the origin found a legacy authorization server whose tokens the resource
rejected — a grant that "refreshes fine and never works".)*

- The PR document's `resource` is validated against the connection URL
  (same origin, and the connection path starts with the resource path). A
  mismatch is shown to the operator with both values; the operator can accept
  it explicitly. *(G-4: one vendor's PR document names the bare origin while the
  documented endpoint has a path and query.)*
- `scopes_supported` from the PR document is requested at consent. *(G-5: one
  vendor issued tokens without product scopes unless they were requested.)*

### 4.2 Registration

- `oauth_dcr`: RFC 7591 registration as a public client
  (`token_endpoint_auth_method: none`, grant types `authorization_code` and
  `refresh_token`, redirect `<public_url>/api/mcp/oauth/callback`).
  - A vendor refusal shows the RFC 7591 `error` and `error_description`,
    truncated to 300 characters. *(G-6: a refusal of a non-HTTPS redirect
    surfaced only as "400 Bad Request".)*
  - No registration endpoint → the UI suggests switching to `oauth_client`.
- `oauth_client`: the operator enters client id and, if the vendor requires a
  confidential client, the secret. The UI shows the redirect URI to register at
  the vendor.
- **Re-register** (`oauth_dcr`) when there is no client, the redirect URI
  changed (`public_url` changed), or discovery now yields a different token
  endpoint. If a grant is live, the new client replaces the old one only after
  the new consent completes. *(G-7: overwriting the client first broke refresh
  of the existing grant, permanently if the consent was abandoned.)*
- The redirect URI must be HTTPS or loopback HTTP; `public_url` enforcement in
  the umbrella §7.5 guarantees this for supported topologies.

### 4.3 Consent and exchange

- PKCE S256 only; refuse servers that do not advertise S256.
- Consent URL carries `resource=<connection URL>` (RFC 8707) and the scopes,
  and must be `https`. If the authorization server rejects the `resource`
  parameter, the flow is retried once without it and
  `resource_param_accepted = false` is recorded on the connection (and used for
  exchange and refresh).
- Flow state: in memory, keyed by `state`, single use, 15-minute TTL. The
  callback rejects an unknown `state` before touching storage and uses only
  values snapshotted when the flow started. *(G-8: a concurrent second authorize
  could swap the client under a callback that re-read it from storage.)*
- **Flow binding:** starting a flow sets a `SameSite=Lax`, `HttpOnly`,
  `Secure` flow cookie bound to its `state`; the callback requires the cookie
  to match, so a consent completed in another browser cannot be attached to
  the operator's connection.
- The popup is opened blank inside the click handler with `opener` set to
  `null`, then navigated to the consent URL (frontend spec §8).
- The exchange sends `code_verifier` and `resource`. Token-endpoint error
  bodies are never logged or echoed (they can repeat the code).
- The grant is stored **under the connection's refresh lock** (§4.5).
- After connecting, the MCP view shows the account identity the vendor
  reports, if any (`account_label`), so the operator can confirm the right
  account was connected.
- Vendor error text shown to the operator is truncated to 300 characters and
  rendered as text.

### 4.4 Tokens

- `expires_at = now + expires_in − 60 s`; absent `expires_in` means unknown,
  not expired.
- **Proactive refresh:** a request that finds the access token within 5
  minutes of `expires_at` refreshes first (single-flight). Expiry otherwise
  costs a 401, a refresh and a retry. *(G-9: the predecessor stored expiry and
  never read it.)*
- A refresh response without `refresh_token` keeps the old one.
- Refresh sends `resource` and the original scopes. *(G-10: the predecessor
  omitted both on refresh.)*

### 4.5 Single-flight refresh

- One mutex per connection. Under it: re-read the stored credential; if the
  access token already differs from the one that failed, use the new one;
  otherwise refresh.
- A started refresh always runs to completion and persists, independent of the
  caller's cancellation, bounded by a 20 s timeout. *(G-11: abandoning a refresh
  after the vendor rotated the refresh token lost the grant.)*
- The caller's own cancellation never sets `needs_auth`. *(G-12: false
  "re-authorize" alerts.)*
- Outcomes are distinct: success; vendor refusal → `needs_auth`; local persist
  failure → 502 without `needs_auth` (a click cannot fix a disk error).
- Every credential write (grant storage, credential clear) takes the same lock.
  *(G-13: a late refresh of an old grant overwrote a freshly stored new one.)*

### 4.6 Credential invalidation on edit

- Changing a connection's URL **origin** or its `cred_kind` deletes its
  credential and OAuth client before the change is saved. *(G-14: otherwise an
  edited URL sends the stored token to a different host; a leftover static
  token would be sent as an OAuth access token.)*
- An omitted field in an update keeps its stored value; an explicit empty value
  clears it.

---

## 5. Proxy

Endpoint: `POST|GET|DELETE <public_url>/mcp/<slug>`, authenticated by
`Authorization: Bearer <client token>`.

### 5.1 Authorization

Resolve token → principal → the connection with that slug, which must be in
the principal's scope (§3.1). Otherwise 404. The request body is capped at
4 MiB (413 only for the size limit).

### 5.2 Forwarding

- Hand-written on `axum` + `reqwest` streaming. Not built on an MCP library:
  those parse and re-serialise, and the proxy must change nothing but auth
  (and, for `tools/list`, the allowlist).
- **Request headers forwarded:** `Content-Type`, `Accept` (default
  `application/json, text/event-stream`), `Mcp-Session-Id`,
  `Mcp-Protocol-Version`, `Last-Event-ID`. `Authorization` is replaced by the
  upstream credential (or removed for `none`). Everything else is dropped.
- **Response headers forwarded:** `Content-Type`, `Mcp-Session-Id`,
  `Cache-Control`. Everything else is dropped, including `Set-Cookie` and
  `WWW-Authenticate`. *(G-15: the predecessor passed `Set-Cookie` through.)*
  The gateway always adds `X-Content-Type-Options: nosniff`, and forwards only
  `application/json` and `text/event-stream` bodies; any other upstream content
  type becomes a 502.
- Upstream redirects are never followed (§5.7).
- **Sessions:** `Mcp-Session-Id` passes through in both directions, so each
  downstream client session maps to its own upstream session and the gateway
  holds no session table. `DELETE` is forwarded so client terminations reach
  the upstream. *(G-16: the predecessor answered DELETE with 405, leaving
  upstream sessions until the vendor expired them.)*
- `GET` (the optional server-to-client SSE channel) is forwarded and streamed.

### 5.3 Streaming

- Responses are streamed chunk by chunk with no buffering and **no compression
  layer on the proxy route** (compression middleware delays SSE).
- **Guarantee:** the first chunk of an upstream response reaches the client
  before the upstream finishes writing. A test asserts it against an upstream
  that writes one chunk and then blocks; the test must fail within seconds,
  not hang, if the proxy buffers.
- Exception: a `tools/list` response for a connection with an allowlist is
  read fully (cap 8 MiB, error if exceeded — never truncated) and rewritten.

### 5.4 Upstream 401

- Every response status other than 401 is committed and streamed immediately.
- A 401 is held: static or `none` credential, or OAuth without a refresh
  token → **502** `{"error": "upstream_auth", "message": "connection <label>
  needs re-authorization in roost"}`; OAuth → single-flight refresh and one
  retry, and a second 401 → `needs_auth` + 502 `upstream_auth`.
- **A 401 is never passed to the client and `WWW-Authenticate` is never
  forwarded.** *(G-17: agents answer an upstream 401 by starting their own
  per-machine OAuth against the gateway — exactly what the gateway exists to
  prevent.)*
- A failure before the response is committed is a clean 502 with no stray
  upstream headers.

### 5.5 Tool allowlist

- `tools/list` responses are filtered to the allowlist, in JSON and in SSE
  framing (per event, other events passed through byte for byte); an allowlist
  matching nothing yields `"tools": []`, never `null`. JSON-RPC batches are
  filtered element by element.
- **`tools/call` for a tool outside the allowlist is rejected** by the gateway
  with a JSON-RPC error (`-32602`, "tool not available through roost") and
  never reaches the upstream. *(G-18: in the predecessor the allowlist only hid
  tools; a direct call still executed.)*

### 5.6 Capabilities not forwarded in v1

The gateway rewrites `initialize.params.capabilities` sent upstream, removing
`sampling`, `elicitation` and `roots`. Server-to-client requests of those kinds
arriving in a response stream are answered by the gateway with a JSON-RPC
error. *(G-19: the predecessor's spec said these were not forwarded, but its
code passed the client's `initialize` through verbatim.)*

Other methods (including ones the gateway does not know, like
`server/discover`) are forwarded unchanged.

### 5.7 Egress policy and limits

The proxy, every OAuth HTTP client and Web Push delivery share one outbound
HTTP policy, which lives in the kernel (kernel §7.1) so that push delivery can
use it without depending on the gateway:

- **Redirects are never followed.**
- DNS is resolved by roost and the address checked before connecting;
  loopback, link-local (including `169.254.169.254`), RFC 1918, unique-local
  IPv6 and other non-public ranges are refused — unless the operator has marked
  the connection **"internal network"**, which allows them for that connection
  only. Web Push endpoints are always public-only.
- Per connection, a cap on concurrent upstream requests and on idle streaming
  responses; beyond it the proxy answers 503.

---

## 6. Credentials at rest

- AEAD: **XChaCha20-Poly1305**, random 24-byte nonce per write,
  AAD = `connection_id ‖ field ‖ key_version`, stored as
  `key_version ‖ nonce ‖ ciphertext`.
- Master key: 32 random bytes generated on first run at `<data>/master.key`
  (created exclusively, mode 0600, permissions checked on load), or supplied
  through an environment variable or a systemd credential. Held in zeroizing
  memory.
- `key_version` exists from day one; `roost gateway rotate-key` re-encrypts
  every row.
- Without the master key, credentials are unrecoverable; backups must carry it
  (umbrella §12.4). *(G-20: the predecessor stored credentials in plaintext.)*

**Stated plainly in the docs:** the collector holding the gateway is a single
point of compromise for every integration it holds. Hats limit what one
session's token reaches; they do not protect against compromise of the
collector. In the default `roost up` install the collector runs as the same OS
user as every agent, so any agent can read `master.key` and `roost.db` and
decrypt every grant. Whenever the gateway holds credentials for more than one
hat, the collector should run as a separate OS user or in a container (the
Docker image); `roost up` warns in that situation (kernel §10). Keeping
`master.key` in the OS keystore is an open question (kernel §12).

---

## 7. Health, probe and notification

- **Status values:** `not_connected` (no credential yet), `ok`,
  `needs_auth` (the vendor refused the credential), `error` (outage or
  transport failure — a click will not fix it).
- **Probe:** every 15 minutes, for OAuth connections with a credential only
  (static and `none` connections are not probed; `not_connected` ones are never
  probed). The probe performs a real MCP handshake through the same forwarding
  path: `initialize` → `notifications/initialized` → `tools/list` on that
  session → `DELETE`. *(G-21: a session-less `tools/list` gets 400 from
  stateful servers; the first probe design reported healthy connections as
  down.)*
  - 2xx → `ok`; 401 after one single-flight refresh → `needs_auth`; 5xx or
    transport failure → `error`; any other 4xx → **no change**.
- **Live traffic** also updates status: a 2xx through a connection marked
  `needs_auth`/`error` sets `ok`.
- A `needs_auth` connection stays mounted (unmounting on a possibly transient
  failure would churn every session's configuration).
- **Notifier** (`Notifier` interface, umbrella §10.2): fires on **transitions**
  into or out of `needs_auth`/`error` only; a problem present at startup is
  announced once. Full mode: Web Push + a badge in the MCP view. Standalone:
  log line and an optional webhook. `error` wording never asks the operator to
  reconnect. *(G-22: re-posting every tick trains people to ignore it; first
  implementation told the operator to click "Connect" during an outage.)*
- Every background loop has panic isolation and a per-tick timeout.

---

## 8. Renderers (standalone and terminal use)

- Targets: `~/.claude.json` (`mcpServers`) and `~/.codex/config.toml`
  (`[mcp_servers.<name>]`).
- **Ownership is in the data, not in comments.** An entry is roost's iff its
  name starts with `roost-` **and** its URL starts with `<public_url>/mcp/`.
  *(G-23: `codex mcp add/remove` rewrites `config.toml` and drops comments; the
  predecessor's comment-delimited block then became "foreign" and its entries
  were orphaned forever.)*
- An existing entry with a roost name that is not roost-owned is skipped and
  reported, never overwritten.
- **Semantic no-op:** if the owned entries already equal the desired set, the
  file is left byte-identical. *(G-24: Claude Code rewrites the file in its own
  key order, so a re-marshal never byte-matched and every reconcile rewrote
  it.)*
- Malformed input is refused without writing. Writes are atomic
  (temp + rename) and leave mode 0600, enforced even when content is
  unchanged. Large integers round-trip exactly.
- TOML is edited with a format-preserving parser (`toml_edit`), never with
  line scanners, and entry names are validated as safe TOML keys.
- `headers` are written (`headers` object for Claude, `http_headers` table for
  Codex).

---

## 9. API

All operator endpoints require an operator session. Endpoints marked
**step-up** also require a fresh passkey or password check (kernel §3.4).

| Method & path | Purpose |
|---|---|
| `GET /api/mcp/connections` | List (no secrets; `has_credential`, status, `account_label`, mounts). |
| `POST /api/mcp/connections` | Create (**step-up**). |
| `PATCH /api/mcp/connections/{id}` | Update (**step-up** when the URL, credential kind or `internal_network` changes); origin or kind change clears credentials (§4.6). |
| `DELETE /api/mcp/connections/{id}` | Delete with mounts, credential, OAuth client. |
| `PUT /api/mcp/connections/{id}/mounts` | Replace the host set (full set, never a delta). |
| `PUT /api/mcp/connections/{id}/credential` | Set a static token (write-only, 204; **step-up**). |
| `PUT /api/mcp/connections/{id}/oauth-client` | Set a pre-registered client (**step-up**). |
| `POST /api/mcp/connections/{id}/authorize` | Start OAuth; sets the flow cookie, returns the consent URL. |
| `GET /api/mcp/oauth/callback` | OAuth redirect target (`state` plus flow cookie). |
| `GET/PUT /api/mcp/stdio-servers?host_id&hat_id` | Local stdio servers for one (host, hat), full set (§3.4; **step-up** on `PUT`). |
| `GET /api/mcp/clients` / `POST` / `DELETE /{id}` | Standalone clients. `POST {label, hat_id, connection_ids[]}` creates the client and its pins; the token is shown once. |
| `PUT /api/mcp/clients/{id}/pins` | Replace a client's pinned connections (`{connection_ids[]}`). |
| `GET /api/mcp/manifest` | Manifest for the presenting standalone client token (renderers). |

No endpoint returns a session token, and no endpoint lets one host read another
host's tokens. *(G-25: the predecessor's pull endpoint took the target machine
from the path behind a shared token, so any host could read any other host's
token.)* Session tokens reach a host only inside the
`start_session`/`resume_session` frames for that session, over the
authenticated host connection.

The consent popup is opened blank inside the click handler, its `opener` set to
`null`, and navigated when the authorize call returns (passing `noopener` to
`window.open` makes it return `null`; opening after an `await` is blocked).
Frontend spec §8.

---

## 10. Standalone mode

`roost gateway` runs the kernel (operator auth, storage, HTTP) and this crate
only. `ClientIdentity` resolves standalone client tokens, `MountPolicy` reads
the client's pins (`gw_client_pins`), `Notifier` logs and calls an optional
webhook. There are no session tokens and no stdio servers. The frontend shows only the MCP and Settings views. The build proves
the boundary: the `roost-gateway` crate does not depend on `roost-sessions`.

---

## 11. Testing

- **Streaming:** first chunk before upstream completion; fails fast on a
  buffering implementation. SSE through the proxy with a long `tools/call`.
- **401 handling:** static token rejected → 502 `upstream_auth`, no
  `WWW-Authenticate`; OAuth refresh+retry; retry rejected → `needs_auth`;
  non-401 errors pass through.
- **Fake OAuth server:** PR discovery (challenge, path-inserted, origin),
  AS metadata forms, DCR success and refusal text, pre-registered confidential
  client, PKCE verification, `resource` on consent/exchange/refresh and the
  retry without it, rotating refresh tokens with reuse detection (concurrent
  401s share one refresh), refresh without a new refresh token, caller
  cancellation mid-refresh; callback without the matching flow cookie refused;
  `http` metadata endpoints refused.
- **Egress:** redirects not followed (proxy and OAuth); loopback, link-local,
  metadata-service and private addresses refused unless the connection is
  marked internal; per-connection concurrency cap; proxy responses carry
  `nosniff` and other content types become 502.
- **Scope negative tests:** a session token for hat A requesting a hat-B
  connection → 404; unmounted connection → 404; token of a parked, closed or
  superseded session → 404; standalone client outside its pins → 404.
- **Token hygiene:** no token or `mcp_servers` header appears in logs, events
  or SSE output.
- **Allowlist:** JSON and SSE `tools/list` filtering, batch, empty result `[]`,
  blocked `tools/call`.
- **Capabilities:** `initialize` upstream lacks `sampling`/`elicitation`/`roots`.
- **Credential edits:** origin change and kind change clear credentials.
- **Renderers:** on real Claude and Codex config files — foreign entries
  untouched, roost-named foreign entry skipped, semantic no-op byte-identical,
  malformed input refused, mode 0600, entries survive a `codex mcp add/remove`
  rewrite.
- **Encryption:** AAD binding (swapping ciphertext between rows fails),
  key rotation, missing key → clear error.
- **Live gate** (per release, optional vendor accounts): one static-token
  connection and one OAuth connection end to end through a real agent session.

---

## 12. Predecessor incidents and gaps referenced

| Id | Incident or gap | Rule |
|---|---|---|
| G-1 | No pre-registered client kind; some vendors impossible | §2 |
| G-2 | Token in URL path | §3.2 |
| G-3 | Origin-only discovery found a legacy AS | §4.1 |
| G-4 | PR `resource` never validated | §4.1 |
| G-5 | Scope-less tokens unless requested | §4.1 |
| G-6 | DCR refusal reason hidden | §4.2 |
| G-7 | Re-registration broke a live grant | §4.2 |
| G-8 | Callback re-read mutable state | §4.3 |
| G-9 | Expiry stored but never used | §4.4 |
| G-10 | Refresh omitted `resource` and scopes | §4.4 |
| G-11 | Abandoned refresh lost a rotated grant | §4.5 |
| G-12 | Caller cancellation flagged `needs_auth` | §4.5 |
| G-13 | Late refresh overwrote a new grant | §4.5 |
| G-14 | Edited URL would send the token elsewhere | §4.6 |
| G-15 | `Set-Cookie` passed through | §5.2 |
| G-16 | DELETE not forwarded | §5.2 |
| G-17 | Upstream 401 triggered agent-side OAuth | §5.4 |
| G-18 | Allowlist did not block calls | §5.5 |
| G-19 | Sampling/elicitation not actually stripped | §5.6 |
| G-20 | Plaintext credentials | §6 |
| G-21 | Session-less probe gave false outages | §7 |
| G-22 | Notification noise and wrong advice | §7 |
| G-23 | Comment markers lost on Codex rewrite | §8 |
| G-24 | Needless rewrites of `~/.claude.json` | §8 |
| G-25 | Pull endpoint leaked other hosts' tokens | §9 |

---

## 13. Open questions

1. **Vendor coverage.** Measured behaviour exists for six vendors, but token
   lifetimes, rotation and reuse detection were never observed over days.
   Worth a small live-gate matrix once the gateway exists.
2. **`GET` SSE channel.** Forwarded in v1; whether any vendor uses it for
   server-initiated messages that roost then has to refuse (§5.6) is unknown.
3. **Static-token header name.** Most vendors take `Authorization: Bearer`;
   some want a custom header (e.g. an API-key header). The connection form
   allows a header name; is a value template (prefix) also needed?

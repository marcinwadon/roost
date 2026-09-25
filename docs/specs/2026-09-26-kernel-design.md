# roost — kernel (subsystem spec)

- **Date:** 2026-09-26
- **Status:** Draft, awaiting review
- **Refines:** [architecture spec](2026-09-25-roost-architecture-design.md) §4
  (data model), §7 (auth and pairing), §8 (hats), §9 (kernel), §12.4
  (backups). Consumed by the [ACP core](2026-09-26-acp-core-design.md),
  [MCP gateway](2026-09-26-mcp-gateway-design.md) and
  [frontend](2026-09-26-frontend-design.md) specs.

The kernel is the `roost-kernel` crate: what every mode of the collector needs
regardless of whether sessions or the gateway are enabled — operator auth,
host pairing and identity, hats, push delivery, outbound HTTP policy,
settings, storage and configuration.

---

## 1. Storage

- One SQLite database, `<data>/roost.db`, WAL mode, `rusqlite` with the
  bundled SQLite (no system library; static builds).
- **One writer thread** owns the write connection and receives work over a
  channel; reads use a small pool. This serialises writes by construction,
  which idempotent ingest (ACP core §8) relies on.
- Migrations are embedded (`rusqlite_migration`) and run at startup before
  anything listens. A database newer than the binary refuses to start with a
  clear message (downgrades are not supported).
- Every table has `owner_id` (umbrella §4). In v1 it is always the single
  owner's id and every query filters by it.
- **Data directory:** `$ROOST_DATA_DIR`, else `$XDG_DATA_HOME/roost`
  (Linux) / `~/Library/Application Support/roost` (macOS); a host on the same
  machine keeps its own files there too (distribution §8). Collector contents:
  `roost.db`, `master.key`, `vapid.key`, `config.toml`, `admin.sock`,
  `setup-url` (until setup), `attachments/` (ACP core §7).

### 1.1 Kernel tables

```sql
owners(id TEXT PK, contact NULL, created_at)
password_credentials(owner_id PK, phc TEXT, updated_at)
passkeys(id TEXT PK, owner_id, credential JSON, label, created_at, last_used_at)
auth_sessions(id_hash PK, owner_id, user_agent, created_at, last_seen_at,
  last_step_up_at NULL, expires_at)
hosts(id TEXT PK, owner_id, name, public_key, default_hat_id, platform,
  host_version, capabilities JSON, agents JSON, workspace_roots JSON,
  last_doctor JSON, last_seen_at, created_at, revoked_at NULL)
pairing_codes(code_hash PK, owner_id, created_at, expires_at, used_at NULL)
settings(owner_id, key, value, PRIMARY KEY(owner_id, key))   -- public_url, contact, push defaults
project_recents(owner_id, host_id, hat_id, path, last_used_at,
  PRIMARY KEY(host_id, hat_id, path))
push_subscriptions(id TEXT PK, owner_id, endpoint, p256dh, auth, device_label,
  created_at, last_success_at, last_error)
purged_hats(hat_id PK, owner_id, purged_at)                  -- §5.5
```

`hats` and `hat_path_rules` are in §5.1.

## 2. Configuration

Precedence: CLI flags > environment (`ROOST_*`) > `<data>/config.toml` >
defaults. Settings that the operator edits in the UI (`public_url`, push
policy) live in the database, not in the file. Secrets are never accepted as
CLI flags (they would show in process listings); they come from files, the
environment, or systemd credentials.

## 3. Operator authentication

### 3.1 Setup

- First start with an empty database: generate a 256-bit setup token and
  write `<public_url or http://localhost:PORT>/setup/<token>` to
  `<data>/setup-url` (mode 0600). The full link is printed to the terminal
  **only when stdout is a TTY**; otherwise (service, container) only the path
  of the `setup-url` file is logged, so the token never lands in a log
  collector. Valid for 1 hour, single use; a restart before setup issues a new
  one and invalidates the old.
- The setup form sets the owner password, `public_url` (pre-filled from the
  request's origin, must be `https://` or a loopback `http://` origin), and the
  name of the default hat; it then offers passkey registration.

### 3.2 Login

- **Password:** Argon2id (PHC string, `password-auth`), verification on a
  blocking thread. Login attempts are rate limited per client address
  (5 per minute, then exponential backoff), with a constant-time failure path.
- **Passkeys:** `webauthn-rs` with RP id and origin derived from `public_url`;
  subdomains and arbitrary ports not allowed. Ceremony state is kept in memory
  keyed by a short-lived ceremony id. Several passkeys per owner, each with a
  label and last-used time; removable while at least one other login method
  remains.
- **Sessions:** server-side (`auth_sessions`: random 256-bit id hashed at
  rest). Cookie `roost_session`: `HttpOnly`, `Secure` (except loopback),
  `SameSite=Strict`, `Path=/`. 30-day sliding expiry. Settings lists sessions
  and can revoke them.
- **Changing `public_url`** changes the passkey RP id and the OAuth redirect
  URI: existing passkeys stop working and OAuth registrations must be redone.
  Settings says so before saving, and the change requires step-up (§3.4).

*Deferred:* identity from a fronting proxy. A header trusted by peer address
is forgeable by any local process when the proxy runs on loopback; a later
version may accept signed assertions only (umbrella §7.1, §15).

### 3.3 Origin rules and CSRF

State-changing endpoints accept only `application/json`. `Origin` is checked
per route class:

| Route | Authentication | `Origin` |
|---|---|---|
| Browser routes, state-changing methods (`POST`, `PUT`, `PATCH`, `DELETE`) | Session cookie | Must match `public_url`; a missing `Origin` is rejected |
| Browser routes, `GET` (JSON, SSE streams, attachments, logos) | Session cookie (`SameSite=Strict`) | Browsers do not send `Origin` on same-origin `GET`s, so these check `Sec-Fetch-Site` instead: `same-origin` or `none` accepted, anything else rejected; a present `Origin` must match |
| `GET /api/hosts/ws` | Unauthenticated until a valid `hello` proof (ACP core §3.5) | Exempt |
| `POST /api/hosts/enroll` | Pairing code | Exempt |
| `/mcp/*` (gateway proxy) | Bearer token | Exempt |
| `GET /api/mcp/oauth/callback` | `state` plus the flow cookie (gateway §4.3) | Exempt |
| `/healthz`, `/readyz` | None (no data) | Exempt |

### 3.4 Step-up authentication

Some actions require a password or passkey check within the last **5
minutes** (`auth_sessions.last_step_up_at`), even inside a valid session:

- minting pairing codes;
- creating or editing gateway connection URLs, credentials, pre-registered
  clients or the "internal network" flag;
- local stdio server configuration;
- revoking hosts, auth sessions or passkeys;
- deleting sessions and purging hats;
- changing `public_url`.

Without a fresh check the endpoint answers 403 `step_up_required`; the
frontend prompts and retries (frontend spec §3).

## 4. Host identity and pairing

### 4.1 Pairing

1. `POST /api/hosts/pairing-codes` (operator, step-up) → code: 8 characters
   from a base32 alphabet without ambiguous characters, displayed as
   `XXXX-XXXX`; TTL 10 minutes; single use; stored hashed.
2. `roost host join <public_url> <code>` on the machine: the host generates an
   Ed25519 keypair (`host.key` in its data dir, 0600; distribution §8) and
   calls `POST /api/hosts/enroll {code, public_key, name, host_version,
   platform}`.
3. The collector creates the `hosts` row with the installation's default hat
   as its default hat and returns `{host_id}`. Transport security is TLS.
4. The host connects (`/api/hosts/ws`) and proves possession in `hello`
   (ACP core §3.5).

**Idempotent:** if `host.key` already exists and the collector accepts it,
`host join` does not pair again. Enrollment is rate limited **per client
address** (5 wrong codes per 10 minutes, then exponential backoff); wrong codes
never invalidate other outstanding codes.

### 4.2 All-in-one pairing and the admin socket

`roost up`'s supervisor pairs its own host with no operator step: on first
start, the collector child hands the supervisor one pairing code over an
**inherited file descriptor**, and the supervisor passes it to the host child
the same way — never on a command line or in the environment. If the host's
existing key is accepted, nothing is minted. A revoked all-in-one host is not
re-paired automatically.

The collector listens on `<data>/admin.sock` (Unix socket, mode 0600) for
`roost admin …` recovery commands (reset password, print setup URL, list
hosts, restart pending) and for `roost backup` / `roost restore`.
**Destructive commands** (backup, restore, password reset, pairing-code
minting) require interactive confirmation on a TTY. The confirmation is
enforced by the CLI: it stops accidental and non-interactive use, not a process
that speaks the socket protocol directly (§10).

### 4.3 Host lifecycle

- Rename, change default hat, **revoke** (step-up). Revoking closes the host's
  connection, rejects future `hello`s with `revoked`, and calls the lifecycle
  hooks (§5.5): sessions are parked and their gateway tokens revoked. The
  host's adapters keep running until it next connects; it is then told it is
  revoked and stops them.
- `last_seen`, versions, platform, capabilities, workspace roots, agent
  availability and the last doctor report are updated from `hello` and
  `probe_agents`.
- One live connection per host (ACP core §3.5).

## 5. Hats

### 5.1 Model

```sql
hats(id TEXT PK, owner_id, name, colour, logo_mime NULL, logo_bytes NULL,
  push_policy JSON, created_at)
hat_path_rules(id TEXT PK, owner_id, host_id, prefix, hat_id, verified BOOL)
```

`hosts.default_hat_id` names each host's default hat. Setup creates one hat; it
is the default for new hosts until changed. A hat referenced by a host default
cannot be deleted; otherwise hats are removed by purge (§5.5).

**Logos** are uploaded as SVG or PNG (≤ 64 KiB), **sanitised server-side**
(SVG: scripts, event handlers, `foreignObject`, external references and
non-`data:` URLs removed; PNG: decoded and re-encoded) and served from
`GET /api/hats/{id}/logo` with `nosniff` and a `default-src 'none'`
policy. The frontend renders them only as `<img>`, never inline.

### 5.2 Resolution

Given `(host_id, path)`:

1. The path is **canonical**: absolute, symlinks resolved, no `.`/`..`, no
   trailing slash. Canonicalisation happens **on the host**, which is where the
   filesystem is (`resolve_path` request, §5.4). Rule prefixes are stored
   canonicalised the same way (resolved through the host when the rule is
   saved; a rule for a path that does not exist is stored as typed, normalised
   lexically, and marked unverified).
2. Candidate rules are those of that host whose prefix equals the path or is a
   **path-segment prefix** of it (`/p/acme` matches `/p/acme` and `/p/acme/x`,
   never `/p/acme-infra`).
3. The longest candidate prefix wins; with no candidate, the host's default
   hat.

**At start:** `resolve_path` → rule match → the hat is stored on the session
with the canonical cwd → `start_session` (ACP core §4.3). **At resume** the
hat is re-resolved; a mismatch refuses the resume until the operator
re-assigns the session. Re-assignment is allowed for any session with no
running adapter (parked, closed or failed), with a warning, and writes a `hat_reassigned{from, to}` timeline event
(ACP core §4.9).

### 5.3 Where hats are enforced (kernel side)

- Project recents are stored per (host, hat) and returned only for the hat the
  browsing path resolves to.
- Push notifications follow the session's hat policy (§6).
- The gateway's `MountPolicy` joins mounts with the principal's hat (gateway
  spec §3.1).

### 5.4 Protocol addition

`resolve_path{path}` → `resolved_path{canonical, exists, is_dir}` (or `error`)
is part of the frame catalogue (ACP core §3.3). It is used for typed paths in
New session, for session start and resume, for rule saving, and by the hat
tester.

### 5.5 Lifecycle hooks and purge

The kernel defines a `LifecycleHooks` trait with `on_host_revoked(host_id)`
and `on_hat_purged(hat_id)`; `roost-sessions` and `roost-gateway` each
implement it, so the kernel never imports either.

**Purge a hat** (`POST /api/hats/{id}/purge`, step-up; the default hat of any
host cannot be purged until the hosts are moved to another hat):

- sessions: every session of the hat is deleted as in ACP core §4.10;
- gateway: its connections with their grants, mounts, session tokens,
  standalone clients and stdio servers are deleted (gateway §2);
- kernel: its path rules, project recents and the hat row are deleted, and the
  hat is recorded in `purged_hats`;
- hosts: after every handshake the collector sends `forget_hat{hat_id}` for
  each hat in `purged_hats` (kept 30 days); the host deletes its composed agent
  home for that hat (ACP core §6) once no process of that hat runs. Deletion
  is idempotent.

## 6. Push

- **VAPID keys:** P-256 key pair generated on first run into `<data>/vapid.key`
  (0600). The VAPID `sub` claim is `mailto:<owner contact>` if configured, else
  the `public_url`; never a non-routable placeholder (iOS silently rejects
  those).
- **Delivery:** `web-push-native` builds RFC 8291 (aes128gcm) requests with
  VAPID (RFC 8292), sent with the shared HTTP client under the egress policy
  (§7.1; push endpoints must be public). TTL 1 hour, urgency `high` for "needs
  your answer", `normal` otherwise. Non-2xx responses are logged with status;
  404/410 delete the subscription.
- **Policy** per hat: `muted`; `details` (include the agent's question title);
  `generic_title` ("Session needs your answer", without the session title).
  Default: not muted, no details, session title shown. Triggers are defined by
  the modules that own them (ACP core §10, gateway §7).

## 7. HTTP server

- `axum` on one listener (default `127.0.0.1:7117`, configurable). TLS is
  provided by the deployment topology (umbrella §7.5); the kernel does not
  terminate TLS in v1.
- Static assets are embedded (`rust-embed`, deterministic timestamps): hashed
  assets `Cache-Control: immutable`, `index.html` and the service worker
  `no-cache` with an ETag.
- Response compression for JSON and HTML only; **never** on SSE or the gateway
  proxy routes.
- Structured logging (`tracing`), JSON optional. Secrets never logged: tokens,
  cookies, `Authorization` headers and OAuth parameters are redacted by a
  shared layer.

### 7.1 Outbound HTTP (egress policy)

One shared `reqwest` client for every outbound call — gateway proxy, OAuth
discovery/registration/token calls, Web Push:

- redirects are never followed;
- DNS is resolved by roost and each address is checked before connecting:
  loopback, link-local (including `169.254.169.254`), RFC 1918, unique-local
  IPv6, CGNAT and other non-public ranges are refused, unless the caller passes
  an explicit "internal network" allowance (a gateway connection the operator
  marked so; never Web Push);
- per-caller limits on concurrent requests and idle streams (gateway §5.7).

It lives in the kernel so that push delivery can use it without depending on
the gateway.

### 7.2 Content-Security-Policy

Every HTML response carries:

```
Content-Security-Policy: script-src 'self' 'sha256-<theme bootstrap>';
  img-src 'self' data: blob:; object-src 'none'; frame-ancestors 'none';
  base-uri 'none'
```

The only inline script is the theme bootstrap (frontend spec §8), whose hash is
computed at build time. Together with the markdown pipeline (no raw HTML,
frontend spec §6.4) this makes agent output unable to run script in the UI.

## 8. API

| Method & path | Purpose |
|---|---|
| `GET /api/capabilities` | `{mode: full \| gateway, features[]}` |
| `POST /api/setup` | One-time owner setup |
| `POST /api/auth/login` / `logout` | Password login / logout |
| `POST /api/auth/passkeys/{register,login}/{start,finish}` | WebAuthn ceremonies |
| `POST /api/auth/step-up/password`, `…/step-up/passkey/{start,finish}` | Step-up (§3.4) |
| `GET/DELETE /api/auth/sessions[/{id}]` | Signed-in devices (revoke: step-up) |
| `GET/PATCH /api/settings` | `public_url` (step-up), contact, push defaults |
| `POST /api/hosts/pairing-codes` | Mint a pairing code (step-up) |
| `POST /api/hosts/enroll` | Host enrollment (code-authenticated) |
| `GET /api/hosts`, `PATCH/DELETE /api/hosts/{id}` | List, rename/default hat, revoke (step-up) |
| `GET /api/hosts/ws` | Host WebSocket (ACP core) |
| `GET/POST /api/hats`, `PATCH /api/hats/{id}` | Hats |
| `GET/PUT /api/hats/{id}/logo` | Sanitised logo (§5.1) |
| `POST /api/hats/{id}/purge` | Purge a hat (step-up, §5.5) |
| `GET/PUT /api/hosts/{id}/path-rules` | Path rules (full set) |
| `POST /api/hats/resolve` | `{host_id, path}` → `{canonical, hat_id, rule_id?}` |
| `GET /api/push/vapid`, `POST/DELETE /api/push/subscriptions` | Push |
| `GET /healthz`, `GET /readyz` | Process up / database ready |

## 9. Backups

`roost backup <file>` (via the admin socket, TTY confirmation) writes a
tarball containing a consistent copy of `roost.db` (SQLite online backup API),
`attachments/`, `master.key` and `vapid.key`, mode 0600, with a warning that it
contains every secret roost holds. `roost restore <file>` refuses to overwrite
an existing data directory without `--force`.

## 10. Threat model and deployment

- **In the default `roost up` install the collector runs as the same OS user
  as every agent.** Any agent can therefore read `master.key`, `roost.db` (all
  transcripts and every encrypted grant, which `master.key` decrypts) and use
  `admin.sock`. Hats do not change this (umbrella §8.4).
- **Recommendation:** whenever the gateway holds credentials for more than one
  hat, run the collector as a separate OS user or in a container (the Docker
  image), with the hosts paired to it like any remote host.
- **`roost up` warns** at start, in Settings and in `doctor` when gateway
  credentials exist for more than one hat and the collector shares its OS user
  with the host child.
- The admin socket's TTY confirmation (§4.2) protects against accidents, not
  against a local process of the same user.

## 11. Testing

- Setup token single use and expiry; restart invalidates it; the link is
  printed only to a TTY.
- Login rate limiting; constant-time failure path.
- Passkey flows with a software authenticator (`passkey` crate).
- `Origin` rules per route class (§3.3), including a missing `Origin`.
- Step-up: every listed action refused without a fresh check, accepted within
  5 minutes, refused after.
- CSP header present on every HTML response; the theme bootstrap hash matches
  the built file.
- Egress: redirects not followed; private, link-local and metadata addresses
  refused; internal-network allowance honoured only where granted.
- Pairing: expiry, single use, per-address rate limiting without invalidating
  other codes; idempotent re-join; revoked host rejected in `hello`.
- Hat resolution table tests: segment match, longest prefix, default fallback,
  symlinked paths (canonical on host), unverified rules.
- Logo sanitisation: scripts, event handlers and external references removed.
- Purge: sessions, gateway rows and rules removed; `forget_hat` sent after
  handshakes.
- Push: 410 removes the subscription; non-2xx logged; generic title honoured.
- Migration from every released schema version (fixtures kept per release).

## 12. Open questions

1. **Owner contact for VAPID** — ask at setup, or derive from `public_url`
   only?
2. **Multiple listeners** (loopback plus a LAN address) — needed in v1, or is
   one address plus a reverse proxy enough?
3. **`master.key` in the OS keystore** (macOS keychain, Secret Service) instead
   of a file. It would stop same-user agents from reading the key file
   directly, but not from asking the running collector; weigh against headless
   and container installs.

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
host pairing and identity, hats, push delivery, settings, storage and
configuration.

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
  (Linux) / `~/Library/Application Support/roost` (macOS). Contents:
  `roost.db`, `master.key`, `vapid.key`, `config.toml`, `admin.sock`.

## 2. Configuration

Precedence: CLI flags > environment (`ROOST_*`) > `<data>/config.toml` >
defaults. Settings that the operator edits in the UI (`public_url`, push
policy) live in the database, not in the file. Secrets are never accepted as
CLI flags (they would show in process listings); they come from files, the
environment, or systemd credentials.

## 3. Operator authentication

### 3.1 Setup

- First start with an empty database: generate a 256-bit setup token, print
  `<public_url or http://localhost:PORT>/setup/<token>` to the log and write it
  to `<data>/setup-url` (mode 0600). Valid for 1 hour, single use; a restart
  before setup issues a new one and invalidates the old.
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
- **Sessions:** server-side table (`auth_sessions`: random 256-bit id hashed at
  rest, created, last seen, user agent, expiry). Cookie `roost_session`:
  `HttpOnly`, `Secure` (except loopback), `SameSite=Strict`, `Path=/`. 30-day
  sliding expiry. Settings lists sessions and can revoke them.
- **CSRF:** `SameSite=Strict` plus an `Origin` check on every state-changing
  request and on SSE/WebSocket upgrades; state-changing endpoints accept only
  `application/json`.

### 3.3 Trusted-proxy mode

Optional (`auth.trusted_proxy`): `{proxies: [CIDR…], header: "…", identity:
"…"}`. A request whose peer address is in `proxies` and whose header equals the
configured identity is treated as the owner. The header is **rejected** (400,
logged) from any other peer. Password and passkey login keep working
alongside, so a misconfigured proxy cannot lock the operator out.

## 4. Host identity and pairing

### 4.1 Pairing

1. `POST /api/hosts/pairing-codes` (operator) → code: 8 characters from a
   base32 alphabet without ambiguous characters, displayed as `XXXX-XXXX`;
   TTL 10 minutes; single use; stored hashed.
2. `roost host join <public_url> <code>` on the machine: the host generates an
   Ed25519 keypair (private key in its data dir, 0600) and calls
   `POST /api/hosts/enroll {code, public_key, name, host_version, platform}`.
3. The collector creates the `host` row with the installation's default hat as
   its default hat and returns `{host_id, collector_public_key}`. The host pins
   the collector's key only as information for `doctor`; transport security is
   TLS.
4. The host connects (`/api/hosts/ws`) and proves possession in `hello`
   (ACP core §3.5).

Enrollment endpoints are rate limited; five wrong codes in a row invalidate all
outstanding codes.

### 4.2 All-in-one pairing

The collector listens on `<data>/admin.sock` (Unix socket, mode 0600) for
local administrative calls. `roost up`'s supervisor uses it to mint a pairing
code and passes it to the host child on its command line via a file
descriptor, so the local host pairs itself with no operator step. The admin
socket also serves `roost admin …` commands (reset password, print setup URL,
list hosts) for recovery.

### 4.3 Host lifecycle

- Rename, change default hat, **revoke** (the collector closes its connection,
  rejects future `hello`s with `revoked`, revokes its gateway clients and parks
  its sessions).
- `last_seen`, versions, platform, agent availability and the last doctor
  report are updated from `hello` and `probe_agents`.

## 5. Hats

### 5.1 Model

`hats(id, owner_id, name, colour, logo_svg NULL, logo_png NULL,
push_policy JSON, created_at)`, `hat_path_rules(id, host_id, prefix, hat_id)`,
and `host.default_hat_id`. Setup creates one hat; it is the default for new
hosts until changed. A hat referenced by a host default, a rule, a session or a
connection cannot be deleted (the UI shows what references it).

### 5.2 Resolution

Given `(host_id, path)`:

1. The path is **canonical**: absolute, symlinks resolved, no `.`/`..`, no
   trailing slash. Canonicalisation happens **on the host**, which is where the
   filesystem is (`resolve_path` request, §5.4). Rule prefixes are stored
   canonicalised the same way (resolved through the host when the rule is
   saved; a rule for a path that does not exist is stored as typed, normalised
   lexically, and marked "unverified").
2. Candidate rules are those of that host whose prefix equals the path or is a
   **path-segment prefix** of it (`/p/acme` matches `/p/acme` and `/p/acme/x`,
   never `/p/acme-infra`).
3. The longest candidate prefix wins; with no candidate, the host's default
   hat.

The hat is stored on the session at start (umbrella §8.2). Re-assignment is an
explicit operation that writes a timeline event `hat_reassigned{from, to, by}`.

### 5.3 Where hats are enforced (kernel side)

- Project recents are stored per (host, hat) and returned only for the hat the
  browsing path resolves to.
- Push notifications follow the session's hat policy (§6).
- The gateway's `MountPolicy` joins mounts with the principal's hat (gateway
  spec §3.1).

### 5.4 Protocol addition

ACP core §3.3 gains one collector→host request: `resolve_path{path}` →
`resolved_path{canonical, exists, is_dir}` (or `error`). It is used for typed
paths in New session, for rule saving, and by the hat tester.

## 6. Push

- **VAPID keys:** P-256 key pair generated on first run into `<data>/vapid.key`
  (0600). The VAPID `sub` claim is `mailto:<owner contact>` if configured, else
  the `public_url`; never a non-routable placeholder (iOS silently rejects
  those).
- **Delivery:** `web-push-native` builds RFC 8291 (aes128gcm) requests with
  VAPID (RFC 8292), sent with the shared `reqwest` client. TTL 1 hour, urgency
  `high` for "needs your answer", `normal` otherwise. Non-2xx responses are
  logged with status; 404/410 delete the subscription.
- `push_subscriptions(id, owner_id, endpoint, p256dh, auth, device_label,
  created_at, last_success_at, last_error)`.
- **Policy** per hat: `muted`, `details` (include the agent's question title),
  default: not muted, no details. Triggers are defined by the modules that own
  them (ACP core §10, gateway §7).

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

## 8. API

| Method & path | Purpose |
|---|---|
| `GET /api/capabilities` | `{mode: full \| gateway, features[]}` |
| `POST /api/setup` | One-time owner setup |
| `POST /api/auth/login` / `logout` | Password login / logout |
| `POST /api/auth/passkeys/{register,login}/{start,finish}` | WebAuthn ceremonies |
| `GET/DELETE /api/auth/sessions[/{id}]` | Signed-in devices |
| `GET/PATCH /api/settings` | `public_url`, contact, push defaults |
| `POST /api/hosts/pairing-codes` | Mint a pairing code |
| `POST /api/hosts/enroll` | Host enrollment (code-authenticated) |
| `GET /api/hosts`, `PATCH/DELETE /api/hosts/{id}` | List, rename/default hat, revoke |
| `GET /api/hosts/ws` | Host WebSocket (ACP core) |
| `GET/POST /api/hats`, `PATCH/DELETE /api/hats/{id}` | Hats |
| `GET/PUT /api/hosts/{id}/path-rules` | Path rules (full set) |
| `POST /api/hats/resolve` | `{host_id, path}` → `{canonical, hat_id, rule_id?}` |
| `GET /api/push/vapid`, `POST/DELETE /api/push/subscriptions` | Push |

## 9. Backups

`roost backup <file>` (via the admin socket) writes a tarball containing a
consistent copy of `roost.db` (SQLite online backup API), `master.key` and
`vapid.key`, mode 0600, with a warning that it contains every secret roost
holds. `roost restore <file>` refuses to overwrite an existing data directory
without `--force`.

## 10. Testing

- Setup token single use and expiry; restart invalidates it.
- Login rate limiting; constant-time failure path.
- Passkey flows with a software authenticator (`passkey` crate).
- Trusted-proxy header rejected from a non-proxy peer.
- `Origin` enforcement on state changes, SSE and WebSocket upgrades.
- Pairing: expiry, single use, lockout after repeated failures; revoked host
  rejected in `hello`.
- Hat resolution table tests: segment match, longest prefix, default fallback,
  symlinked paths (canonical on host), unverified rules.
- Push: 410 removes the subscription; non-2xx logged.
- Migration from every released schema version (fixtures kept per release).

## 11. Open questions

1. **Owner contact for VAPID** — ask at setup, or derive from `public_url`
   only?
2. **Multiple listeners** (loopback plus a LAN address) — needed in v1, or is
   one address plus a reverse proxy enough?

# Walking skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A thin, real, end-to-end slice of roost: a browser-less API client starts a session on a host, sends a prompt, and reads the agent's streamed reply from the collector — with the durability rules (outbox, idempotent ingest, one turn at a time) in place from day one.

**Architecture:** A Cargo workspace of six crates. `roost-proto` owns every wire type and generates JSON Schema and TypeScript from them. `roost-host` runs one ACP adapter process per session, persists every state-bearing fact in an on-disk outbox and relays it over an outgoing WebSocket. `roost-sessions` is the collector side: it ingests frames idempotently into SQLite, acks after commit, resolves HTTP requests from the ingested facts, and serves REST + SSE. `roost-testkit` holds a scripted fake ACP adapter built on the same ACP crate real adapters use, plus the end-to-end tests. `roost` is the binary.

**Tech Stack:** Rust (edition 2024, MSRV 1.88), tokio, axum 0.8 (HTTP, SSE, WebSocket server), tokio-tungstenite 0.29 (WebSocket client), agent-client-protocol 2.2.0, rusqlite 0.40 (bundled SQLite), schemars 1.2 + ts-rs 12 (codegen), clap 4, reqwest (tests). Nix flake dev shell.

**Spec:** [`docs/specs/2026-09-26-acp-core-design.md`](../specs/2026-09-26-acp-core-design.md) (primary), with the [architecture spec](../specs/2026-09-25-roost-architecture-design.md) §5–§6 and the [kernel spec](../specs/2026-09-26-kernel-design.md) §1. Read the ACP core spec before starting; this plan implements a deliberate subset of it (see "Scope" below).

## Scope

In: workspace and tooling; wire types and codegen with a CI drift gate; the fake ACP adapter; the host outbox, session actor and connection loop; the collector store, host WebSocket, REST (`/api/hosts`, `/api/sessions`, `/api/sessions/{id}/prompt`, `/api/sessions/{id}/events`) and the session SSE stream; the `roost` binary with `collector`, `host run` and `up`; end-to-end tests including a collector restart mid-turn.

Out (later plans): real auth (a shared development token stands in — the `hello.token` field and the bearer middleware are marked as skeleton-only), pairing and Ed25519 proof, hats, the gateway, permission/elicitation, resume/park/close, the idle reaper, adapter exit watching, the managed Node runtime, the frontend (a placeholder page only), push, `resend_complete` reconciliation logic (the frame is sent and logged; reconciliation lands with resume/park), outbox size bounds, host chunk coalescing, and the kernel's dedicated writer thread (a mutex-serialised connection stands in).

## Global Constraints

- Language: Rust edition 2024, `rust-version = "1.88"`; licence `AGPL-3.0-only`; crates are `publish = false`.
- Crate names are prefixed `roost-`; the binary is `roost`.
- `cargo fmt --all --check` (with `max_width = 120`) and `cargo clippy --workspace --all-targets -- -D warnings` must pass after every task.
- Generated files (`schema/roost-protocol.schema.json`, `web/src/generated/protocol.ts`) are committed and must match `cargo run -p roost-proto --bin gen -- --check`.
- ACP payloads are forwarded verbatim as `serde_json::Value`; never re-serialize them through typed ACP structs (ACP core §2.4).
- Every state-bearing fact from the host travels as a sequenced `session` frame through the outbox; only rejections (`error`) bypass it (ACP core §3.3).
- The collector acks a frame only after its transaction commits; ingest is idempotent on `(session_id, seq)`.
- Adapters strip `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT` from their environment and run in their own process group (ACP core §2.3).
- No global installs: all tooling comes from the flake dev shell (`nix develop`, or direnv with `use flake`).
- Commits: Conventional Commits, e.g. `feat(proto): …`.

## Review Focus

These are the inputs most likely to bite a real user that the obvious tests would not exercise; each one is pinned by a test in the task named. (Six, not five: the start-failure case was added after the spec review.)

1. **The collector goes away mid-turn** — the agent keeps producing output; after the collector returns, every chunk and exactly one `turn_ended` appear, with no duplicate seqs. (Task 8, `a_collector_restart_mid_turn_loses_nothing_and_duplicates_nothing`)
2. **A prompt is delivered twice** (retry after a lost confirmation) — the turn must not run twice. (Task 4, `a_repeated_turn_id_is_not_run_twice`)
3. **A second prompt while a turn is running, and an empty prompt** — refused with 409 / 400, never forwarded to the agent. (Task 8, `empty_prompts_and_overlapping_prompts_are_refused`; Task 4, `an_empty_prompt_is_rejected_without_starting_a_turn`)
4. **A late or duplicated `turn_ended`** for a turn that already ended — must not close the next turn. (Task 7, `turn_ended_closes_only_the_open_turn_and_a_late_duplicate_changes_nothing`)
5. **An adapter that cannot start** (missing binary, crashing on `initialize`) — the start is reported as a clear 502 and the session is marked failed, never left `starting`. (Task 4, `an_adapter_that_cannot_start_reports_start_failed_durably`; Task 8, `a_start_that_fails_on_the_host_is_reported_as_502`)
6. **The host's outbox counter is behind the collector** (outbox file lost or restored) — new frames must not reuse seqs the collector already committed. (Task 3, `fast_forward_never_moves_the_counter_backwards`; the collector reports committed seqs in `hello_ack`, Task 8)

## File structure

| Path | Responsibility |
|---|---|
| `Cargo.toml`, `rustfmt.toml` | Workspace, shared dependency versions, formatting |
| `flake.nix`, `.envrc` | Dev shell (Rust, Node, pnpm, sqlite) |
| `.github/workflows/ci.yml` | fmt, clippy, tests, codegen drift gate on Linux and macOS |
| `crates/roost-proto/src/frames.rs` | Host↔collector frames |
| `crates/roost-proto/src/rest.rs` | REST payloads |
| `crates/roost-proto/src/codegen.rs`, `src/bin/gen.rs` | Schema/TS rendering and the `gen` binary |
| `crates/roost-testkit/src/bin/roost-fake-acp.rs` | Scripted ACP agent for tests |
| `crates/roost-host/src/outbox.rs` | Durable sequenced frames |
| `crates/roost-host/src/uplink.rs` | The host's single path to the collector |
| `crates/roost-host/src/session.rs` | Session actor and adapter process |
| `crates/roost-host/src/connection.rs` | WebSocket loop, hello, resend, dispatch |
| `crates/roost-kernel/src/db.rs`, `src/auth.rs` | SQLite helpers, skeleton bearer auth |
| `crates/roost-sessions/src/store.rs` | Collector session storage and state transitions |
| `crates/roost-sessions/src/hub.rs` | Connected hosts, in-flight requests, event broadcast |
| `crates/roost-sessions/src/ws.rs` | Host WebSocket endpoint |
| `crates/roost-sessions/src/api.rs` | REST and SSE |
| `crates/roost/src/main.rs` | CLI: `collector`, `host run`, `up` |

All commands below run from the repository root inside the dev shell (`nix develop`, or `direnv allow` once). The first build downloads crates; later builds are incremental.

---

### Task 1: Workspace, dev shell, CI and wire types

**Files:**
- Create: `Cargo.toml`, `rustfmt.toml`, `flake.nix`, `.envrc`, `.gitignore`, `.github/workflows/ci.yml`
- Create: `crates/roost-proto/Cargo.toml`, `crates/roost-proto/src/{lib.rs,frames.rs,rest.rs,codegen.rs}`, `crates/roost-proto/src/bin/gen.rs`
- Test: `crates/roost-proto/tests/frames.rs`, `crates/roost-proto/tests/codegen.rs`
- Generated (committed): `schema/roost-protocol.schema.json`, `web/src/generated/protocol.ts`

**Interfaces:**
- Produces: `roost_proto::PROTOCOL_VERSION: &str` (`"1.0"`), `roost_proto::protocol_major(&str) -> Option<u32>`; `roost_proto::frames::{HostFrame, CollectorFrame, SessionBody, TurnOutcome, Indexed, AttachedSession}`; `roost_proto::rest::{StartSessionRequest, StartSessionResponse, PromptRequest, PromptResponse, EventDto, ApiError}`; `roost_proto::codegen::{render_schema() -> String, render_ts() -> String, SCHEMA_PATH, TS_PATH}`.
- Wire shapes: `HostFrame` tagged by `"type"` (snake_case): `hello`, `session`, `error`, `resend_complete`. `SessionBody` tagged by `"kind"`: `session_started`, `start_failed`, `turn_started`, `acp_update`, `turn_ended`. `CollectorFrame`: `hello_ack` (with `committed: BTreeMap<session_id, seq>`), `hello_error`, `start_session`, `prompt`, `ack`. `hello`/`hello_ack` carry `protocol_version` as `MAJOR.MINOR`.

- [ ] **Step 1: Create the workspace and tooling files**

  `Cargo.toml`

  ```toml
  [workspace]
  resolver = "3"
  members = ["crates/*"]

  [workspace.package]
  version = "0.0.0"
  edition = "2024"
  license = "AGPL-3.0-only"
  rust-version = "1.88"
  publish = false

  [workspace.dependencies]
  agent-client-protocol = "=2.2.0"
  anyhow = "1"
  axum = { version = "0.8.9", features = ["ws"] }
  clap = { version = "4", features = ["derive", "env"] }
  futures = "0.3"
  reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls", "stream"] }
  rusqlite = { version = "0.40", features = ["bundled"] }
  schemars = "1.2"
  serde = { version = "1", features = ["derive"] }
  serde_json = "1"
  thiserror = "2"
  time = { version = "0.3", features = ["formatting"] }
  tokio = { version = "1", features = ["full"] }
  tokio-stream = { version = "0.1", features = ["sync"] }
  tokio-tungstenite = "0.29"
  tokio-util = { version = "0.7", features = ["compat"] }
  tracing = "0.1"
  tracing-subscriber = { version = "0.3", features = ["env-filter"] }
  ts-rs = { version = "12", features = ["serde-json-impl"] }
  uuid = { version = "1", features = ["v7", "serde"] }
  roost-proto = { path = "crates/roost-proto" }
  roost-kernel = { path = "crates/roost-kernel" }
  roost-sessions = { path = "crates/roost-sessions" }
  roost-host = { path = "crates/roost-host" }
  ```

  `rustfmt.toml`

  ```toml
  max_width = 120
  ```

  `flake.nix` — `nix develop` creates `flake.lock` on first use; commit it

  ```nix
  {
    description = "roost: self-hosted cockpit for ACP coding agents";

    inputs = {
      nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
      flake-utils.url = "github:numtide/flake-utils";
    };

    outputs = { nixpkgs, flake-utils, ... }:
      flake-utils.lib.eachDefaultSystem (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          devShells.default = pkgs.mkShell {
            packages = with pkgs; [
              cargo
              rustc
              clippy
              rustfmt
              rust-analyzer
              nodejs_24
              pnpm
              sqlite
            ];
            RUST_SRC_PATH = "${pkgs.rustPlatform.rustLibSrc}";
          };
        });
  }
  ```

  `.envrc`

  ```
  use flake
  ```

  `.gitignore`

  ```
  /target
  /result
  .direnv/
  node_modules/
  ```

  `.github/workflows/ci.yml`

  ```yaml
  name: ci

  on:
    push:
      branches: [main]
    pull_request:

  permissions:
    contents: read

  jobs:
    rust:
      strategy:
        fail-fast: false
        matrix:
          os: [ubuntu-latest, macos-latest]
      runs-on: ${{ matrix.os }}
      steps:
        - uses: actions/checkout@v4
        - uses: dtolnay/rust-toolchain@stable
          with:
            components: clippy, rustfmt
        - uses: Swatinem/rust-cache@v2
        - run: cargo fmt --all --check
        - run: cargo clippy --workspace --all-targets -- -D warnings
        - run: cargo test --workspace
        - name: Generated files are up to date
          run: cargo run -p roost-proto --bin gen -- --check
  ```

  The workspace lists path dependencies for crates later tasks create; Cargo only resolves the ones a built crate uses, so this is fine.

  If you use direnv, run `direnv allow` now; otherwise prefix commands with `nix develop -c`.

- [ ] **Step 2: Create the proto crate manifest and the failing tests**

  `crates/roost-proto/Cargo.toml`

  ```toml
  [package]
  name = "roost-proto"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [dependencies]
  schemars.workspace = true
  serde.workspace = true
  serde_json.workspace = true
  ts-rs.workspace = true
  ```

  `crates/roost-proto/tests/frames.rs`

  ```rust
  use roost_proto::frames::{CollectorFrame, HostFrame, Indexed, SessionBody, TurnOutcome};
  use serde_json::json;

  #[test]
  fn host_frames_use_snake_case_type_tags() {
      let frame = HostFrame::Session {
          session_id: "s1".into(),
          seq: 7,
          body: SessionBody::TurnEnded {
              turn_id: "t1".into(),
              outcome: TurnOutcome::Completed,
              stop_reason: Some("end_turn".into()),
              error: None,
          },
      };
      let value = serde_json::to_value(&frame).unwrap();
      assert_eq!(
          value,
          json!({
              "type": "session",
              "session_id": "s1",
              "seq": 7,
              "body": {"kind": "turn_ended", "turn_id": "t1", "outcome": "completed", "stop_reason": "end_turn"}
          })
      );
  }

  #[test]
  fn acp_payload_round_trips_unknown_fields_verbatim() {
      let payload = json!({
          "sessionId": "a1",
          "update": {"sessionUpdate": "some_future_kind", "_meta": {"x": [1, 2]}, "novel": true}
      });
      let frame = HostFrame::Session {
          session_id: "s1".into(),
          seq: 1,
          body: SessionBody::AcpUpdate {
              indexed: Indexed::default(),
              payload: payload.clone(),
          },
      };
      let text = serde_json::to_string(&frame).unwrap();
      let back: HostFrame = serde_json::from_str(&text).unwrap();
      match back {
          HostFrame::Session {
              body: SessionBody::AcpUpdate { payload: got, .. },
              ..
          } => assert_eq!(got, payload),
          other => panic!("unexpected frame {other:?}"),
      }
  }

  #[test]
  fn unknown_frame_type_is_an_error_not_a_panic() {
      let err = serde_json::from_value::<CollectorFrame>(json!({"type": "from_the_future"}));
      assert!(err.is_err());
  }

  #[test]
  fn every_collector_frame_round_trips() {
      let frames = vec![
          CollectorFrame::HelloAck {
              protocol_version: "1.0".into(),
              collector_version: "0.0.0".into(),
              committed: [("s".to_string(), 4u64)].into_iter().collect(),
          },
          CollectorFrame::HelloError {
              code: "bad_token".into(),
              message: "no".into(),
          },
          CollectorFrame::StartSession {
              request_id: "r".into(),
              session_id: "s".into(),
              agent: "claude".into(),
              cwd: "/tmp".into(),
          },
          CollectorFrame::Prompt {
              request_id: "r".into(),
              session_id: "s".into(),
              turn_id: "t".into(),
              content: vec![json!({"type": "text", "text": "hi"})],
          },
          CollectorFrame::Ack {
              session_id: "s".into(),
              ack_seq: 3,
          },
      ];
      for f in frames {
          let back: CollectorFrame = serde_json::from_str(&serde_json::to_string(&f).unwrap()).unwrap();
          assert_eq!(back, f);
      }
  }

  #[test]
  fn protocol_major_parses_only_well_formed_versions() {
      use roost_proto::protocol_major;
      assert_eq!(protocol_major("1.0"), Some(1));
      assert_eq!(protocol_major("2.13"), Some(2));
      assert_eq!(protocol_major("1"), None);
      assert_eq!(protocol_major("x.0"), None);
      assert_eq!(protocol_major("1.x"), None);
  }
  ```

  `crates/roost-proto/tests/codegen.rs`

  ```rust
  use roost_proto::codegen::{render_schema, render_ts};

  #[test]
  fn typescript_declares_tagged_unions() {
      let ts = render_ts();
      assert!(ts.contains("export type HostFrame ="), "{ts}");
      assert!(ts.contains("\"type\": \"session\""), "{ts}");
      assert!(!ts.contains("bigint"), "u64 fields must be typed as number: {ts}");
  }

  #[test]
  fn schema_has_a_const_tag_per_variant() {
      let schema: serde_json::Value = serde_json::from_str(&render_schema()).unwrap();
      let host = &schema["$defs"]["HostFrame"];
      let text = host.to_string();
      for tag in ["hello", "session", "error", "resend_complete"] {
          assert!(
              text.contains(&format!("\"const\":\"{tag}\"")),
              "missing tag {tag}: {text}"
          );
      }
  }
  ```

- [ ] **Step 3: Run the tests to see them fail**

  Run:

  ```bash
  cargo test -p roost-proto
  ```

  Expected: compilation fails: `roost_proto` has no `frames`/`codegen` modules (there is no `src/lib.rs` yet).

- [ ] **Step 4: Implement the wire types and codegen**

  `crates/roost-proto/src/lib.rs`

  ```rust
  //! roost's own wire types: host<->collector frames and REST payloads.
  //!
  //! This crate is the single source of truth (architecture spec §5.4). JSON
  //! Schema and TypeScript are generated from these types by the `gen` binary;
  //! ACP payloads travel inside them as raw JSON and are never modelled here.

  pub mod frames;
  pub mod rest;

  /// Version of the host<->collector protocol, `MAJOR.MINOR`. Sent only in
  /// `hello` / `hello_ack`; minor versions are additive (architecture spec §5.3).
  pub const PROTOCOL_VERSION: &str = "1.0";

  /// The major part of a `MAJOR.MINOR` version string, if well formed.
  pub fn protocol_major(version: &str) -> Option<u32> {
      let (major, minor) = version.split_once('.')?;
      minor.parse::<u32>().ok()?;
      major.parse().ok()
  }
  pub mod codegen;
  ```

  `crates/roost-proto/src/frames.rs`

  ```rust
  use schemars::JsonSchema;
  use serde::{Deserialize, Serialize};
  use serde_json::Value;
  use std::collections::BTreeMap;
  use ts_rs::TS;

  /// A session that a host still has an adapter for, reported in `hello`.
  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct AttachedSession {
      pub session_id: String,
      #[ts(type = "number")]
      pub last_seq: u64,
      #[serde(default, skip_serializing_if = "Option::is_none")]
      pub open_turn_id: Option<String>,
  }

  /// How a turn ended. Exactly one `turn_ended` per accepted turn (ACP core §4.4).
  #[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema, TS)]
  #[serde(rename_all = "snake_case")]
  pub enum TurnOutcome {
      Completed,
      Cancelled,
      Failed,
      Interrupted,
  }

  /// Fields the collector may read from a session event. Closed set (ACP core §3.2).
  #[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct Indexed {
      #[serde(default, skip_serializing_if = "Option::is_none")]
      pub turn_id: Option<String>,
      #[serde(default, skip_serializing_if = "Option::is_none")]
      pub title: Option<String>,
  }

  /// The body of a sequenced, outboxed session frame.
  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  #[serde(tag = "kind", rename_all = "snake_case")]
  pub enum SessionBody {
      /// The adapter session exists. Resolves the collector's start waiter.
      SessionStarted {
          request_id: String,
          agent_session_id: String,
      },
      /// The host accepted a start but could not create the adapter session
      /// (spawn, `initialize` or `session/new` failed). Rejects the start waiter.
      StartFailed {
          request_id: String,
          code: String,
          message: String,
      },
      /// The prompt reached the adapter. Resolves the collector's prompt waiter.
      TurnStarted { request_id: String, turn_id: String },
      /// An ACP message from the adapter, verbatim in `payload`.
      AcpUpdate {
          #[serde(default)]
          indexed: Indexed,
          #[ts(type = "unknown")]
          payload: Value,
      },
      TurnEnded {
          turn_id: String,
          outcome: TurnOutcome,
          #[serde(default, skip_serializing_if = "Option::is_none")]
          stop_reason: Option<String>,
          #[serde(default, skip_serializing_if = "Option::is_none")]
          error: Option<String>,
      },
  }

  /// Host -> collector.
  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  #[serde(tag = "type", rename_all = "snake_case")]
  pub enum HostFrame {
      Hello {
          protocol_version: String,
          host_version: String,
          host_id: String,
          /// Walking skeleton only: a shared development token. Replaced by an
          /// Ed25519 proof of possession (ACP core §3.5).
          token: String,
          attached_sessions: Vec<AttachedSession>,
      },
      /// Every state-bearing fact is a sequenced frame: it goes through the host
      /// outbox and is acked (ACP core §3.3).
      Session {
          session_id: String,
          #[ts(type = "number")]
          seq: u64,
          body: SessionBody,
      },
      /// A rejected request. Not outboxed: a rejection means nothing happened,
      /// so losing it only costs the collector a timeout.
      Error {
          request_id: String,
          code: String,
          message: String,
      },
      /// Sent once per connection after the unacked outbox has been resent.
      /// The collector reconciles `hello.attached_sessions` only after this
      /// frame, so a resent `turn_ended` is never duplicated by a synthesised
      /// one (ACP core §5.2).
      ResendComplete,
  }

  /// Collector -> host.
  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  #[serde(tag = "type", rename_all = "snake_case")]
  pub enum CollectorFrame {
      HelloAck {
          protocol_version: String,
          collector_version: String,
          /// Highest committed seq per session listed in `hello`; the host
          /// fast-forwards its counters if they are lower (lost outbox).
          #[ts(type = "Record<string, number>")]
          committed: BTreeMap<String, u64>,
      },
      HelloError {
          code: String,
          message: String,
      },
      StartSession {
          request_id: String,
          session_id: String,
          agent: String,
          cwd: String,
      },
      Prompt {
          request_id: String,
          session_id: String,
          turn_id: String,
          /// ACP ContentBlocks, built by the frontend.
          #[ts(type = "unknown[]")]
          content: Vec<Value>,
      },
      Ack {
          session_id: String,
          #[ts(type = "number")]
          ack_seq: u64,
      },
  }
  ```

  `crates/roost-proto/src/rest.rs`

  ```rust
  use schemars::JsonSchema;
  use serde::{Deserialize, Serialize};
  use serde_json::Value;
  use ts_rs::TS;

  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct StartSessionRequest {
      pub host_id: String,
      pub agent: String,
      pub cwd: String,
  }

  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct StartSessionResponse {
      pub session_id: String,
  }

  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct PromptRequest {
      #[ts(type = "unknown[]")]
      pub content: Vec<Value>,
  }

  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct PromptResponse {
      pub turn_id: String,
  }

  /// One stored timeline event, as served by REST and SSE.
  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct EventDto {
      #[ts(type = "number")]
      pub event_id: i64,
      pub session_id: String,
      #[serde(default, skip_serializing_if = "Option::is_none")]
      #[ts(type = "number | undefined", optional)]
      pub host_seq: Option<u64>,
      pub kind: String,
      #[ts(type = "unknown")]
      pub body: Value,
      pub ts: String,
  }

  #[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema, TS)]
  pub struct ApiError {
      pub code: String,
      pub message: String,
  }
  ```

  `crates/roost-proto/src/codegen.rs`

  ```rust
  //! Renders the generated artefacts. The `gen` binary writes them to disk or,
  //! with `--check`, fails if the checked-in copies differ.

  use crate::{frames, rest};
  use schemars::generate::SchemaSettings;
  use ts_rs::{Config, TS};

  /// Repository-relative path of the generated JSON Schema.
  pub const SCHEMA_PATH: &str = "schema/roost-protocol.schema.json";
  /// Repository-relative path of the generated TypeScript module.
  pub const TS_PATH: &str = "web/src/generated/protocol.ts";

  const HEADER: &str = "// Generated by `cargo run -p roost-proto --bin gen`. Do not edit.\n";

  /// One JSON Schema document with every top-level type under `$defs`.
  pub fn render_schema() -> String {
      let mut generator = SchemaSettings::draft2020_12().into_generator();
      let mut defs = serde_json::Map::new();
      macro_rules! add {
          ($($t:ty),* $(,)?) => {$(
              let schema = generator.root_schema_for::<$t>();
              defs.insert(stringify!($t).rsplit("::").next().unwrap().to_string(), schema.to_value());
          )*};
      }
      add!(
          frames::HostFrame,
          frames::CollectorFrame,
          rest::StartSessionRequest,
          rest::StartSessionResponse,
          rest::PromptRequest,
          rest::PromptResponse,
          rest::EventDto,
          rest::ApiError,
      );
      let doc = serde_json::json!({
          "$schema": "https://json-schema.org/draft/2020-12/schema",
          "title": "roost protocol",
          "$defs": defs,
      });
      let mut out = serde_json::to_string_pretty(&doc).expect("schema serializes");
      out.push('\n');
      out
  }

  /// One TypeScript module exporting every roost wire type.
  pub fn render_ts() -> String {
      let cfg = Config::default();
      let mut out = String::from(HEADER);
      macro_rules! add {
          ($($t:ty),* $(,)?) => {$(
              out.push_str("\nexport ");
              out.push_str(&<$t as TS>::decl(&cfg));
              out.push('\n');
          )*};
      }
      add!(
          frames::AttachedSession,
          frames::TurnOutcome,
          frames::Indexed,
          frames::SessionBody,
          frames::HostFrame,
          frames::CollectorFrame,
          rest::StartSessionRequest,
          rest::StartSessionResponse,
          rest::PromptRequest,
          rest::PromptResponse,
          rest::EventDto,
          rest::ApiError,
      );
      out
  }
  ```

  `crates/roost-proto/src/bin/gen.rs`

  ```rust
  //! Write (or with `--check`, verify) the generated schema and TypeScript.
  //! Run from the repository root.

  use roost_proto::codegen::{SCHEMA_PATH, TS_PATH, render_schema, render_ts};
  use std::path::Path;
  use std::process::ExitCode;

  fn main() -> ExitCode {
      let check = std::env::args().any(|a| a == "--check");
      let outputs = [(SCHEMA_PATH, render_schema()), (TS_PATH, render_ts())];
      let mut stale = false;
      for (path, content) in outputs {
          let path = Path::new(path);
          if check {
              let current = std::fs::read_to_string(path).unwrap_or_default();
              if current != content {
                  eprintln!("stale generated file: {}", path.display());
                  stale = true;
              }
          } else {
              if let Some(dir) = path.parent() {
                  std::fs::create_dir_all(dir).expect("create output dir");
              }
              std::fs::write(path, content).expect("write generated file");
              println!("wrote {}", path.display());
          }
      }
      if stale {
          eprintln!("run `cargo run -p roost-proto --bin gen` and commit the result");
          ExitCode::FAILURE
      } else {
          ExitCode::SUCCESS
      }
  }
  ```

  Why `#[ts(type = "number")]` on `u64`: ts-rs maps `u64` to `bigint`, which `JSON.parse` never produces; the test guards it.

- [ ] **Step 5: Run the tests to see them pass**

  Run:

  ```bash
  cargo test -p roost-proto
  ```

  Expected: 7 tests pass (5 in `frames`, 2 in `codegen`).

- [ ] **Step 6: Generate the committed artefacts and prove the drift gate**

  Run:

  ```bash
  cargo run -p roost-proto --bin gen && cargo run -p roost-proto --bin gen -- --check; echo gate=$?
  ```

  Expected: `wrote schema/roost-protocol.schema.json`, `wrote web/src/generated/protocol.ts`, then `gate=0`.

  Then edit any doc comment in `frames.rs`, rerun only the `--check` command, and confirm it prints `stale generated file: …` and exits 1. Revert the edit (revert probe for the gate).

- [ ] **Step 7: Lint and format**

  Run:

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings
  ```

  Expected: no output, exit 0.

- [ ] **Step 8: Commit**

  ```bash
  git add Cargo.toml Cargo.lock rustfmt.toml flake.nix flake.lock .envrc .gitignore .github crates/roost-proto schema web
  git commit -m "feat(proto): add workspace, dev shell, CI and wire types with codegen"
  ```

---

### Task 2: Fake ACP adapter

**Files:**
- Create: `crates/roost-testkit/Cargo.toml`, `crates/roost-testkit/src/lib.rs`, `crates/roost-testkit/src/bin/roost-fake-acp.rs`
- Test: `crates/roost-testkit/tests/fake_acp.rs`

**Interfaces:**
- Produces: binary `roost-fake-acp` (ACP agent over stdio); `roost_testkit::{FakeScript { chunks: Vec<String>, chunk_delay_ms: u64 }, SCRIPT_ENV}`. `FakeScript::default()` streams `"Hello"`, `" world"`.
- Behaviour: answers `initialize`; `session/new` returns session id `fake-session-1`; `session/prompt` sends one `agent_message_chunk` `session/update` per chunk (after `chunk_delay_ms` each) and then returns `stopReason: end_turn`.

- [ ] **Step 1: Write the manifest (runtime dependencies only for now) and the failing test**

  `crates/roost-testkit/Cargo.toml` — later tasks add a `[dev-dependencies]` section

  ```toml
  [package]
  name = "roost-testkit"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [[bin]]
  name = "roost-fake-acp"
  path = "src/bin/roost-fake-acp.rs"

  [dependencies]
  agent-client-protocol.workspace = true
  serde.workspace = true
  serde_json.workspace = true
  tokio.workspace = true
  ```

  `crates/roost-testkit/tests/fake_acp.rs`

  ```rust
  //! The fake adapter speaks ACP over stdio like a real one.

  use serde_json::{Value, json};
  use std::io::{BufRead, BufReader, Write};
  use std::process::{Command, Stdio};

  fn exchange(script: Option<&str>, requests: &[Value]) -> Vec<Value> {
      let mut cmd = Command::new(env!("CARGO_BIN_EXE_roost-fake-acp"));
      cmd.stdin(Stdio::piped()).stdout(Stdio::piped());
      if let Some(s) = script {
          cmd.env(roost_testkit::SCRIPT_ENV, s);
      }
      let mut child = cmd.spawn().unwrap();
      let mut stdin = child.stdin.take().unwrap();
      for r in requests {
          writeln!(stdin, "{r}").unwrap();
      }
      let reader = BufReader::new(child.stdout.take().unwrap());
      let mut out = Vec::new();
      for line in reader.lines() {
          let msg: Value = serde_json::from_str(&line.unwrap()).unwrap();
          let done = msg["id"] == json!(3);
          out.push(msg);
          if done {
              break;
          }
      }
      drop(stdin);
      child.kill().ok();
      child.wait().ok();
      out
  }

  fn session_requests() -> Vec<Value> {
      vec![
          json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{}}}),
          json!({"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/tmp","mcpServers":[]}}),
          json!({"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"fake-session-1","prompt":[{"type":"text","text":"hi"}]}}),
      ]
  }

  #[test]
  fn default_script_streams_two_chunks_then_ends_the_turn() {
      let out = exchange(None, &session_requests());
      let chunks: Vec<&str> = out
          .iter()
          .filter(|m| m["method"] == "session/update")
          .filter_map(|m| m["params"]["update"]["content"]["text"].as_str())
          .collect();
      assert_eq!(chunks, ["Hello", " world"]);
      assert_eq!(out.last().unwrap()["result"]["stopReason"], "end_turn");
  }

  #[test]
  fn script_env_controls_the_chunks() {
      let out = exchange(Some(r#"{"chunks":["x"]}"#), &session_requests());
      let updates = out.iter().filter(|m| m["method"] == "session/update").count();
      assert_eq!(updates, 1);
  }
  ```

- [ ] **Step 2: Run the test to see it fail**

  Run:

  ```bash
  cargo test -p roost-testkit --test fake_acp
  ```

  Expected: compilation fails: `roost_testkit::SCRIPT_ENV` and the `roost-fake-acp` binary do not exist.

- [ ] **Step 3: Implement the fake adapter**

  `crates/roost-testkit/src/lib.rs`

  ```rust
  //! Test support for roost: the fake ACP adapter binary and shared helpers.

  use serde::{Deserialize, Serialize};

  /// Behaviour of `roost-fake-acp`, passed as JSON in `ROOST_FAKE_ACP_SCRIPT`.
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct FakeScript {
      /// Text chunks streamed as `agent_message_chunk` updates for every prompt.
      pub chunks: Vec<String>,
      /// Delay before each chunk, in milliseconds.
      #[serde(default)]
      pub chunk_delay_ms: u64,
  }

  impl Default for FakeScript {
      fn default() -> Self {
          Self {
              chunks: vec!["Hello".into(), " world".into()],
              chunk_delay_ms: 0,
          }
      }
  }

  /// Environment variable carrying the script.
  pub const SCRIPT_ENV: &str = "ROOST_FAKE_ACP_SCRIPT";
  ```

  `crates/roost-testkit/src/bin/roost-fake-acp.rs`

  ```rust
  //! A scripted ACP agent for tests. Speaks ACP over stdio via the
  //! `agent-client-protocol` crate, so the host is tested against the same
  //! wire format real adapters use.

  use agent_client_protocol::schema::v1::{
      AgentCapabilities, ContentBlock, ContentChunk, InitializeRequest, InitializeResponse, NewSessionRequest,
      NewSessionResponse, PromptRequest, PromptResponse, SessionNotification, SessionUpdate, StopReason, TextContent,
  };
  use agent_client_protocol::{Agent, Stdio};
  use roost_testkit::{FakeScript, SCRIPT_ENV};
  use std::time::Duration;

  #[tokio::main]
  async fn main() -> agent_client_protocol::Result<()> {
      let script: FakeScript = std::env::var(SCRIPT_ENV)
          .ok()
          .map(|s| serde_json::from_str(&s).expect("valid fake script JSON"))
          .unwrap_or_default();

      Agent
          .builder()
          .name("roost-fake-acp")
          .on_receive_request(
              async move |req: InitializeRequest, responder, _cx| {
                  responder
                      .respond(InitializeResponse::new(req.protocol_version).agent_capabilities(AgentCapabilities::new()))
              },
              agent_client_protocol::on_receive_request!(),
          )
          .on_receive_request(
              async move |_req: NewSessionRequest, responder, _cx| {
                  responder.respond(NewSessionResponse::new("fake-session-1"))
              },
              agent_client_protocol::on_receive_request!(),
          )
          .on_receive_request(
              {
                  let script = script.clone();
                  async move |req: PromptRequest, responder, cx| {
                      let script = script.clone();
                      let cx2 = cx.clone();
                      cx.spawn(async move {
                          for chunk in script.chunks {
                              tokio::time::sleep(Duration::from_millis(script.chunk_delay_ms)).await;
                              cx2.send_notification(SessionNotification::new(
                                  req.session_id.clone(),
                                  SessionUpdate::AgentMessageChunk(ContentChunk::new(ContentBlock::Text(
                                      TextContent::new(chunk),
                                  ))),
                              ))?;
                          }
                          responder.respond(PromptResponse::new(StopReason::EndTurn))
                      })
                  }
              },
              agent_client_protocol::on_receive_request!(),
          )
          .connect_to(Stdio::new())
          .await
  }
  ```

  The prompt handler does its work inside `cx.spawn`, so the connection keeps reading while chunks are being sent (a handler that awaited inline would block cancel notifications in later plans).

- [ ] **Step 4: Run the test to see it pass**

  Run:

  ```bash
  cargo test -p roost-testkit --test fake_acp
  ```

  Expected: 2 tests pass.

- [ ] **Step 5: Lint, format, commit**

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings
  git add crates/roost-testkit Cargo.lock
  git commit -m "test(testkit): add scripted fake ACP adapter"
  ```

---

### Task 3: Host outbox

**Files:**
- Create: `crates/roost-host/Cargo.toml`, `crates/roost-host/src/lib.rs`, `crates/roost-host/src/outbox.rs`
- Test: `crates/roost-host/tests/outbox.rs`

**Interfaces:**
- Consumes: `roost_proto::frames::{HostFrame, SessionBody}`.
- Produces: `roost_host::outbox::Outbox` with `open(&Path) -> Result<Outbox>`, `open_in_memory() -> Result<Outbox>`, `enqueue(&mut self, session_id: &str, body: SessionBody) -> Result<HostFrame>` (assigns the next seq, starting at 1, persists), `pending(&self) -> Result<Vec<HostFrame>>` (insertion order), `ack(&mut self, session_id: &str, ack_seq: u64) -> Result<usize>` (deletes seq ≤ ack_seq), `fast_forward(&mut self, session_id: &str, seq: u64) -> Result<()>` (never lowers), `last_seq(&self, session_id: &str) -> Result<u64>`.

- [ ] **Step 1: Write the manifest, a minimal lib and the failing tests**

  `crates/roost-host/Cargo.toml` — all runtime dependencies of the finished crate; unused ones compile fine

  ```toml
  [package]
  name = "roost-host"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [dependencies]
  agent-client-protocol.workspace = true
  anyhow.workspace = true
  futures.workspace = true
  roost-proto.workspace = true
  rusqlite.workspace = true
  serde_json.workspace = true
  tokio.workspace = true
  tokio-tungstenite.workspace = true
  tokio-util.workspace = true
  tracing.workspace = true

  [dev-dependencies]
  tempfile = "3"
  ```

  `crates/roost-host/src/lib.rs` — grows in Tasks 4 and 5

  ```rust
  //! The roost host: runs ACP adapters for one machine and relays their
  //! sessions to the collector (ACP core spec §2).

  pub mod outbox;
  ```

  `crates/roost-host/tests/outbox.rs`

  ```rust
  use roost_host::outbox::Outbox;
  use roost_proto::frames::{HostFrame, Indexed, SessionBody};
  use serde_json::json;

  fn update(n: u32) -> SessionBody {
      SessionBody::AcpUpdate {
          indexed: Indexed::default(),
          payload: json!({ "n": n }),
      }
  }

  fn seq_of(frame: &HostFrame) -> u64 {
      match frame {
          HostFrame::Session { seq, .. } => *seq,
          other => panic!("not a session frame: {other:?}"),
      }
  }

  #[test]
  fn seqs_are_per_session_and_start_at_one() {
      let mut ob = Outbox::open_in_memory().unwrap();
      assert_eq!(seq_of(&ob.enqueue("a", update(1)).unwrap()), 1);
      assert_eq!(seq_of(&ob.enqueue("a", update(2)).unwrap()), 2);
      assert_eq!(seq_of(&ob.enqueue("b", update(1)).unwrap()), 1);
  }

  #[test]
  fn ack_removes_only_frames_up_to_the_acked_seq() {
      let mut ob = Outbox::open_in_memory().unwrap();
      for n in 1..=3 {
          ob.enqueue("a", update(n)).unwrap();
      }
      ob.enqueue("b", update(1)).unwrap();
      assert_eq!(ob.ack("a", 2).unwrap(), 2);
      let left: Vec<u64> = ob.pending().unwrap().iter().map(seq_of).collect();
      assert_eq!(left, vec![3, 1]);
  }

  #[test]
  fn seq_numbering_survives_reopen_even_after_everything_is_acked() {
      let dir = tempfile::tempdir().unwrap();
      let path = dir.path().join("outbox.db");
      {
          let mut ob = Outbox::open(&path).unwrap();
          ob.enqueue("a", update(1)).unwrap();
          ob.enqueue("a", update(2)).unwrap();
          ob.ack("a", 2).unwrap();
      }
      let mut ob = Outbox::open(&path).unwrap();
      assert!(ob.pending().unwrap().is_empty());
      assert_eq!(ob.last_seq("a").unwrap(), 2);
      assert_eq!(seq_of(&ob.enqueue("a", update(3)).unwrap()), 3);
  }

  #[test]
  fn unacked_frames_survive_reopen() {
      let dir = tempfile::tempdir().unwrap();
      let path = dir.path().join("outbox.db");
      {
          let mut ob = Outbox::open(&path).unwrap();
          ob.enqueue("a", update(1)).unwrap();
      }
      let ob = Outbox::open(&path).unwrap();
      assert_eq!(ob.pending().unwrap().len(), 1);
  }

  #[test]
  fn fast_forward_never_moves_the_counter_backwards() {
      let mut ob = Outbox::open_in_memory().unwrap();
      ob.enqueue("a", update(1)).unwrap();
      ob.fast_forward("a", 10).unwrap();
      assert_eq!(seq_of(&ob.enqueue("a", update(2)).unwrap()), 11);
      ob.fast_forward("a", 3).unwrap();
      assert_eq!(seq_of(&ob.enqueue("a", update(3)).unwrap()), 12);
  }
  ```

- [ ] **Step 2: Run the tests to see them fail**

  Run:

  ```bash
  cargo test -p roost-host --test outbox
  ```

  Expected: compilation fails: module `outbox` has no file (`src/outbox.rs` does not exist).

- [ ] **Step 3: Implement the outbox**

  `crates/roost-host/src/outbox.rs`

  ```rust
  //! On-disk outbox for sequenced session frames (ACP core §5.5).
  //!
  //! Every session frame is written here before it is sent, and deleted only
  //! when the collector acks it. Sequence numbers are persisted, so a restarted
  //! host continues a session's numbering instead of reusing seqs.

  use anyhow::Result;
  use roost_proto::frames::{HostFrame, SessionBody};
  use rusqlite::{Connection, OptionalExtension, params};
  use std::path::Path;

  pub struct Outbox {
      conn: Connection,
  }

  impl Outbox {
      pub fn open(path: &Path) -> Result<Self> {
          Self::init(Connection::open(path)?)
      }

      pub fn open_in_memory() -> Result<Self> {
          Self::init(Connection::open_in_memory()?)
      }

      fn init(conn: Connection) -> Result<Self> {
          conn.pragma_update(None, "journal_mode", "WAL")?;
          conn.execute_batch(
              "CREATE TABLE IF NOT EXISTS outbox (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   session_id TEXT NOT NULL,
                   seq INTEGER NOT NULL,
                   frame TEXT NOT NULL,
                   UNIQUE(session_id, seq));
               CREATE TABLE IF NOT EXISTS seqs (
                   session_id TEXT PRIMARY KEY,
                   last_seq INTEGER NOT NULL);",
          )?;
          Ok(Self { conn })
      }

      /// Assign the next seq for `session_id`, persist the frame, return it.
      pub fn enqueue(&mut self, session_id: &str, body: SessionBody) -> Result<HostFrame> {
          let tx = self.conn.transaction()?;
          let last: i64 = tx
              .query_row("SELECT last_seq FROM seqs WHERE session_id = ?1", [session_id], |r| {
                  r.get(0)
              })
              .optional()?
              .unwrap_or(0);
          let seq = last as u64 + 1;
          let frame = HostFrame::Session {
              session_id: session_id.to_string(),
              seq,
              body,
          };
          tx.execute(
              "INSERT INTO seqs(session_id, last_seq) VALUES (?1, ?2)
               ON CONFLICT(session_id) DO UPDATE SET last_seq = excluded.last_seq",
              params![session_id, seq as i64],
          )?;
          tx.execute(
              "INSERT INTO outbox(session_id, seq, frame) VALUES (?1, ?2, ?3)",
              params![session_id, seq as i64, serde_json::to_string(&frame)?],
          )?;
          tx.commit()?;
          Ok(frame)
      }

      /// Every unacked frame, in insertion order (which is per-session seq order).
      pub fn pending(&self) -> Result<Vec<HostFrame>> {
          let mut stmt = self.conn.prepare("SELECT frame FROM outbox ORDER BY id")?;
          let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
          let mut out = Vec::new();
          for row in rows {
              out.push(serde_json::from_str(&row?)?);
          }
          Ok(out)
      }

      /// Delete every frame of `session_id` with seq <= `ack_seq`.
      pub fn ack(&mut self, session_id: &str, ack_seq: u64) -> Result<usize> {
          Ok(self.conn.execute(
              "DELETE FROM outbox WHERE session_id = ?1 AND seq <= ?2",
              params![session_id, ack_seq as i64],
          )?)
      }

      /// Raise the counter for `session_id` to at least `seq`. Used when the
      /// collector has committed more than this outbox remembers (outbox file
      /// lost or restored from an old backup), so new frames never reuse seqs.
      pub fn fast_forward(&mut self, session_id: &str, seq: u64) -> Result<()> {
          self.conn.execute(
              "INSERT INTO seqs(session_id, last_seq) VALUES (?1, ?2)
               ON CONFLICT(session_id) DO UPDATE SET last_seq = MAX(last_seq, excluded.last_seq)",
              params![session_id, seq as i64],
          )?;
          Ok(())
      }

      /// Highest seq ever assigned for `session_id` (0 if none).
      pub fn last_seq(&self, session_id: &str) -> Result<u64> {
          let last: Option<i64> = self
              .conn
              .query_row("SELECT last_seq FROM seqs WHERE session_id = ?1", [session_id], |r| {
                  r.get(0)
              })
              .optional()?;
          Ok(last.unwrap_or(0) as u64)
      }
  }
  ```

  SQLite has no unsigned 64-bit type, so seqs are stored as `i64` and converted at the boundary (they never approach 2^63).

- [ ] **Step 4: Run the tests to see them pass**

  Run:

  ```bash
  cargo test -p roost-host --test outbox
  ```

  Expected: 5 tests pass.

- [ ] **Step 5: Revert probe**

  Temporarily change the `seqs` upsert in `enqueue` to `DO NOTHING` and confirm `seqs_are_per_session_and_start_at_one` fails; change `MAX(last_seq, excluded.last_seq)` in `fast_forward` to `excluded.last_seq` and confirm `fast_forward_never_moves_the_counter_backwards` fails. Restore both.

- [ ] **Step 6: Lint, format, commit**

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings
  git add crates/roost-host Cargo.lock
  git commit -m "feat(host): add durable outbox for sequenced session frames"
  ```

---

### Task 4: Host uplink and session actor

**Files:**
- Create: `crates/roost-host/src/uplink.rs`, `crates/roost-host/src/session.rs`
- Modify: `crates/roost-host/src/lib.rs`, `crates/roost-testkit/Cargo.toml` (add dev-dependencies)
- Test: `crates/roost-testkit/tests/host_session.rs` (lives in the testkit so it can use the fake adapter binary)

**Interfaces:**
- Consumes: `Outbox` (Task 3); binary `roost-fake-acp` (Task 2).
- Produces: `roost_host::uplink::Uplink` (`Clone`) with `new(Outbox) -> (Uplink, mpsc::UnboundedReceiver<HostFrame>)`, `emit(&self, session_id: &str, body: SessionBody) -> Result<()>` (synchronous, persists then wakes the sender), `reply(&self, HostFrame)`, `pending()`, `ack(session_id, ack_seq)`, `fast_forward(session_id, seq)`, `last_seq(session_id)`, `async changed()`.
- Produces: `roost_host::session::{AgentCommand { program, args, env }, AgentCommand::parse(&str) -> Option<AgentCommand>, SessionCmd::Prompt { request_id, turn_id, content: Vec<Value> }, start(uplink, request_id, session_id, agent, cwd) -> mpsc::UnboundedSender<SessionCmd>, NESTING_VARS}`.
- Behaviour: the actor spawns the adapter, runs `initialize` and `session/new`, emits `session_started`; per prompt emits `turn_started`, forwards every `session/update` verbatim as `acp_update` (in arrival order), then emits exactly one `turn_ended`. A repeated `turn_id` is ignored. An empty or unparseable prompt is answered with `error{code: invalid}` and no turn starts. If the adapter cannot be started or `initialize`/`session/new` fail, the actor emits `start_failed` (durable, through the outbox).

- [ ] **Step 1: Add the testkit dev-dependencies and write the failing tests**

  `crates/roost-testkit/Cargo.toml`

  ```toml
  [package]
  name = "roost-testkit"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [[bin]]
  name = "roost-fake-acp"
  path = "src/bin/roost-fake-acp.rs"

  [dependencies]
  agent-client-protocol.workspace = true
  serde.workspace = true
  serde_json.workspace = true
  tokio.workspace = true

  [dev-dependencies]
  roost-host.workspace = true
  roost-proto.workspace = true
  ```

  `crates/roost-testkit/tests/host_session.rs`

  ```rust
  //! The host's session actor against the fake adapter, without a collector:
  //! the outbox must receive session_started, then per turn turn_started, the
  //! adapter's updates verbatim, and exactly one turn_ended, in that order.

  use roost_host::outbox::Outbox;
  use roost_host::session::{self, AgentCommand, SessionCmd};
  use roost_host::uplink::Uplink;
  use roost_proto::frames::{HostFrame, SessionBody, TurnOutcome};
  use serde_json::json;
  use std::time::Duration;

  fn kinds(frames: &[HostFrame]) -> Vec<String> {
      frames
          .iter()
          .map(|f| match f {
              HostFrame::Session { body, .. } => match body {
                  SessionBody::SessionStarted { .. } => "session_started".to_string(),
                  SessionBody::StartFailed { .. } => "start_failed".to_string(),
                  SessionBody::TurnStarted { .. } => "turn_started".to_string(),
                  SessionBody::AcpUpdate { payload, .. } => {
                      format!(
                          "update:{}",
                          payload["update"]["content"]["text"].as_str().unwrap_or("?")
                      )
                  }
                  SessionBody::TurnEnded { .. } => "turn_ended".to_string(),
              },
              other => format!("{other:?}"),
          })
          .collect()
  }

  async fn wait_until(uplink: &Uplink, pred: impl Fn(&[HostFrame]) -> bool) -> Vec<HostFrame> {
      let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
      loop {
          let frames = uplink.pending().unwrap();
          if pred(&frames) {
              return frames;
          }
          assert!(
              tokio::time::Instant::now() < deadline,
              "timed out; outbox: {:?}",
              kinds(&frames)
          );
          tokio::time::sleep(Duration::from_millis(20)).await;
      }
  }

  #[tokio::test]
  async fn a_turn_produces_ordered_outboxed_facts() {
      let (uplink, _replies) = Uplink::new(Outbox::open_in_memory().unwrap());
      let fake = AgentCommand::parse(env!("CARGO_BIN_EXE_roost-fake-acp")).unwrap();
      let tx = session::start(uplink.clone(), "r0".into(), "s1".into(), fake, std::env::temp_dir());
      wait_until(&uplink, |f| !f.is_empty()).await;

      tx.send(SessionCmd::Prompt {
          request_id: "r1".into(),
          turn_id: "t1".into(),
          content: vec![json!({"type":"text","text":"hi"})],
      })
      .unwrap();
      let frames = wait_until(&uplink, |f| kinds(f).contains(&"turn_ended".to_string())).await;
      assert_eq!(
          kinds(&frames),
          [
              "session_started",
              "turn_started",
              "update:Hello",
              "update: world",
              "turn_ended"
          ]
      );
      match frames.last().unwrap() {
          HostFrame::Session {
              body: SessionBody::TurnEnded {
                  outcome, stop_reason, ..
              },
              ..
          } => {
              assert_eq!(*outcome, TurnOutcome::Completed);
              assert_eq!(stop_reason.as_deref(), Some("end_turn"));
          }
          other => panic!("{other:?}"),
      }
  }

  #[tokio::test]
  async fn a_repeated_turn_id_is_not_run_twice() {
      let (uplink, _replies) = Uplink::new(Outbox::open_in_memory().unwrap());
      let fake = AgentCommand::parse(env!("CARGO_BIN_EXE_roost-fake-acp")).unwrap();
      let tx = session::start(uplink.clone(), "r0".into(), "s1".into(), fake, std::env::temp_dir());
      let prompt = || SessionCmd::Prompt {
          request_id: "r1".into(),
          turn_id: "t1".into(),
          content: vec![json!({"type":"text","text":"hi"})],
      };
      tx.send(prompt()).unwrap();
      tx.send(prompt()).unwrap();
      wait_until(&uplink, |f| kinds(f).contains(&"turn_ended".to_string())).await;
      tokio::time::sleep(Duration::from_millis(300)).await;
      let ends = kinds(&uplink.pending().unwrap())
          .iter()
          .filter(|k| *k == "turn_ended")
          .count();
      assert_eq!(ends, 1);
  }

  #[tokio::test]
  async fn an_empty_prompt_is_rejected_without_starting_a_turn() {
      let (uplink, mut replies) = Uplink::new(Outbox::open_in_memory().unwrap());
      let fake = AgentCommand::parse(env!("CARGO_BIN_EXE_roost-fake-acp")).unwrap();
      let tx = session::start(uplink.clone(), "r0".into(), "s1".into(), fake, std::env::temp_dir());
      wait_until(&uplink, |f| !f.is_empty()).await;
      tx.send(SessionCmd::Prompt {
          request_id: "r1".into(),
          turn_id: "t1".into(),
          content: vec![],
      })
      .unwrap();
      let reply = tokio::time::timeout(Duration::from_secs(5), replies.recv())
          .await
          .unwrap()
          .unwrap();
      assert!(
          matches!(reply, HostFrame::Error { ref code, .. } if code == "invalid"),
          "{reply:?}"
      );
      assert_eq!(kinds(&uplink.pending().unwrap()), ["session_started"]);
  }

  #[tokio::test]
  async fn an_adapter_that_cannot_start_reports_start_failed_durably() {
      let (uplink, _replies) = Uplink::new(Outbox::open_in_memory().unwrap());
      let broken = AgentCommand::parse("/nonexistent/roost-test-adapter").unwrap();
      let _tx = session::start(uplink.clone(), "r0".into(), "s1".into(), broken, std::env::temp_dir());
      let frames = wait_until(&uplink, |f| !f.is_empty()).await;
      assert_eq!(kinds(&frames), ["start_failed"]);
  }
  ```

- [ ] **Step 2: Run the tests to see them fail**

  Run:

  ```bash
  cargo test -p roost-testkit --test host_session
  ```

  Expected: compilation fails: `roost_host::session` and `roost_host::uplink` do not exist.

- [ ] **Step 3: Implement the uplink and the session actor**

  `crates/roost-host/src/lib.rs`

  ```rust
  //! The roost host: runs ACP adapters for one machine and relays their
  //! sessions to the collector (ACP core spec §2).

  pub mod outbox;
  pub mod session;
  pub mod uplink;

  pub use session::AgentCommand;
  ```

  `crates/roost-host/src/uplink.rs`

  ```rust
  //! The host's single path to the collector.
  //!
  //! Sequenced session frames go through the outbox (`emit`); correlated
  //! responses to collector requests go straight to the socket (`reply`). The
  //! connection task drains both.

  use crate::outbox::Outbox;
  use anyhow::Result;
  use roost_proto::frames::{HostFrame, SessionBody};
  use std::sync::{Arc, Mutex};
  use tokio::sync::{Notify, mpsc};

  #[derive(Clone)]
  pub struct Uplink {
      outbox: Arc<Mutex<Outbox>>,
      outbox_changed: Arc<Notify>,
      replies: mpsc::UnboundedSender<HostFrame>,
  }

  impl Uplink {
      pub fn new(outbox: Outbox) -> (Self, mpsc::UnboundedReceiver<HostFrame>) {
          let (tx, rx) = mpsc::unbounded_channel();
          let uplink = Self {
              outbox: Arc::new(Mutex::new(outbox)),
              outbox_changed: Arc::new(Notify::new()),
              replies: tx,
          };
          (uplink, rx)
      }

      /// Persist a session frame and wake the sender. Synchronous on purpose:
      /// callers rely on frames being ordered exactly as `emit` is called.
      pub fn emit(&self, session_id: &str, body: SessionBody) -> Result<()> {
          self.outbox.lock().expect("outbox lock").enqueue(session_id, body)?;
          self.outbox_changed.notify_one();
          Ok(())
      }

      /// Send a correlated response. Dropped responses are recovered by the
      /// collector's request timeout, so a closed channel is not an error.
      pub fn reply(&self, frame: HostFrame) {
          let _ = self.replies.send(frame);
      }

      pub fn pending(&self) -> Result<Vec<HostFrame>> {
          self.outbox.lock().expect("outbox lock").pending()
      }

      pub fn ack(&self, session_id: &str, ack_seq: u64) -> Result<()> {
          self.outbox.lock().expect("outbox lock").ack(session_id, ack_seq)?;
          Ok(())
      }

      pub fn fast_forward(&self, session_id: &str, seq: u64) -> Result<()> {
          self.outbox.lock().expect("outbox lock").fast_forward(session_id, seq)
      }

      pub fn last_seq(&self, session_id: &str) -> Result<u64> {
          self.outbox.lock().expect("outbox lock").last_seq(session_id)
      }

      pub async fn changed(&self) {
          self.outbox_changed.notified().await;
      }
  }
  ```

  `crates/roost-host/src/session.rs`

  ```rust
  //! One session actor per attached session (ACP core §2.2), each owning one
  //! adapter process (umbrella §6.9).

  use crate::uplink::Uplink;
  use agent_client_protocol::schema::ProtocolVersion;
  use agent_client_protocol::schema::v1::{ContentBlock, InitializeRequest, NewSessionRequest, PromptRequest, SessionId};
  use agent_client_protocol::{Agent, ByteStreams, Client, ConnectionTo, UntypedMessage};
  use roost_proto::frames::{HostFrame, Indexed, SessionBody, TurnOutcome};
  use serde_json::Value;
  use std::path::PathBuf;
  use std::process::Stdio;
  use std::sync::Arc;
  use std::sync::atomic::{AtomicBool, Ordering};
  use tokio::sync::mpsc;
  use tokio_util::compat::{TokioAsyncReadCompatExt, TokioAsyncWriteCompatExt};

  /// Environment variables that make an agent refuse to start or double-report
  /// when roost itself runs inside an agent session (ACP core §2.3).
  pub const NESTING_VARS: &[&str] = &["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"];

  /// How to launch an agent's ACP adapter.
  #[derive(Debug, Clone)]
  pub struct AgentCommand {
      pub program: String,
      pub args: Vec<String>,
      pub env: Vec<(String, String)>,
  }

  impl AgentCommand {
      /// Parse `"program arg1 arg2"` (whitespace-separated, no quoting).
      pub fn parse(command: &str) -> Option<Self> {
          let mut parts = command.split_whitespace().map(str::to_string);
          let program = parts.next()?;
          Some(Self {
              program,
              args: parts.collect(),
              env: Vec::new(),
          })
      }
  }

  /// Messages from the connection task to a session actor.
  #[derive(Debug)]
  pub enum SessionCmd {
      Prompt {
          request_id: String,
          turn_id: String,
          content: Vec<Value>,
      },
  }

  /// Spawn a session actor. Returns the actor's mailbox.
  pub fn start(
      uplink: Uplink,
      request_id: String,
      session_id: String,
      agent: AgentCommand,
      cwd: PathBuf,
  ) -> mpsc::UnboundedSender<SessionCmd> {
      let (tx, rx) = mpsc::unbounded_channel();
      tokio::spawn(async move {
          let started = Arc::new(AtomicBool::new(false));
          let result = run(
              uplink.clone(),
              &request_id,
              &session_id,
              agent,
              cwd,
              rx,
              started.clone(),
          )
          .await;
          if let Err(err) = result {
              tracing::warn!(%session_id, error = %err, "session actor ended with an error");
              // Before `session_started`, the failure is the start's outcome and
              // must reach the collector durably (ACP core §3.2).
              if !started.load(Ordering::SeqCst) {
                  let body = SessionBody::StartFailed {
                      request_id,
                      code: "start_failed".into(),
                      message: err.to_string(),
                  };
                  if let Err(e) = uplink.emit(&session_id, body) {
                      tracing::error!(error = %e, "failed to persist start_failed");
                  }
              }
          }
      });
      tx
  }

  async fn run(
      uplink: Uplink,
      request_id: &str,
      session_id: &str,
      agent: AgentCommand,
      cwd: PathBuf,
      mut commands: mpsc::UnboundedReceiver<SessionCmd>,
      started: Arc<AtomicBool>,
  ) -> anyhow::Result<()> {
      let mut command = tokio::process::Command::new(&agent.program);
      command
          .args(&agent.args)
          .envs(agent.env.iter().cloned())
          .current_dir(&cwd)
          .stdin(Stdio::piped())
          .stdout(Stdio::piped())
          .stderr(Stdio::inherit())
          .process_group(0)
          .kill_on_drop(true);
      for var in NESTING_VARS {
          command.env_remove(var);
      }
      let mut child = command.spawn()?;
      let stdin = child.stdin.take().expect("piped stdin");
      let stdout = child.stdout.take().expect("piped stdout");
      let transport = ByteStreams::new(stdin.compat_write(), stdout.compat());

      let updates_uplink = uplink.clone();
      let updates_session = session_id.to_string();
      let request_id = request_id.to_string();
      let session_id = session_id.to_string();

      Client
          .builder()
          .name("roost-host")
          // Raw handler: ACP payloads are forwarded verbatim, including update
          // kinds this build does not know (ACP core §2.4). Emitting inside the
          // handler keeps frames in arrival order.
          .on_receive_notification(
              async move |msg: UntypedMessage, _cx| {
                  if msg.method == "session/update" {
                      let body = SessionBody::AcpUpdate {
                          indexed: Indexed::default(),
                          payload: msg.params,
                      };
                      if let Err(err) = updates_uplink.emit(&updates_session, body) {
                          tracing::error!(error = %err, "failed to persist a session update");
                      }
                  }
                  Ok(())
              },
              agent_client_protocol::on_receive_notification!(),
          )
          .connect_with(transport, async move |conn: ConnectionTo<Agent>| {
              conn.send_request(InitializeRequest::new(ProtocolVersion::V1))
                  .block_task()
                  .await?;
              let created = conn
                  .send_request(NewSessionRequest::new(cwd.clone()))
                  .block_task()
                  .await?;
              let agent_session: SessionId = created.session_id;
              uplink
                  .emit(
                      &session_id,
                      SessionBody::SessionStarted {
                          request_id: request_id.clone(),
                          agent_session_id: agent_session.to_string(),
                      },
                  )
                  .map_err(|e| agent_client_protocol::Error::into_internal_error(&*e))?;
              started.store(true, Ordering::SeqCst);

              // Prompts are deduplicated by turn_id: a retried delivery after a
              // lost acknowledgement must never run the same turn twice.
              let mut seen_turns = std::collections::HashSet::new();
              while let Some(cmd) = commands.recv().await {
                  match cmd {
                      SessionCmd::Prompt {
                          request_id,
                          turn_id,
                          content,
                      } => {
                          if !seen_turns.insert(turn_id.clone()) {
                              tracing::info!(%turn_id, "ignoring duplicate prompt delivery");
                              continue;
                          }
                          run_turn(
                              &conn,
                              &uplink,
                              &session_id,
                              &agent_session,
                              request_id,
                              turn_id,
                              content,
                          )
                          .await;
                      }
                  }
              }
              Ok(())
          })
          .await?;
      Ok(())
  }

  async fn run_turn(
      conn: &ConnectionTo<Agent>,
      uplink: &Uplink,
      session_id: &str,
      agent_session: &SessionId,
      request_id: String,
      turn_id: String,
      content: Vec<Value>,
  ) {
      let blocks: Result<Vec<ContentBlock>, _> = content.into_iter().map(serde_json::from_value).collect();
      let blocks = match blocks {
          Ok(blocks) if !blocks.is_empty() => blocks,
          Ok(_) => {
              uplink.reply(HostFrame::Error {
                  request_id,
                  code: "invalid".into(),
                  message: "empty prompt".into(),
              });
              return;
          }
          Err(err) => {
              uplink.reply(HostFrame::Error {
                  request_id,
                  code: "invalid".into(),
                  message: err.to_string(),
              });
              return;
          }
      };
      if let Err(err) = uplink.emit(
          session_id,
          SessionBody::TurnStarted {
              request_id,
              turn_id: turn_id.clone(),
          },
      ) {
          tracing::error!(error = %err, "failed to persist turn_started");
          return;
      }
      let result = conn
          .send_request(PromptRequest::new(agent_session.clone(), blocks))
          .block_task()
          .await;
      let body = match result {
          Ok(response) => SessionBody::TurnEnded {
              turn_id,
              outcome: TurnOutcome::Completed,
              stop_reason: serde_json::to_value(response.stop_reason)
                  .ok()
                  .and_then(|v| v.as_str().map(str::to_string)),
              error: None,
          },
          Err(err) => SessionBody::TurnEnded {
              turn_id,
              outcome: TurnOutcome::Failed,
              stop_reason: None,
              error: Some(err.to_string()),
          },
      };
      if let Err(err) = uplink.emit(session_id, body) {
          tracing::error!(error = %err, "failed to persist turn_ended");
      }
  }
  ```

  Two details carry the spec's ordering guarantee (ACP core §2.2, §2.4):

  1. The notification handler is registered for `UntypedMessage`, so update kinds this build does not know are still forwarded; a typed handler would drop them on a parse failure.
  2. `Uplink::emit` is synchronous and is called inside that handler, so each update is persisted before the crate delivers the prompt's response, and `turn_ended` is always last.

- [ ] **Step 4: Run the tests to see them pass**

  Run:

  ```bash
  cargo test -p roost-testkit --test host_session
  ```

  Expected: 4 tests pass.

- [ ] **Step 5: Revert probe**

  Delete the `if !seen_turns.insert(...)` block in `session.rs` and confirm `a_repeated_turn_id_is_not_run_twice` fails; restore it.

- [ ] **Step 6: Lint, format, commit**

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings
  git add crates/roost-host crates/roost-testkit Cargo.lock
  git commit -m "feat(host): add session actor driving one ACP adapter per session"
  ```

---

### Task 5: Host connection loop

**Files:**
- Create: `crates/roost-host/src/connection.rs`
- Modify: `crates/roost-host/src/lib.rs`

**Interfaces:**
- Consumes: `Uplink`, `session::start` (Task 4).
- Produces: `roost_host::{HostConfig, run}`. `HostConfig::new(collector_url, host_id, token, data_dir)` with public fields `agents: HashMap<String, AgentCommand>`, `reconnect_min`, `reconnect_max`, `ping_interval`, `read_timeout`. `run(cfg) -> Result<()>` never returns under normal operation.
- Protocol behaviour: send `hello` (with attached sessions and their last seqs) → expect `hello_ack` and fast-forward counters from `committed` → resend every unacked outbox frame → send `resend_complete` → loop: send new outbox frames as they appear, send replies, ping every 15 s, and fail the connection if nothing arrives within 45 s. `start_session` spawns an actor (idempotent per session id); `prompt` goes to the actor or is answered `error{code: not_attached}`; `ack` trims the outbox. Unknown frames are logged and ignored. Reconnects with exponential backoff; session actors survive a dropped connection.

- [ ] **Step 1: Implement the connection loop**

  `crates/roost-host/src/lib.rs`

  ```rust
  //! The roost host: runs ACP adapters for one machine and relays their
  //! sessions to the collector (ACP core spec §2).

  pub mod connection;
  pub mod outbox;
  pub mod session;
  pub mod uplink;

  pub use connection::{HostConfig, run};
  pub use session::AgentCommand;
  ```

  `crates/roost-host/src/connection.rs`

  ```rust
  //! The host's connection to the collector (ACP core §2, §5).
  //!
  //! Adapters belong to the host process, not to this connection: a dropped
  //! socket only ends `connect_once`; session actors keep running and keep
  //! writing to the outbox, which is resent on the next connection.

  use crate::outbox::Outbox;
  use crate::session::{self, AgentCommand, SessionCmd};
  use crate::uplink::Uplink;
  use anyhow::{Context, Result, bail};
  use futures::{SinkExt, StreamExt};
  use roost_proto::PROTOCOL_VERSION;
  use roost_proto::frames::{AttachedSession, CollectorFrame, HostFrame};
  use std::collections::HashMap;
  use std::path::PathBuf;
  use std::sync::{Arc, Mutex};
  use std::time::Duration;
  use tokio::sync::mpsc;
  use tokio_tungstenite::tungstenite::Message;

  #[derive(Debug, Clone)]
  pub struct HostConfig {
      /// e.g. `ws://127.0.0.1:7117/api/hosts/ws`
      pub collector_url: String,
      pub host_id: String,
      /// Walking skeleton: shared development token (ACP core §3.5 replaces it).
      pub token: String,
      pub data_dir: PathBuf,
      pub agents: HashMap<String, AgentCommand>,
      pub reconnect_min: Duration,
      pub reconnect_max: Duration,
      pub ping_interval: Duration,
      pub read_timeout: Duration,
  }

  impl HostConfig {
      pub fn new(
          collector_url: impl Into<String>,
          host_id: impl Into<String>,
          token: impl Into<String>,
          data_dir: PathBuf,
      ) -> Self {
          Self {
              collector_url: collector_url.into(),
              host_id: host_id.into(),
              token: token.into(),
              data_dir,
              agents: HashMap::new(),
              reconnect_min: Duration::from_millis(500),
              reconnect_max: Duration::from_secs(30),
              ping_interval: Duration::from_secs(15),
              read_timeout: Duration::from_secs(45),
          }
      }
  }

  type Sessions = Arc<Mutex<HashMap<String, mpsc::UnboundedSender<SessionCmd>>>>;

  /// Run the host until the process exits. Reconnects with exponential backoff.
  pub async fn run(cfg: HostConfig) -> Result<()> {
      std::fs::create_dir_all(&cfg.data_dir)?;
      let outbox = Outbox::open(&cfg.data_dir.join("outbox.db"))?;
      let (uplink, mut replies) = Uplink::new(outbox);
      let sessions: Sessions = Arc::new(Mutex::new(HashMap::new()));
      let mut backoff = cfg.reconnect_min;
      loop {
          match connect_once(&cfg, &uplink, &sessions, &mut replies).await {
              Ok(()) => backoff = cfg.reconnect_min,
              Err(err) => tracing::warn!(error = %err, "collector connection ended"),
          }
          tokio::time::sleep(backoff).await;
          backoff = (backoff * 2).min(cfg.reconnect_max);
      }
  }

  async fn connect_once(
      cfg: &HostConfig,
      uplink: &Uplink,
      sessions: &Sessions,
      replies: &mut mpsc::UnboundedReceiver<HostFrame>,
  ) -> Result<()> {
      let (ws, _) = tokio_tungstenite::connect_async(&cfg.collector_url)
          .await
          .context("connect to collector")?;
      let (mut sink, mut stream) = ws.split();

      let attached = {
          let ids: Vec<String> = sessions.lock().expect("sessions lock").keys().cloned().collect();
          let mut out = Vec::new();
          for session_id in ids {
              let last_seq = uplink.last_seq(&session_id)?;
              out.push(AttachedSession {
                  session_id,
                  last_seq,
                  open_turn_id: None,
              });
          }
          out
      };
      send(
          &mut sink,
          &HostFrame::Hello {
              protocol_version: PROTOCOL_VERSION.into(),
              host_version: env!("CARGO_PKG_VERSION").into(),
              host_id: cfg.host_id.clone(),
              token: cfg.token.clone(),
              attached_sessions: attached,
          },
      )
      .await?;

      match tokio::time::timeout(cfg.read_timeout, stream.next()).await {
          Ok(Some(Ok(Message::Text(text)))) => match serde_json::from_str::<CollectorFrame>(&text)? {
              CollectorFrame::HelloAck { committed, .. } => {
                  for (session_id, seq) in committed {
                      uplink.fast_forward(&session_id, seq)?;
                  }
              }
              CollectorFrame::HelloError { code, message } => bail!("hello rejected: {code}: {message}"),
              other => bail!("expected hello_ack, got {other:?}"),
          },
          other => bail!("no hello_ack: {other:?}"),
      }
      tracing::info!(collector = %cfg.collector_url, "connected to collector");

      // Resend everything unacked, then tell the collector we are done.
      let mut sent: HashMap<String, u64> = HashMap::new();
      send_pending(&mut sink, uplink, &mut sent).await?;
      send(&mut sink, &HostFrame::ResendComplete).await?;

      let mut ping = tokio::time::interval(cfg.ping_interval);
      ping.tick().await;
      loop {
          tokio::select! {
              _ = uplink.changed() => send_pending(&mut sink, uplink, &mut sent).await?,
              Some(frame) = replies.recv() => send(&mut sink, &frame).await?,
              _ = ping.tick() => sink.send(Message::Ping(Default::default())).await?,
              msg = tokio::time::timeout(cfg.read_timeout, stream.next()) => match msg {
                  Err(_) => bail!("no frame from collector within {:?}", cfg.read_timeout),
                  Ok(None) => bail!("collector closed the connection"),
                  Ok(Some(Err(err))) => return Err(err.into()),
                  Ok(Some(Ok(Message::Text(text)))) => match serde_json::from_str::<CollectorFrame>(&text) {
                      Ok(frame) => handle(cfg, uplink, sessions, frame)?,
                      Err(err) => tracing::warn!(error = %err, "ignoring unknown or invalid frame"),
                  },
                  Ok(Some(Ok(Message::Close(_)))) => bail!("collector closed the connection"),
                  Ok(Some(Ok(_))) => {} // ping/pong/binary: liveness only
              },
          }
      }
  }

  fn handle(cfg: &HostConfig, uplink: &Uplink, sessions: &Sessions, frame: CollectorFrame) -> Result<()> {
      match frame {
          CollectorFrame::StartSession {
              request_id,
              session_id,
              agent,
              cwd,
          } => {
              let Some(command) = cfg.agents.get(&agent).cloned() else {
                  uplink.reply(HostFrame::Error {
                      request_id,
                      code: "unknown_agent".into(),
                      message: format!("agent {agent} is not configured on this host"),
                  });
                  return Ok(());
              };
              let mut map = sessions.lock().expect("sessions lock");
              if map.contains_key(&session_id) {
                  // Idempotent: a retried start for an attached session is a no-op;
                  // its session_started fact is already in the outbox.
                  return Ok(());
              }
              let tx = session::start(
                  uplink.clone(),
                  request_id,
                  session_id.clone(),
                  command,
                  PathBuf::from(cwd),
              );
              map.insert(session_id, tx);
          }
          CollectorFrame::Prompt {
              request_id,
              session_id,
              turn_id,
              content,
          } => {
              let tx = sessions.lock().expect("sessions lock").get(&session_id).cloned();
              match tx {
                  Some(tx)
                      if tx
                          .send(SessionCmd::Prompt {
                              request_id: request_id.clone(),
                              turn_id,
                              content,
                          })
                          .is_ok() => {}
                  _ => uplink.reply(HostFrame::Error {
                      request_id,
                      code: "not_attached".into(),
                      message: "session is not attached on this host".into(),
                  }),
              }
          }
          CollectorFrame::Ack { session_id, ack_seq } => uplink.ack(&session_id, ack_seq)?,
          CollectorFrame::HelloAck { .. } | CollectorFrame::HelloError { .. } => {}
      }
      Ok(())
  }

  async fn send_pending<S>(sink: &mut S, uplink: &Uplink, sent: &mut HashMap<String, u64>) -> Result<()>
  where
      S: futures::Sink<Message> + Unpin,
      S::Error: std::error::Error + Send + Sync + 'static,
  {
      for frame in uplink.pending()? {
          if let HostFrame::Session { session_id, seq, .. } = &frame {
              if sent.get(session_id).is_some_and(|last| seq <= last) {
                  continue;
              }
              sent.insert(session_id.clone(), *seq);
          }
          send(sink, &frame).await?;
      }
      Ok(())
  }

  async fn send<S>(sink: &mut S, frame: &HostFrame) -> Result<()>
  where
      S: futures::Sink<Message> + Unpin,
      S::Error: std::error::Error + Send + Sync + 'static,
  {
      sink.send(Message::text(serde_json::to_string(frame)?)).await?;
      Ok(())
  }
  ```

  This task has no test of its own: its behaviour is observable only against a collector and is covered by the end-to-end tests in Task 8 (all four of them fail if this loop is wrong). Keep the file exactly as above so Task 8's expectations hold.

- [ ] **Step 2: Build, lint, format**

  Run:

  ```bash
  cargo build -p roost-host && cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test -p roost-host -p roost-testkit
  ```

  Expected: builds cleanly; all existing tests still pass.

- [ ] **Step 3: Commit**

  ```bash
  git add crates/roost-host Cargo.lock
  git commit -m "feat(host): add collector connection with hello, resend and keepalive"
  ```

---

### Task 6: Kernel: database helpers and skeleton auth

**Files:**
- Create: `crates/roost-kernel/Cargo.toml`, `crates/roost-kernel/src/{lib.rs,db.rs,auth.rs}` (unit tests inline)

**Interfaces:**
- Produces: `roost_kernel::db::{open(&Path) -> Result<Connection>, open_in_memory() -> Result<Connection>, migrate(&mut Connection, &[&str]) -> Result<()>}` (WAL, foreign keys, 5 s busy timeout; `user_version`-based; refuses a newer database).
- Produces: `roost_kernel::auth::{DevToken(Arc<str>), DevToken::new, DevToken::matches(&str) -> bool (constant time), require_bearer}` — axum middleware requiring `Authorization: Bearer <token>`.

- [ ] **Step 1: Write the crate with its inline tests**

  `crates/roost-kernel/Cargo.toml`

  ```toml
  [package]
  name = "roost-kernel"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [dependencies]
  anyhow.workspace = true
  axum.workspace = true
  rusqlite.workspace = true
  ```

  `crates/roost-kernel/src/lib.rs`

  ```rust
  //! Shared collector foundations (kernel spec): storage and request auth.
  //! The walking skeleton carries only what the session path needs.

  pub mod auth;
  pub mod db;
  ```

  `crates/roost-kernel/src/db.rs`

  ```rust
  //! SQLite helpers (kernel spec §1).

  use anyhow::{Result, bail};
  use rusqlite::Connection;
  use std::path::Path;

  /// Open (or create) a database with the pragmas every roost database uses.
  pub fn open(path: &Path) -> Result<Connection> {
      configure(Connection::open(path)?)
  }

  pub fn open_in_memory() -> Result<Connection> {
      configure(Connection::open_in_memory()?)
  }

  fn configure(conn: Connection) -> Result<Connection> {
      conn.pragma_update(None, "journal_mode", "WAL")?;
      conn.pragma_update(None, "foreign_keys", "ON")?;
      conn.busy_timeout(std::time::Duration::from_secs(5))?;
      Ok(conn)
  }

  /// Apply `migrations[user_version..]` in order, each in its own transaction.
  /// Refuses to run against a database newer than this binary.
  pub fn migrate(conn: &mut Connection, migrations: &[&str]) -> Result<()> {
      let current: usize = conn.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))? as usize;
      if current > migrations.len() {
          bail!(
              "database schema version {current} is newer than this binary supports ({})",
              migrations.len()
          );
      }
      for (index, sql) in migrations.iter().enumerate().skip(current) {
          let tx = conn.transaction()?;
          tx.execute_batch(sql)?;
          tx.pragma_update(None, "user_version", (index + 1) as i64)?;
          tx.commit()?;
      }
      Ok(())
  }

  #[cfg(test)]
  mod tests {
      use super::*;

      #[test]
      fn migrations_apply_once_and_record_the_version() {
          let mut conn = open_in_memory().unwrap();
          let steps = ["CREATE TABLE a (x INTEGER);", "CREATE TABLE b (y INTEGER);"];
          migrate(&mut conn, &steps).unwrap();
          migrate(&mut conn, &steps).unwrap();
          let v: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0)).unwrap();
          assert_eq!(v, 2);
      }

      #[test]
      fn a_newer_database_is_refused() {
          let mut conn = open_in_memory().unwrap();
          migrate(
              &mut conn,
              &["CREATE TABLE a (x INTEGER);", "CREATE TABLE b (y INTEGER);"],
          )
          .unwrap();
          let err = migrate(&mut conn, &["CREATE TABLE a (x INTEGER);"]).unwrap_err();
          assert!(err.to_string().contains("newer"), "{err}");
      }
  }
  ```

  `crates/roost-kernel/src/auth.rs`

  ```rust
  //! Walking-skeleton request auth: a shared development bearer token.
  //! Replaced by operator sessions and passkeys (kernel spec §3).

  use axum::extract::{Request, State};
  use axum::http::{StatusCode, header};
  use axum::middleware::Next;
  use axum::response::{IntoResponse, Response};
  use std::sync::Arc;

  #[derive(Clone)]
  pub struct DevToken(pub Arc<str>);

  impl DevToken {
      pub fn new(token: impl Into<String>) -> Self {
          Self(Arc::from(token.into()))
      }

      /// Constant-time comparison.
      pub fn matches(&self, candidate: &str) -> bool {
          let a = self.0.as_bytes();
          let b = candidate.as_bytes();
          a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
      }
  }

  /// Axum middleware: require `Authorization: Bearer <dev token>`.
  pub async fn require_bearer(State(token): State<DevToken>, req: Request, next: Next) -> Response {
      let presented = req
          .headers()
          .get(header::AUTHORIZATION)
          .and_then(|v| v.to_str().ok())
          .and_then(|v| v.strip_prefix("Bearer "));
      match presented {
          Some(p) if token.matches(p) => next.run(req).await,
          _ => (StatusCode::UNAUTHORIZED, "missing or invalid bearer token").into_response(),
      }
  }

  #[cfg(test)]
  mod tests {
      use super::DevToken;

      #[test]
      fn token_comparison_rejects_prefixes_and_different_lengths() {
          let t = DevToken::new("secret");
          assert!(t.matches("secret"));
          assert!(!t.matches("secre"));
          assert!(!t.matches("secret2"));
          assert!(!t.matches(""));
      }
  }
  ```

  (TDD note: the tests live next to the code as `#[cfg(test)]` modules. Write the `tests` modules first, run `cargo test -p roost-kernel` to see the missing functions fail to compile, then add the functions.)

- [ ] **Step 2: Run the tests**

  Run:

  ```bash
  cargo test -p roost-kernel
  ```

  Expected: 3 tests pass.

- [ ] **Step 3: Lint, format, commit**

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings
  git add crates/roost-kernel Cargo.lock
  git commit -m "feat(kernel): add SQLite helpers and skeleton bearer auth"
  ```

---

### Task 7: Collector session store

**Files:**
- Create: `crates/roost-sessions/Cargo.toml`, `crates/roost-sessions/src/lib.rs` (minimal), `crates/roost-sessions/src/store.rs`
- Test: `crates/roost-sessions/tests/store.rs`

**Interfaces:**
- Consumes: `roost_kernel::db`; `roost_proto::frames::SessionBody`, `roost_proto::rest::EventDto`.
- Produces: `roost_sessions::store::{Store, SessionRow { id, host_id, agent, cwd, lifecycle, activity, open_turn_id }}`; `Store::open(&Path)`, `open_in_memory()`, `create_session(id, host_id, agent, cwd)` (lifecycle `starting`), `mark_failed(id, reason)`, `session(id) -> Result<Option<SessionRow>>`, `open_turn(session_id, turn_id, content: &[Value]) -> Result<bool>` (only if `active` and no open turn), `abandon_turn(session_id, turn_id)`, `committed_seq(session_id) -> Result<u64>`, `ingest(session_id, seq, &SessionBody) -> Result<Vec<EventDto>>` (idempotent; applies state transitions; returns created events, empty for a duplicate), `events(session_id, after: i64, limit: u32) -> Result<Vec<EventDto>>`.
- State rules: `session_started` → `active/idle`; `start_failed` → `failed` (reason stored); `turn_started` → `running` and inserts a collector `user_turn` event (host_seq NULL) from the stored prompt, right after the `turn_started` event; `turn_ended` closes the turn only if it is the open one.

- [ ] **Step 1: Write the manifest, a minimal lib and the failing tests**

  `crates/roost-sessions/Cargo.toml` — all dependencies of the finished crate

  ```toml
  [package]
  name = "roost-sessions"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [dependencies]
  anyhow.workspace = true
  axum.workspace = true
  futures.workspace = true
  roost-kernel.workspace = true
  roost-proto.workspace = true
  rusqlite.workspace = true
  serde.workspace = true
  serde_json.workspace = true
  time.workspace = true
  tokio.workspace = true
  tokio-stream.workspace = true
  tokio-util.workspace = true
  tracing.workspace = true
  uuid.workspace = true
  ```

  `crates/roost-sessions/src/lib.rs` — replaced in Task 8

  ```rust
  //! Collector-side session module (ACP core spec §4, §8, §9).

  pub mod store;
  ```

  `crates/roost-sessions/tests/store.rs`

  ```rust
  use roost_proto::frames::{Indexed, SessionBody, TurnOutcome};
  use roost_sessions::store::Store;
  use serde_json::json;

  fn started(store: &Store) {
      store.create_session("s1", "h1", "fake", "/tmp").unwrap();
      store
          .ingest(
              "s1",
              1,
              &SessionBody::SessionStarted {
                  request_id: "r0".into(),
                  agent_session_id: "a1".into(),
              },
          )
          .unwrap();
  }

  fn update(n: u32) -> SessionBody {
      SessionBody::AcpUpdate {
          indexed: Indexed::default(),
          payload: json!({ "n": n }),
      }
  }

  fn ended(turn: &str) -> SessionBody {
      SessionBody::TurnEnded {
          turn_id: turn.into(),
          outcome: TurnOutcome::Completed,
          stop_reason: None,
          error: None,
      }
  }

  #[test]
  fn session_started_activates_the_session() {
      let store = Store::open_in_memory().unwrap();
      started(&store);
      let s = store.session("s1").unwrap().unwrap();
      assert_eq!((s.lifecycle.as_str(), s.activity.as_deref()), ("active", Some("idle")));
  }

  #[test]
  fn ingest_is_idempotent_on_session_and_seq() {
      let store = Store::open_in_memory().unwrap();
      started(&store);
      assert_eq!(store.ingest("s1", 2, &update(1)).unwrap().len(), 1);
      assert!(store.ingest("s1", 2, &update(1)).unwrap().is_empty());
      assert_eq!(store.events("s1", 0, 100).unwrap().len(), 2);
      assert_eq!(store.committed_seq("s1").unwrap(), 2);
  }

  #[test]
  fn only_one_turn_can_be_open() {
      let store = Store::open_in_memory().unwrap();
      started(&store);
      assert!(
          store
              .open_turn("s1", "t1", &[json!({"type": "text", "text": "a"})])
              .unwrap()
      );
      assert!(
          !store
              .open_turn("s1", "t2", &[json!({"type": "text", "text": "b"})])
              .unwrap()
      );
  }

  #[test]
  fn a_turn_cannot_open_on_a_session_that_is_not_active() {
      let store = Store::open_in_memory().unwrap();
      store.create_session("s1", "h1", "fake", "/tmp").unwrap();
      assert!(
          !store
              .open_turn("s1", "t1", &[json!({"type": "text", "text": "a"})])
              .unwrap()
      );
  }

  #[test]
  fn turn_started_records_the_user_turn_before_the_turns_updates() {
      let store = Store::open_in_memory().unwrap();
      started(&store);
      store
          .open_turn("s1", "t1", &[json!({"type": "text", "text": "hi"})])
          .unwrap();
      let created = store
          .ingest(
              "s1",
              2,
              &SessionBody::TurnStarted {
                  request_id: "r1".into(),
                  turn_id: "t1".into(),
              },
          )
          .unwrap();
      let kinds: Vec<&str> = created.iter().map(|e| e.kind.as_str()).collect();
      assert_eq!(kinds, ["turn_started", "user_turn"]);
      assert_eq!(created[1].body["content"][0]["text"], "hi");
  }

  #[test]
  fn turn_ended_closes_only_the_open_turn_and_a_late_duplicate_changes_nothing() {
      let store = Store::open_in_memory().unwrap();
      started(&store);
      store
          .open_turn("s1", "t1", &[json!({"type": "text", "text": "a"})])
          .unwrap();
      store.ingest("s1", 2, &ended("t1")).unwrap();
      assert!(
          store
              .open_turn("s1", "t2", &[json!({"type": "text", "text": "b"})])
              .unwrap()
      );
      // A late turn_ended for t1 (e.g. resent after a reconnect) must not close t2.
      store.ingest("s1", 3, &ended("t1")).unwrap();
      assert_eq!(
          store.session("s1").unwrap().unwrap().open_turn_id.as_deref(),
          Some("t2")
      );
  }

  #[test]
  fn abandon_turn_frees_the_session_for_the_next_prompt() {
      let store = Store::open_in_memory().unwrap();
      started(&store);
      store
          .open_turn("s1", "t1", &[json!({"type": "text", "text": "a"})])
          .unwrap();
      store.abandon_turn("s1", "t1").unwrap();
      assert!(
          store
              .open_turn("s1", "t2", &[json!({"type": "text", "text": "b"})])
              .unwrap()
      );
  }
  ```

- [ ] **Step 2: Run the tests to see them fail**

  Run:

  ```bash
  cargo test -p roost-sessions --test store
  ```

  Expected: compilation fails: `src/store.rs` does not exist.

- [ ] **Step 3: Implement the store**

  `crates/roost-sessions/src/store.rs`

  ```rust
  //! Collector session storage (ACP core §8). SQLite; writes are serialised by
  //! the connection mutex (kernel §1's writer thread replaces it later).

  use anyhow::Result;
  use roost_proto::frames::SessionBody;
  use roost_proto::rest::EventDto;
  use rusqlite::{Connection, OptionalExtension, params};
  use serde_json::Value;
  use std::path::Path;
  use std::sync::Mutex;

  const MIGRATIONS: &[&str] = &["
      CREATE TABLE sessions (
          id TEXT PRIMARY KEY,
          host_id TEXT NOT NULL,
          agent TEXT NOT NULL,
          cwd TEXT NOT NULL,
          agent_session_id TEXT,
          lifecycle TEXT NOT NULL,
          activity TEXT,
          failure_reason TEXT,
          open_turn_id TEXT,
          created_at TEXT NOT NULL,
          last_event_at TEXT NOT NULL);
      CREATE TABLE turns (
          turn_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL REFERENCES sessions(id),
          content TEXT NOT NULL,
          outcome TEXT,
          created_at TEXT NOT NULL);
      CREATE TABLE events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL REFERENCES sessions(id),
          host_seq INTEGER,
          kind TEXT NOT NULL,
          body TEXT NOT NULL,
          ts TEXT NOT NULL,
          UNIQUE(session_id, host_seq));
  "];

  #[derive(Debug, Clone, PartialEq)]
  pub struct SessionRow {
      pub id: String,
      pub host_id: String,
      pub agent: String,
      pub cwd: String,
      pub lifecycle: String,
      pub activity: Option<String>,
      pub open_turn_id: Option<String>,
  }

  pub struct Store {
      conn: Mutex<Connection>,
  }

  fn now() -> String {
      time::OffsetDateTime::now_utc()
          .format(&time::format_description::well_known::Rfc3339)
          .expect("RFC 3339 formatting of the current time")
  }

  impl Store {
      pub fn open(path: &Path) -> Result<Self> {
          Self::init(roost_kernel::db::open(path)?)
      }

      pub fn open_in_memory() -> Result<Self> {
          Self::init(roost_kernel::db::open_in_memory()?)
      }

      fn init(mut conn: Connection) -> Result<Self> {
          roost_kernel::db::migrate(&mut conn, MIGRATIONS)?;
          Ok(Self { conn: Mutex::new(conn) })
      }

      fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
          self.conn.lock().expect("store lock")
      }

      pub fn create_session(&self, id: &str, host_id: &str, agent: &str, cwd: &str) -> Result<()> {
          let ts = now();
          self.conn().execute(
              "INSERT INTO sessions(id, host_id, agent, cwd, lifecycle, created_at, last_event_at)
               VALUES (?1, ?2, ?3, ?4, 'starting', ?5, ?5)",
              params![id, host_id, agent, cwd, ts],
          )?;
          Ok(())
      }

      pub fn mark_failed(&self, id: &str, reason: &str) -> Result<()> {
          self.conn().execute(
              "UPDATE sessions SET lifecycle = 'failed', failure_reason = ?2 WHERE id = ?1",
              params![id, reason],
          )?;
          Ok(())
      }

      pub fn session(&self, id: &str) -> Result<Option<SessionRow>> {
          Ok(self
              .conn()
              .query_row(
                  "SELECT id, host_id, agent, cwd, lifecycle, activity, open_turn_id FROM sessions WHERE id = ?1",
                  [id],
                  |r| {
                      Ok(SessionRow {
                          id: r.get(0)?,
                          host_id: r.get(1)?,
                          agent: r.get(2)?,
                          cwd: r.get(3)?,
                          lifecycle: r.get(4)?,
                          activity: r.get(5)?,
                          open_turn_id: r.get(6)?,
                      })
                  },
              )
              .optional()?)
      }

      /// Open a turn if the session is active and has none open. Returns false
      /// when a turn is already in flight (ACP core §4.4: one turn at a time).
      pub fn open_turn(&self, session_id: &str, turn_id: &str, content: &[Value]) -> Result<bool> {
          let mut conn = self.conn();
          let tx = conn.transaction()?;
          let changed = tx.execute(
              "UPDATE sessions SET open_turn_id = ?2
               WHERE id = ?1 AND lifecycle = 'active' AND open_turn_id IS NULL",
              params![session_id, turn_id],
          )?;
          if changed == 1 {
              tx.execute(
                  "INSERT INTO turns(turn_id, session_id, content, created_at) VALUES (?1, ?2, ?3, ?4)",
                  params![turn_id, session_id, serde_json::to_string(content)?, now()],
              )?;
          }
          tx.commit()?;
          Ok(changed == 1)
      }

      /// Undo `open_turn` after the host rejected the prompt.
      pub fn abandon_turn(&self, session_id: &str, turn_id: &str) -> Result<()> {
          let mut conn = self.conn();
          let tx = conn.transaction()?;
          tx.execute(
              "UPDATE sessions SET open_turn_id = NULL WHERE id = ?1 AND open_turn_id = ?2",
              params![session_id, turn_id],
          )?;
          tx.execute("DELETE FROM turns WHERE turn_id = ?1", [turn_id])?;
          tx.commit()?;
          Ok(())
      }

      /// Highest committed host seq for a session (0 if none).
      pub fn committed_seq(&self, session_id: &str) -> Result<u64> {
          let v: Option<i64> = self.conn().query_row(
              "SELECT MAX(host_seq) FROM events WHERE session_id = ?1",
              [session_id],
              |r| r.get(0),
          )?;
          Ok(v.unwrap_or(0) as u64)
      }

      /// Ingest one sequenced host frame. Idempotent on (session_id, seq).
      /// Returns the events it created (empty for a duplicate), in order.
      pub fn ingest(&self, session_id: &str, seq: u64, body: &SessionBody) -> Result<Vec<EventDto>> {
          let mut conn = self.conn();
          let tx = conn.transaction()?;
          let ts = now();
          let kind = body_kind(body);
          let inserted = tx.execute(
              "INSERT INTO events(session_id, host_seq, kind, body, ts) VALUES (?1, ?2, ?3, ?4, ?5)
               ON CONFLICT(session_id, host_seq) DO NOTHING",
              params![session_id, seq as i64, kind, serde_json::to_string(body)?, ts],
          )?;
          if inserted == 0 {
              tx.commit()?;
              return Ok(Vec::new());
          }
          let mut created = vec![EventDto {
              event_id: tx.last_insert_rowid(),
              session_id: session_id.to_string(),
              host_seq: Some(seq),
              kind: kind.to_string(),
              body: serde_json::to_value(body)?,
              ts: ts.clone(),
          }];
          match body {
              SessionBody::SessionStarted { agent_session_id, .. } => {
                  tx.execute(
                      "UPDATE sessions SET lifecycle = 'active', activity = 'idle', agent_session_id = ?2
                       WHERE id = ?1 AND lifecycle IN ('starting', 'failed')",
                      params![session_id, agent_session_id],
                  )?;
              }
              SessionBody::StartFailed { code, .. } => {
                  tx.execute(
                      "UPDATE sessions SET lifecycle = 'failed', failure_reason = ?2 WHERE id = ?1 AND lifecycle = 'starting'",
                      params![session_id, code],
                  )?;
              }
              SessionBody::TurnStarted { turn_id, .. } => {
                  tx.execute("UPDATE sessions SET activity = 'running' WHERE id = ?1", [session_id])?;
                  // The user's turn is recorded only once the adapter has it
                  // (ACP core §4.4), in seq order before the turn's updates.
                  let content: Option<String> = tx
                      .query_row("SELECT content FROM turns WHERE turn_id = ?1", [turn_id], |r| r.get(0))
                      .optional()?;
                  if let Some(content) = content {
                      let body =
                          serde_json::json!({ "turn_id": turn_id, "content": serde_json::from_str::<Value>(&content)? });
                      tx.execute(
                          "INSERT INTO events(session_id, host_seq, kind, body, ts) VALUES (?1, NULL, 'user_turn', ?2, ?3)",
                          params![session_id, body.to_string(), ts],
                      )?;
                      created.push(EventDto {
                          event_id: tx.last_insert_rowid(),
                          session_id: session_id.to_string(),
                          host_seq: None,
                          kind: "user_turn".into(),
                          body,
                          ts: ts.clone(),
                      });
                  }
              }
              SessionBody::TurnEnded { turn_id, outcome, .. } => {
                  // Applied only to the open turn; a late duplicate for an
                  // already-ended turn is stored but changes nothing.
                  tx.execute(
                      "UPDATE sessions SET open_turn_id = NULL, activity = 'idle' WHERE id = ?1 AND open_turn_id = ?2",
                      params![session_id, turn_id],
                  )?;
                  tx.execute(
                      "UPDATE turns SET outcome = ?2 WHERE turn_id = ?1 AND outcome IS NULL",
                      params![turn_id, serde_json::to_value(outcome)?.as_str().unwrap_or_default()],
                  )?;
              }
              SessionBody::AcpUpdate { .. } => {}
          }
          tx.execute(
              "UPDATE sessions SET last_event_at = ?2 WHERE id = ?1",
              params![session_id, ts],
          )?;
          tx.commit()?;
          Ok(created)
      }

      /// Events of one session with `event_id > after`, oldest first.
      pub fn events(&self, session_id: &str, after: i64, limit: u32) -> Result<Vec<EventDto>> {
          let conn = self.conn();
          let mut stmt = conn.prepare(
              "SELECT event_id, host_seq, kind, body, ts FROM events
               WHERE session_id = ?1 AND event_id > ?2 ORDER BY event_id LIMIT ?3",
          )?;
          let rows = stmt.query_map(params![session_id, after, limit], |r| {
              Ok((
                  r.get::<_, i64>(0)?,
                  r.get::<_, Option<i64>>(1)?,
                  r.get::<_, String>(2)?,
                  r.get::<_, String>(3)?,
                  r.get::<_, String>(4)?,
              ))
          })?;
          let mut out = Vec::new();
          for row in rows {
              let (event_id, host_seq, kind, body, ts) = row?;
              out.push(EventDto {
                  event_id,
                  session_id: session_id.to_string(),
                  host_seq: host_seq.map(|s| s as u64),
                  kind,
                  body: serde_json::from_str(&body)?,
                  ts,
              });
          }
          Ok(out)
      }
  }

  fn body_kind(body: &SessionBody) -> &'static str {
      match body {
          SessionBody::SessionStarted { .. } => "session_started",
          SessionBody::StartFailed { .. } => "start_failed",
          SessionBody::TurnStarted { .. } => "turn_started",
          SessionBody::AcpUpdate { .. } => "acp_update",
          SessionBody::TurnEnded { .. } => "turn_ended",
      }
  }

  #[cfg(test)]
  mod tests {
      #[test]
      fn timestamps_are_rfc3339_utc() {
          let ts = super::now();
          assert!(ts.ends_with('Z') && ts.as_bytes()[10] == b'T', "{ts}");
      }
  }
  ```

- [ ] **Step 4: Run the tests to see them pass**

  Run:

  ```bash
  cargo test -p roost-sessions
  ```

  Expected: 8 tests pass (7 integration, 1 unit).

- [ ] **Step 5: Revert probe**

  Remove `AND open_turn_id = ?2` from the `turn_ended` update and confirm `turn_ended_closes_only_the_open_turn_and_a_late_duplicate_changes_nothing` fails; remove `ON CONFLICT(session_id, host_seq) DO NOTHING` and confirm `ingest_is_idempotent_on_session_and_seq` fails. Restore both.

- [ ] **Step 6: Lint, format, commit**

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings
  git add crates/roost-sessions Cargo.lock
  git commit -m "feat(sessions): add collector session store with idempotent ingest"
  ```

---

### Task 8: Collector hub, host WebSocket, REST and SSE — end to end

**Files:**
- Create: `crates/roost-sessions/src/{hub.rs,ws.rs,api.rs}`
- Modify: `crates/roost-sessions/src/lib.rs`, `crates/roost-testkit/Cargo.toml` (more dev-dependencies)
- Test: `crates/roost-testkit/tests/e2e.rs`

**Interfaces:**
- Consumes: everything above.
- Produces: `roost_sessions::{AppState { store, hub, token, shutdown }, AppState::new(Store, DevToken), router(AppState) -> Router, serve(TcpListener, AppState) -> io::Result<()>}` (serves until `state.shutdown` is cancelled).
- Produces: `roost_sessions::hub::{Hub, RequestError::{NotConnected, Rejected{code,message}, DeliveryUnknown}}` — `register` refuses a second live connection for a host id; `unregister` fails that host's in-flight requests as `DeliveryUnknown`; `request` waits for the ingested fact carrying its `request_id`.
- HTTP (all behind the dev bearer token): `GET /api/hosts` → connected host ids; `POST /api/sessions` `{host_id, agent, cwd}` → 202 `{session_id}` once `session_started` is ingested, 502 `start_failed` / `unknown_agent`, 409 `host_offline`, 504 `delivery_unknown`; `POST /api/sessions/{id}/prompt` `{content}` → 202 `{turn_id}`, 400 `empty_prompt`, 404, 409 `not_attached` / `turn_in_progress`, 504 `delivery_unknown`; `GET /api/sessions/{id}/events?after=&limit=`; `GET /api/stream/sessions/{id}` SSE (`id:` = event_id, replays after `Last-Event-ID`, 15 s keepalive, `resync_required` on lag). `GET /api/hosts/ws` is the host socket (auth by `hello.token`).

- [ ] **Step 1: Extend the testkit dev-dependencies and write the failing end-to-end tests**

  `crates/roost-testkit/Cargo.toml`

  ```toml
  [package]
  name = "roost-testkit"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [[bin]]
  name = "roost-fake-acp"
  path = "src/bin/roost-fake-acp.rs"

  [dependencies]
  agent-client-protocol.workspace = true
  serde.workspace = true
  serde_json.workspace = true
  tokio.workspace = true

  [dev-dependencies]
  anyhow.workspace = true
  futures.workspace = true
  reqwest.workspace = true
  roost-host.workspace = true
  roost-kernel.workspace = true
  roost-proto.workspace = true
  roost-sessions.workspace = true



  tempfile = "3"
  tokio.workspace = true
  ```

  `crates/roost-testkit/tests/e2e.rs`

  ```rust
  //! End to end: a real collector (in process), a real host (in process) and the
  //! fake ACP adapter as a real child process, talking over real sockets.

  use roost_host::{AgentCommand, HostConfig};
  use roost_kernel::auth::DevToken;
  use roost_proto::rest::{EventDto, PromptResponse, StartSessionResponse};
  use roost_sessions::{AppState, store::Store};
  use roost_testkit::{FakeScript, SCRIPT_ENV};
  use serde_json::{Value, json};
  use std::net::SocketAddr;
  use std::path::Path;
  use std::time::Duration;

  const TOKEN: &str = "dev-token";

  struct Collector {
      addr: SocketAddr,
      state: AppState,
      task: tokio::task::JoinHandle<std::io::Result<()>>,
  }

  impl Collector {
      async fn start(db: &Path, addr: Option<SocketAddr>) -> Self {
          let listener = tokio::net::TcpListener::bind(addr.unwrap_or_else(|| "127.0.0.1:0".parse().unwrap()))
              .await
              .expect("bind collector");
          let addr = listener.local_addr().unwrap();
          let state = AppState::new(Store::open(db).unwrap(), DevToken::new(TOKEN));
          let task = tokio::spawn(roost_sessions::serve(listener, state.clone()));
          Self { addr, state, task }
      }

      async fn stop(self) {
          self.state.shutdown.cancel();
          self.task.await.unwrap().unwrap();
      }

      fn url(&self, path: &str) -> String {
          format!("http://{}{path}", self.addr)
      }
  }

  fn start_host(collector: SocketAddr, data_dir: &Path, script: &FakeScript) {
      let mut cfg = HostConfig::new(
          format!("ws://{collector}/api/hosts/ws"),
          "host-1",
          TOKEN,
          data_dir.to_path_buf(),
      );
      cfg.reconnect_min = Duration::from_millis(100);
      cfg.reconnect_max = Duration::from_millis(500);
      let mut fake = AgentCommand::parse(env!("CARGO_BIN_EXE_roost-fake-acp")).unwrap();
      fake.env
          .push((SCRIPT_ENV.into(), serde_json::to_string(script).unwrap()));
      cfg.agents.insert("fake".into(), fake);
      cfg.agents.insert(
          "broken".into(),
          AgentCommand::parse("/nonexistent/roost-test-adapter").unwrap(),
      );
      tokio::spawn(async move {
          roost_host::run(cfg).await.unwrap();
      });
  }

  fn client() -> reqwest::Client {
      let mut headers = reqwest::header::HeaderMap::new();
      headers.insert("authorization", format!("Bearer {TOKEN}").parse().unwrap());
      reqwest::Client::builder().default_headers(headers).build().unwrap()
  }

  async fn wait_for<T, F, Fut>(what: &str, mut probe: F) -> T
  where
      F: FnMut() -> Fut,
      Fut: std::future::Future<Output = Option<T>>,
  {
      let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
      loop {
          if let Some(v) = probe().await {
              return v;
          }
          assert!(tokio::time::Instant::now() < deadline, "timed out waiting for {what}");
          tokio::time::sleep(Duration::from_millis(50)).await;
      }
  }

  async fn wait_host_connected(c: &reqwest::Client, collector: &Collector) {
      let url = collector.url("/api/hosts");
      wait_for("host connection", || async {
          let hosts: Vec<String> = c.get(&url).send().await.ok()?.json().await.ok()?;
          hosts.contains(&"host-1".to_string()).then_some(())
      })
      .await;
  }

  async fn start_session(c: &reqwest::Client, collector: &Collector) -> String {
      let resp = c
          .post(collector.url("/api/sessions"))
          .json(&json!({ "host_id": "host-1", "agent": "fake", "cwd": std::env::temp_dir() }))
          .send()
          .await
          .unwrap();
      assert_eq!(resp.status(), 202, "{}", resp.text().await.unwrap());
      resp.json::<StartSessionResponse>().await.unwrap().session_id
  }

  fn text(s: &str) -> Value {
      json!([{ "type": "text", "text": s }])
  }

  async fn events(c: &reqwest::Client, collector: &Collector, session: &str) -> Vec<EventDto> {
      c.get(collector.url(&format!("/api/sessions/{session}/events")))
          .send()
          .await
          .unwrap()
          .json()
          .await
          .unwrap()
  }

  /// Concatenated text of every agent_message_chunk in the timeline.
  fn agent_text(events: &[EventDto]) -> String {
      events
          .iter()
          .filter(|e| e.kind == "acp_update")
          .filter_map(|e| e.body["payload"]["update"]["content"]["text"].as_str())
          .collect()
  }

  fn turn_ends(events: &[EventDto]) -> Vec<&EventDto> {
      events.iter().filter(|e| e.kind == "turn_ended").collect()
  }

  #[tokio::test]
  async fn a_prompt_streams_the_agents_reply_and_ends_the_turn_once() {
      let dir = tempfile::tempdir().unwrap();
      let collector = Collector::start(&dir.path().join("roost.db"), None).await;
      start_host(collector.addr, &dir.path().join("host"), &FakeScript::default());
      let c = client();
      wait_host_connected(&c, &collector).await;

      let session = start_session(&c, &collector).await;
      let resp = c
          .post(collector.url(&format!("/api/sessions/{session}/prompt")))
          .json(&json!({ "content": text("hi") }))
          .send()
          .await
          .unwrap();
      assert_eq!(resp.status(), 202);
      let turn = resp.json::<PromptResponse>().await.unwrap().turn_id;

      let evs = wait_for("turn end", || async {
          let evs = events(&c, &collector, &session).await;
          (!turn_ends(&evs).is_empty()).then_some(evs)
      })
      .await;
      assert_eq!(agent_text(&evs), "Hello world");
      let ends = turn_ends(&evs);
      assert_eq!(ends.len(), 1);
      assert_eq!(ends[0].body["turn_id"], turn);
      assert_eq!(ends[0].body["outcome"], "completed");
      // The user's turn precedes the agent's reply.
      let kinds: Vec<&str> = evs.iter().map(|e| e.kind.as_str()).collect();
      let user = kinds
          .iter()
          .position(|k| *k == "user_turn")
          .expect("user turn recorded");
      let first_update = kinds.iter().position(|k| *k == "acp_update").unwrap();
      assert!(user < first_update, "{kinds:?}");
  }

  #[tokio::test]
  async fn empty_prompts_and_overlapping_prompts_are_refused() {
      let dir = tempfile::tempdir().unwrap();
      let collector = Collector::start(&dir.path().join("roost.db"), None).await;
      let slow = FakeScript {
          chunks: vec!["a".into(), "b".into(), "c".into()],
          chunk_delay_ms: 300,
      };
      start_host(collector.addr, &dir.path().join("host"), &slow);
      let c = client();
      wait_host_connected(&c, &collector).await;
      let session = start_session(&c, &collector).await;
      let prompt_url = collector.url(&format!("/api/sessions/{session}/prompt"));

      let empty = c
          .post(&prompt_url)
          .json(&json!({ "content": [] }))
          .send()
          .await
          .unwrap();
      assert_eq!(empty.status(), 400);

      let first = c
          .post(&prompt_url)
          .json(&json!({ "content": text("one") }))
          .send()
          .await
          .unwrap();
      assert_eq!(first.status(), 202);
      let second = c
          .post(&prompt_url)
          .json(&json!({ "content": text("two") }))
          .send()
          .await
          .unwrap();
      assert_eq!(second.status(), 409);
      assert_eq!(second.json::<Value>().await.unwrap()["code"], "turn_in_progress");
  }

  #[tokio::test]
  async fn a_collector_restart_mid_turn_loses_nothing_and_duplicates_nothing() {
      let dir = tempfile::tempdir().unwrap();
      let db = dir.path().join("roost.db");
      let collector = Collector::start(&db, None).await;
      let addr = collector.addr;
      let script = FakeScript {
          chunks: (1..=6).map(|n| format!("{n}.")).collect(),
          chunk_delay_ms: 250,
      };
      start_host(addr, &dir.path().join("host"), &script);
      let c = client();
      wait_host_connected(&c, &collector).await;
      let session = start_session(&c, &collector).await;
      let resp = c
          .post(collector.url(&format!("/api/sessions/{session}/prompt")))
          .json(&json!({ "content": text("go") }))
          .send()
          .await
          .unwrap();
      assert_eq!(resp.status(), 202);

      // Wait for the first chunk, then take the collector down mid-turn.
      wait_for("first chunk", || async {
          (!agent_text(&events(&c, &collector, &session).await).is_empty()).then_some(())
      })
      .await;
      collector.stop().await;
      tokio::time::sleep(Duration::from_millis(700)).await; // chunks keep arriving at the host
      let collector = Collector::start(&db, Some(addr)).await;

      let evs = wait_for("turn end after restart", || async {
          let evs = events(&c, &collector, &session).await;
          (!turn_ends(&evs).is_empty()).then_some(evs)
      })
      .await;
      assert_eq!(agent_text(&evs), "1.2.3.4.5.6.");
      assert_eq!(turn_ends(&evs).len(), 1);
      let mut seqs: Vec<u64> = evs.iter().filter_map(|e| e.host_seq).collect();
      let n = seqs.len();
      seqs.dedup();
      assert_eq!(seqs.len(), n, "duplicate host seqs stored");
  }

  #[tokio::test]
  async fn the_session_stream_replays_from_last_event_id() {
      use futures::StreamExt;
      let dir = tempfile::tempdir().unwrap();
      let collector = Collector::start(&dir.path().join("roost.db"), None).await;
      start_host(collector.addr, &dir.path().join("host"), &FakeScript::default());
      let c = client();
      wait_host_connected(&c, &collector).await;
      let session = start_session(&c, &collector).await;
      c.post(collector.url(&format!("/api/sessions/{session}/prompt")))
          .json(&json!({ "content": text("hi") }))
          .send()
          .await
          .unwrap();
      let evs = wait_for("turn end", || async {
          let evs = events(&c, &collector, &session).await;
          (!turn_ends(&evs).is_empty()).then_some(evs)
      })
      .await;
      let resume_after = evs[1].event_id;

      let resp = c
          .get(collector.url(&format!("/api/stream/sessions/{session}")))
          .header("last-event-id", resume_after.to_string())
          .send()
          .await
          .unwrap();
      let mut body = resp.bytes_stream();
      let mut buf = String::new();
      let expected_ids: Vec<String> = evs
          .iter()
          .filter(|e| e.event_id > resume_after)
          .map(|e| format!("id: {}", e.event_id))
          .collect();
      let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
      while !expected_ids.iter().all(|id| buf.contains(id.as_str())) {
          let chunk = tokio::time::timeout_at(deadline, body.next())
              .await
              .expect("stream stalled")
              .unwrap()
              .unwrap();
          buf.push_str(&String::from_utf8_lossy(&chunk));
      }
      assert!(
          !buf.contains(&format!("id: {}\n", evs[0].event_id)),
          "replayed an event before Last-Event-ID: {buf}"
      );
  }

  #[tokio::test]
  async fn a_start_that_fails_on_the_host_is_reported_as_502() {
      let dir = tempfile::tempdir().unwrap();
      let collector = Collector::start(&dir.path().join("roost.db"), None).await;
      start_host(collector.addr, &dir.path().join("host"), &FakeScript::default());
      let c = client();
      wait_host_connected(&c, &collector).await;

      for (agent, expected) in [("broken", "start_failed"), ("not-configured", "unknown_agent")] {
          let resp = c
              .post(collector.url("/api/sessions"))
              .json(&json!({ "host_id": "host-1", "agent": agent, "cwd": std::env::temp_dir() }))
              .send()
              .await
              .unwrap();
          assert_eq!(resp.status(), 502, "agent {agent}");
          assert_eq!(resp.json::<Value>().await.unwrap()["code"], expected, "agent {agent}");
      }
  }
  ```

- [ ] **Step 2: Run the tests to see them fail**

  Run:

  ```bash
  cargo test -p roost-testkit --test e2e
  ```

  Expected: compilation fails: `roost_sessions::AppState`, `serve` and `router` do not exist.

- [ ] **Step 3: Implement the hub, the host socket, the API and the server**

  `crates/roost-sessions/src/lib.rs`

  ```rust
  //! Collector-side session module (ACP core spec §4, §8, §9).

  pub mod api;
  pub mod hub;
  pub mod store;
  pub mod ws;

  use axum::Router;
  use roost_kernel::auth::DevToken;
  use std::sync::Arc;
  use tokio_util::sync::CancellationToken;

  #[derive(Clone)]
  pub struct AppState {
      pub store: Arc<store::Store>,
      pub hub: Arc<hub::Hub>,
      pub token: DevToken,
      /// Cancelled on shutdown; long-lived handlers (host sockets, SSE) end
      /// when it fires so graceful shutdown completes.
      pub shutdown: CancellationToken,
  }

  impl AppState {
      pub fn new(store: store::Store, token: DevToken) -> Self {
          Self {
              store: Arc::new(store),
              hub: Arc::new(hub::Hub::new()),
              token,
              shutdown: CancellationToken::new(),
          }
      }
  }

  /// Serve until `state.shutdown` is cancelled.
  pub async fn serve(listener: tokio::net::TcpListener, state: AppState) -> std::io::Result<()> {
      let shutdown = state.shutdown.clone();
      axum::serve(listener, router(state))
          .with_graceful_shutdown(shutdown.cancelled_owned())
          .await
  }

  /// Every session route plus the host WebSocket.
  pub fn router(state: AppState) -> Router {
      api::router(state.clone()).merge(ws::router(state))
  }
  ```

  `crates/roost-sessions/src/hub.rs`

  ```rust
  //! Connected hosts and in-flight collector→host requests.

  use roost_proto::frames::{CollectorFrame, SessionBody};
  use roost_proto::rest::EventDto;
  use std::collections::HashMap;
  use std::sync::Mutex;
  use std::sync::atomic::{AtomicU64, Ordering};
  use std::time::Duration;
  use tokio::sync::{broadcast, mpsc, oneshot};

  /// Why a request produced no fact.
  #[derive(Debug, Clone, PartialEq)]
  pub enum RequestError {
      /// The host is not connected.
      NotConnected,
      /// The host rejected the request; nothing happened.
      Rejected { code: String, message: String },
      /// The connection dropped or the timeout passed: the request may or may
      /// not have been delivered. Reconciled later from the host's outbox.
      DeliveryUnknown,
  }

  struct Waiter {
      host_id: String,
      tx: oneshot::Sender<Result<SessionBody, RequestError>>,
  }

  struct HostConn {
      conn_id: u64,
      tx: mpsc::UnboundedSender<CollectorFrame>,
  }

  pub struct Hub {
      next_conn: AtomicU64,
      hosts: Mutex<HashMap<String, HostConn>>,
      waiters: Mutex<HashMap<String, Waiter>>,
      events: broadcast::Sender<EventDto>,
  }

  impl Default for Hub {
      fn default() -> Self {
          Self::new()
      }
  }

  impl Hub {
      pub fn new() -> Self {
          Self {
              next_conn: AtomicU64::new(1),
              hosts: Mutex::new(HashMap::new()),
              waiters: Mutex::new(HashMap::new()),
              events: broadcast::channel(1024).0,
          }
      }

      /// Register a host connection. A second live connection for the same
      /// host id is refused, never allowed to supersede the first silently.
      pub fn register(&self, host_id: &str, tx: mpsc::UnboundedSender<CollectorFrame>) -> Option<u64> {
          let mut hosts = self.hosts.lock().expect("hosts lock");
          if hosts.get(host_id).is_some_and(|h| !h.tx.is_closed()) {
              return None;
          }
          let conn_id = self.next_conn.fetch_add(1, Ordering::Relaxed);
          hosts.insert(host_id.to_string(), HostConn { conn_id, tx });
          Some(conn_id)
      }

      /// Drop a connection and fail its in-flight requests as delivery-unknown.
      pub fn unregister(&self, host_id: &str, conn_id: u64) {
          let mut hosts = self.hosts.lock().expect("hosts lock");
          if hosts.get(host_id).is_some_and(|h| h.conn_id == conn_id) {
              hosts.remove(host_id);
          }
          drop(hosts);
          let mut waiters = self.waiters.lock().expect("waiters lock");
          let ids: Vec<String> = waiters
              .iter()
              .filter(|(_, w)| w.host_id == host_id)
              .map(|(k, _)| k.clone())
              .collect();
          for id in ids {
              if let Some(w) = waiters.remove(&id) {
                  let _ = w.tx.send(Err(RequestError::DeliveryUnknown));
              }
          }
      }

      pub fn connected_hosts(&self) -> Vec<String> {
          let mut ids: Vec<String> = self.hosts.lock().expect("hosts lock").keys().cloned().collect();
          ids.sort();
          ids
      }

      /// Send a frame to a host without waiting for anything.
      pub fn send(&self, host_id: &str, frame: CollectorFrame) -> bool {
          self.hosts
              .lock()
              .expect("hosts lock")
              .get(host_id)
              .is_some_and(|h| h.tx.send(frame).is_ok())
      }

      /// Send a request and wait until the outboxed fact carrying `request_id`
      /// is ingested (`resolve`), the host rejects it (`reject`), the
      /// connection drops, or `timeout` passes.
      pub async fn request(
          &self,
          host_id: &str,
          request_id: &str,
          frame: CollectorFrame,
          timeout: Duration,
      ) -> Result<SessionBody, RequestError> {
          let (tx, rx) = oneshot::channel();
          self.waiters.lock().expect("waiters lock").insert(
              request_id.to_string(),
              Waiter {
                  host_id: host_id.to_string(),
                  tx,
              },
          );
          if !self.send(host_id, frame) {
              self.waiters.lock().expect("waiters lock").remove(request_id);
              return Err(RequestError::NotConnected);
          }
          let result = tokio::time::timeout(timeout, rx).await;
          self.waiters.lock().expect("waiters lock").remove(request_id);
          match result {
              Ok(Ok(outcome)) => outcome,
              _ => Err(RequestError::DeliveryUnknown),
          }
      }

      pub fn resolve(&self, request_id: &str, fact: SessionBody) {
          if let Some(w) = self.waiters.lock().expect("waiters lock").remove(request_id) {
              let _ = w.tx.send(Ok(fact));
          }
      }

      pub fn reject(&self, request_id: &str, code: String, message: String) {
          if let Some(w) = self.waiters.lock().expect("waiters lock").remove(request_id) {
              let _ = w.tx.send(Err(RequestError::Rejected { code, message }));
          }
      }

      pub fn publish(&self, event: EventDto) {
          let _ = self.events.send(event);
      }

      pub fn subscribe(&self) -> broadcast::Receiver<EventDto> {
          self.events.subscribe()
      }
  }
  ```

  `crates/roost-sessions/src/ws.rs`

  ```rust
  //! The host WebSocket endpoint (ACP core §3, §5).

  use crate::AppState;
  use axum::Router;
  use axum::extract::State;
  use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
  use axum::response::Response;
  use axum::routing::get;
  use futures::{SinkExt, StreamExt};
  use roost_proto::frames::{CollectorFrame, HostFrame, SessionBody};
  use roost_proto::{PROTOCOL_VERSION, protocol_major};
  use std::collections::BTreeMap;
  use std::time::Duration;
  use tokio::sync::mpsc;

  const MAX_FRAME: usize = 32 << 20;
  const PING_INTERVAL: Duration = Duration::from_secs(15);
  const READ_TIMEOUT: Duration = Duration::from_secs(45);
  const HELLO_TIMEOUT: Duration = Duration::from_secs(10);

  pub fn router(state: AppState) -> Router {
      Router::new().route("/api/hosts/ws", get(upgrade)).with_state(state)
  }

  async fn upgrade(ws: WebSocketUpgrade, State(state): State<AppState>) -> Response {
      ws.max_message_size(MAX_FRAME)
          .max_frame_size(MAX_FRAME)
          .on_upgrade(move |socket| serve(socket, state))
  }

  fn text(frame: &CollectorFrame) -> Message {
      Message::Text(serde_json::to_string(frame).expect("frame serializes").into())
  }

  async fn serve(socket: WebSocket, state: AppState) {
      let (mut sink, mut stream) = socket.split();

      // 1. hello: first frame, authenticated.
      let hello = match tokio::time::timeout(HELLO_TIMEOUT, stream.next()).await {
          Ok(Some(Ok(Message::Text(t)))) => serde_json::from_str::<HostFrame>(&t).ok(),
          _ => None,
      };
      let Some(HostFrame::Hello {
          protocol_version,
          host_id,
          token,
          attached_sessions,
          ..
      }) = hello
      else {
          return;
      };
      let reject = |code: &str, message: &str| CollectorFrame::HelloError {
          code: code.into(),
          message: message.into(),
      };
      if protocol_major(&protocol_version) != protocol_major(PROTOCOL_VERSION) {
          let _ = sink
              .send(text(&reject("incompatible_protocol", "unsupported protocol major")))
              .await;
          return;
      }
      if !state.token.matches(&token) {
          let _ = sink
              .send(text(&reject("unauthorized", "invalid host credential")))
              .await;
          return;
      }
      let (tx, mut rx) = mpsc::unbounded_channel::<CollectorFrame>();
      let Some(conn_id) = state.hub.register(&host_id, tx.clone()) else {
          let _ = sink
              .send(text(&reject(
                  "already_connected",
                  "another connection for this host is live",
              )))
              .await;
          return;
      };

      let mut committed = BTreeMap::new();
      for a in &attached_sessions {
          committed.insert(
              a.session_id.clone(),
              state.store.committed_seq(&a.session_id).unwrap_or(0),
          );
      }
      let ack = CollectorFrame::HelloAck {
          protocol_version: PROTOCOL_VERSION.into(),
          collector_version: env!("CARGO_PKG_VERSION").into(),
          committed,
      };
      if sink.send(text(&ack)).await.is_err() {
          state.hub.unregister(&host_id, conn_id);
          return;
      }
      tracing::info!(%host_id, "host connected");

      // 2. writer: frames for this host plus keepalive pings.
      let writer = tokio::spawn(async move {
          let mut ping = tokio::time::interval(PING_INTERVAL);
          ping.tick().await;
          loop {
              tokio::select! {
                  frame = rx.recv() => match frame {
                      Some(frame) => if sink.send(text(&frame)).await.is_err() { break },
                      None => break,
                  },
                  _ = ping.tick() => if sink.send(Message::Ping(Default::default())).await.is_err() { break },
              }
          }
      });

      // 3. reader.
      loop {
          let next = tokio::select! {
              _ = state.shutdown.cancelled() => break,
              next = tokio::time::timeout(READ_TIMEOUT, stream.next()) => next,
          };
          let msg = match next {
              Ok(Some(Ok(msg))) => msg,
              _ => break,
          };
          let Message::Text(t) = msg else {
              if matches!(msg, Message::Close(_)) {
                  break;
              }
              continue;
          };
          let frame = match serde_json::from_str::<HostFrame>(&t) {
              Ok(f) => f,
              Err(err) => {
                  tracing::warn!(%host_id, error = %err, "ignoring unknown or invalid frame");
                  continue;
              }
          };
          match frame {
              HostFrame::Session { session_id, seq, body } => {
                  // A host may only write to its own sessions.
                  match state.store.session(&session_id) {
                      Ok(Some(row)) if row.host_id == host_id => {}
                      _ => {
                          tracing::warn!(%host_id, %session_id, "frame for a session this host does not own");
                          continue;
                      }
                  }
                  match state.store.ingest(&session_id, seq, &body) {
                      Ok(created) => {
                          for event in created {
                              state.hub.publish(event);
                          }
                          match &body {
                              SessionBody::SessionStarted { request_id, .. }
                              | SessionBody::TurnStarted { request_id, .. } => {
                                  state.hub.resolve(request_id, body.clone());
                              }
                              SessionBody::StartFailed {
                                  request_id,
                                  code,
                                  message,
                              } => {
                                  state.hub.reject(request_id, code.clone(), message.clone());
                              }
                              _ => {}
                          }
                          let _ = tx.send(CollectorFrame::Ack {
                              session_id,
                              ack_seq: seq,
                          });
                      }
                      Err(err) => tracing::error!(%host_id, error = %err, "ingest failed; not acking"),
                  }
              }
              HostFrame::Error {
                  request_id,
                  code,
                  message,
              } => state.hub.reject(&request_id, code, message),
              HostFrame::ResendComplete => tracing::debug!(%host_id, "host finished resending"),
              HostFrame::Hello { .. } => tracing::warn!(%host_id, "ignoring repeated hello"),
          }
      }

      writer.abort();
      state.hub.unregister(&host_id, conn_id);
      tracing::info!(%host_id, "host disconnected");
  }
  ```

  `crates/roost-sessions/src/api.rs`

  ```rust
  //! Session REST and SSE endpoints (ACP core §9), walking-skeleton subset.

  use crate::AppState;
  use crate::hub::RequestError;
  use axum::extract::{Path, Query, State};
  use axum::http::{HeaderMap, StatusCode};
  use axum::response::sse::{Event, KeepAlive, Sse};
  use axum::response::{IntoResponse, Response};
  use axum::routing::{get, post};
  use axum::{Json, Router, middleware};
  use futures::stream::{self, Stream, StreamExt};
  use roost_proto::frames::CollectorFrame;
  use roost_proto::rest::{ApiError, EventDto, PromptRequest, PromptResponse, StartSessionRequest, StartSessionResponse};
  use serde::Deserialize;
  use std::convert::Infallible;
  use std::time::Duration;
  use tokio_stream::wrappers::BroadcastStream;

  const START_TIMEOUT: Duration = Duration::from_secs(90);
  /// At least the WebSocket read deadline, so a half-open socket is detected
  /// before the request gives up (ACP core §3.4).
  const PROMPT_TIMEOUT: Duration = Duration::from_secs(60);

  pub fn router(state: AppState) -> Router {
      Router::new()
          .route("/api/hosts", get(list_hosts))
          .route("/api/sessions", post(start_session))
          .route("/api/sessions/{id}/prompt", post(prompt))
          .route("/api/sessions/{id}/events", get(events))
          .route("/api/stream/sessions/{id}", get(stream_session))
          .layer(middleware::from_fn_with_state(
              state.token.clone(),
              roost_kernel::auth::require_bearer,
          ))
          .with_state(state)
  }

  fn error(status: StatusCode, code: &str, message: impl Into<String>) -> Response {
      (
          status,
          Json(ApiError {
              code: code.into(),
              message: message.into(),
          }),
      )
          .into_response()
  }

  fn internal(err: anyhow::Error) -> Response {
      tracing::error!(error = %err, "internal error");
      error(StatusCode::INTERNAL_SERVER_ERROR, "internal", "internal error")
  }

  fn request_failed(err: RequestError) -> Response {
      match err {
          RequestError::NotConnected => error(StatusCode::CONFLICT, "host_offline", "the host is not connected"),
          RequestError::Rejected { code, message } => {
              let status = match code.as_str() {
                  "not_attached" => StatusCode::CONFLICT,
                  "unknown_agent" | "start_failed" => StatusCode::BAD_GATEWAY,
                  "invalid" => StatusCode::BAD_REQUEST,
                  _ => StatusCode::BAD_GATEWAY,
              };
              error(status, &code, message)
          }
          RequestError::DeliveryUnknown => error(
              StatusCode::GATEWAY_TIMEOUT,
              "delivery_unknown",
              "the host did not confirm in time; the result will appear when it reconnects",
          ),
      }
  }

  async fn list_hosts(State(state): State<AppState>) -> Json<Vec<String>> {
      Json(state.hub.connected_hosts())
  }

  async fn start_session(State(state): State<AppState>, Json(req): Json<StartSessionRequest>) -> Response {
      let session_id = uuid::Uuid::now_v7().to_string();
      if let Err(err) = state
          .store
          .create_session(&session_id, &req.host_id, &req.agent, &req.cwd)
      {
          return internal(err);
      }
      let request_id = uuid::Uuid::now_v7().to_string();
      let frame = CollectorFrame::StartSession {
          request_id: request_id.clone(),
          session_id: session_id.clone(),
          agent: req.agent,
          cwd: req.cwd,
      };
      match state.hub.request(&req.host_id, &request_id, frame, START_TIMEOUT).await {
          Ok(_) => (StatusCode::ACCEPTED, Json(StartSessionResponse { session_id })).into_response(),
          Err(RequestError::DeliveryUnknown) => request_failed(RequestError::DeliveryUnknown),
          Err(err) => {
              let reason = match &err {
                  RequestError::Rejected { code, .. } => code.clone(),
                  _ => "host_offline".into(),
              };
              if let Err(e) = state.store.mark_failed(&session_id, &reason) {
                  return internal(e);
              }
              request_failed(err)
          }
      }
  }

  async fn prompt(State(state): State<AppState>, Path(id): Path<String>, Json(req): Json<PromptRequest>) -> Response {
      if req.content.is_empty() {
          return error(
              StatusCode::BAD_REQUEST,
              "empty_prompt",
              "a prompt needs text or an image",
          );
      }
      let session = match state.store.session(&id) {
          Ok(Some(s)) => s,
          Ok(None) => return error(StatusCode::NOT_FOUND, "not_found", "no such session"),
          Err(err) => return internal(err),
      };
      if session.lifecycle != "active" {
          return error(StatusCode::CONFLICT, "not_attached", "resume the session first");
      }
      let turn_id = uuid::Uuid::now_v7().to_string();
      match state.store.open_turn(&id, &turn_id, &req.content) {
          Ok(true) => {}
          Ok(false) => {
              return error(
                  StatusCode::CONFLICT,
                  "turn_in_progress",
                  "wait for the current turn to end",
              );
          }
          Err(err) => return internal(err),
      }
      let request_id = uuid::Uuid::now_v7().to_string();
      let frame = CollectorFrame::Prompt {
          request_id: request_id.clone(),
          session_id: id.clone(),
          turn_id: turn_id.clone(),
          content: req.content,
      };
      match state
          .hub
          .request(&session.host_id, &request_id, frame, PROMPT_TIMEOUT)
          .await
      {
          Ok(_) => (StatusCode::ACCEPTED, Json(PromptResponse { turn_id })).into_response(),
          // Unknown delivery keeps the turn open; the outbox resolves it.
          Err(RequestError::DeliveryUnknown) => request_failed(RequestError::DeliveryUnknown),
          Err(err) => {
              if let Err(e) = state.store.abandon_turn(&id, &turn_id) {
                  return internal(e);
              }
              request_failed(err)
          }
      }
  }

  #[derive(Deserialize)]
  struct EventsQuery {
      #[serde(default)]
      after: i64,
      #[serde(default = "default_limit")]
      limit: u32,
  }

  fn default_limit() -> u32 {
      500
  }

  async fn events(State(state): State<AppState>, Path(id): Path<String>, Query(q): Query<EventsQuery>) -> Response {
      match state.store.events(&id, q.after, q.limit.min(5000)) {
          Ok(list) => Json(list).into_response(),
          Err(err) => internal(err),
      }
  }

  fn sse_event(e: &EventDto) -> Event {
      Event::default()
          .id(e.event_id.to_string())
          .event("event")
          .data(serde_json::to_string(e).expect("event serializes"))
  }

  /// Session stream: replays from `Last-Event-ID`, then follows live events.
  async fn stream_session(
      State(state): State<AppState>,
      Path(id): Path<String>,
      headers: HeaderMap,
  ) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
      let after: i64 = headers
          .get("last-event-id")
          .and_then(|v| v.to_str().ok())
          .and_then(|v| v.parse().ok())
          .unwrap_or(0);
      // Subscribe before reading the backlog so nothing falls between them.
      let live = BroadcastStream::new(state.hub.subscribe());
      let backlog = state.store.events(&id, after, u32::MAX).unwrap_or_default();
      let last = backlog.last().map(|e| e.event_id).unwrap_or(after);
      let replay = stream::iter(backlog.into_iter().map(|e| Ok(sse_event(&e))));
      let session = id.clone();
      let follow = live.filter_map(move |item| {
          let session = session.clone();
          async move {
              match item {
                  Ok(e) if e.session_id == session && e.event_id > last => Some(Ok(sse_event(&e))),
                  Ok(_) => None,
                  // Lagged: tell the client to refetch instead of skipping silently.
                  Err(_) => Some(Ok(Event::default().event("resync_required").data("{}"))),
              }
          }
      });
      let stream = replay
          .chain(follow)
          .take_until(state.shutdown.clone().cancelled_owned());
      Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
  }
  ```

  Notes for the reviewer:

  - HTTP waiters are resolved by the **ingested outbox fact** (`session_started` / `turn_started` carrying the request id), not by a separate response frame, so a lost confirmation can only produce `delivery_unknown`, never a wrong state (ACP core §3.3, §5).
  - The WebSocket reader ignores frames for sessions owned by a different host.
  - `shutdown` makes graceful shutdown finish even with open host sockets and SSE streams; the restart test depends on it.

- [ ] **Step 4: Run the end-to-end tests**

  Run:

  ```bash
  cargo test -p roost-testkit --test e2e
  ```

  Expected: 5 tests pass, including `a_collector_restart_mid_turn_loses_nothing_and_duplicates_nothing`.

- [ ] **Step 5: Revert probe**

  In `crates/roost-host/src/connection.rs`, replace the `send_pending(&mut sink, uplink, &mut sent).await?;` call that follows `hello_ack` with a loop that only records every pending frame's seq in `sent` without sending it — i.e. the host skips its backlog on reconnect. Confirm `a_collector_restart_mid_turn_loses_nothing_and_duplicates_nothing` fails (chunks produced while the collector was down never arrive). Restore. (This probe was verified while writing the plan; a probe that only kept `sent` across reconnects does **not** fail the test, because frames are rarely lost inside a closing socket.)

- [ ] **Step 6: Run everything**

  Run:

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace && cargo run -p roost-proto --bin gen -- --check
  ```

  Expected: clean; 34 tests pass.

- [ ] **Step 7: Commit**

  ```bash
  git add crates/roost-sessions crates/roost-testkit Cargo.lock
  git commit -m "feat(sessions): add host socket, session REST and SSE with end-to-end tests"
  ```

---

### Task 9: The `roost` binary

**Files:**
- Create: `crates/roost/Cargo.toml`, `crates/roost/src/main.rs`
- Test: `crates/roost/tests/cli.rs`

**Interfaces:**
- Consumes: `roost_sessions::{AppState, router, store::Store}`, `roost_host::{HostConfig, run, AgentCommand}`, `roost_kernel::auth::DevToken`.
- Produces: `roost collector --listen --data-dir --dev-token` (placeholder page at `/`), `roost host run --collector --host-id --data-dir --dev-token --agent name=command…`, `roost up --listen --data-dir --dev-token --agent …` (two child processes; SIGTERM/SIGINT stop the host first, then the collector). Tokens also come from `ROOST_DEV_TOKEN`; directories from `ROOST_DATA_DIR` / `ROOST_HOST_DATA_DIR`.

- [ ] **Step 1: Write the failing CLI tests**

  `crates/roost/Cargo.toml`

  ```toml
  [package]
  name = "roost"
  version.workspace = true
  edition.workspace = true
  license.workspace = true
  rust-version.workspace = true
  publish.workspace = true

  [dependencies]
  anyhow.workspace = true
  axum.workspace = true
  clap.workspace = true
  libc = "0.2"
  roost-host.workspace = true
  roost-kernel.workspace = true
  roost-sessions.workspace = true
  tokio.workspace = true
  tracing.workspace = true
  tracing-subscriber.workspace = true
  ```

  `crates/roost/tests/cli.rs`

  ```rust
  use std::process::Command;

  #[test]
  fn help_lists_the_skeleton_commands() {
      let out = Command::new(env!("CARGO_BIN_EXE_roost"))
          .arg("--help")
          .output()
          .unwrap();
      let text = String::from_utf8_lossy(&out.stdout);
      for cmd in ["collector", "host", "up"] {
          assert!(text.contains(cmd), "missing {cmd} in help:\n{text}");
      }
  }

  #[test]
  fn a_malformed_agent_flag_is_rejected() {
      let out = Command::new(env!("CARGO_BIN_EXE_roost"))
          .args([
              "host",
              "run",
              "--collector",
              "ws://x",
              "--data-dir",
              "/tmp/x",
              "--dev-token",
              "t",
              "--agent",
              "noequals",
          ])
          .output()
          .unwrap();
      assert!(!out.status.success());
      assert!(String::from_utf8_lossy(&out.stderr).contains("name=command"));
  }
  ```

- [ ] **Step 2: Run them to see them fail**

  Run:

  ```bash
  cargo test -p roost --test cli
  ```

  Expected: compilation fails or the binary has no subcommands (`src/main.rs` missing).

- [ ] **Step 3: Implement the binary**

  `crates/roost/src/main.rs`

  ```rust
  //! The `roost` binary (distribution spec §1). Walking skeleton: collector,
  //! host and an all-in-one mode, with a shared development token.

  use anyhow::{Context, Result};
  use axum::response::Html;
  use axum::routing::get;
  use clap::{Args, Parser, Subcommand};
  use roost_host::{AgentCommand, HostConfig};
  use roost_kernel::auth::DevToken;
  use roost_sessions::{AppState, store::Store};
  use std::path::PathBuf;

  #[derive(Parser)]
  #[command(name = "roost", version, about = "Self-hosted cockpit for ACP coding agents")]
  struct Cli {
      #[command(subcommand)]
      command: Command,
  }

  #[derive(Subcommand)]
  enum Command {
      /// Run the collector.
      Collector(CollectorArgs),
      /// Host commands.
      Host {
          #[command(subcommand)]
          command: HostCommand,
      },
      /// Run a collector and a host together (two processes).
      Up(UpArgs),
  }

  #[derive(Subcommand)]
  enum HostCommand {
      /// Run a host.
      Run(HostArgs),
  }

  #[derive(Args, Clone)]
  struct CollectorArgs {
      #[arg(long, default_value = "127.0.0.1:7117")]
      listen: String,
      #[arg(long, env = "ROOST_DATA_DIR")]
      data_dir: PathBuf,
      /// Development token for hosts and API clients (skeleton only).
      #[arg(long, env = "ROOST_DEV_TOKEN", hide_env_values = true)]
      dev_token: String,
  }

  #[derive(Args, Clone)]
  struct HostArgs {
      /// e.g. ws://127.0.0.1:7117/api/hosts/ws
      #[arg(long)]
      collector: String,
      #[arg(long, default_value = "local")]
      host_id: String,
      #[arg(long, env = "ROOST_HOST_DATA_DIR")]
      data_dir: PathBuf,
      #[arg(long, env = "ROOST_DEV_TOKEN", hide_env_values = true)]
      dev_token: String,
      /// Agent adapter, as `name=command args…`. Repeatable.
      #[arg(long = "agent", value_parser = parse_agent)]
      agents: Vec<(String, AgentCommand)>,
  }

  #[derive(Args)]
  struct UpArgs {
      #[arg(long, default_value = "127.0.0.1:7117")]
      listen: String,
      #[arg(long, env = "ROOST_DATA_DIR")]
      data_dir: PathBuf,
      #[arg(long, env = "ROOST_DEV_TOKEN", hide_env_values = true)]
      dev_token: String,
      #[arg(long = "agent", value_parser = parse_agent)]
      agents: Vec<(String, AgentCommand)>,
  }

  fn parse_agent(s: &str) -> Result<(String, AgentCommand), String> {
      let (name, command) = s.split_once('=').ok_or("expected name=command")?;
      let command = AgentCommand::parse(command).ok_or("empty command")?;
      Ok((name.to_string(), command))
  }

  const PLACEHOLDER: &str = "<!doctype html><meta charset=utf-8><title>roost</title><h1>roost</h1><p>Walking skeleton. The UI is not built yet.</p>";

  #[tokio::main]
  async fn main() -> Result<()> {
      tracing_subscriber::fmt()
          .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
          .init();
      match Cli::parse().command {
          Command::Collector(args) => run_collector(args).await,
          Command::Host {
              command: HostCommand::Run(args),
          } => run_host(args).await,
          Command::Up(args) => run_up(args).await,
      }
  }

  async fn run_collector(args: CollectorArgs) -> Result<()> {
      std::fs::create_dir_all(&args.data_dir)?;
      let store = Store::open(&args.data_dir.join("roost.db"))?;
      let state = AppState::new(store, DevToken::new(args.dev_token));
      let listener = tokio::net::TcpListener::bind(&args.listen)
          .await
          .with_context(|| format!("bind {}", args.listen))?;
      tracing::info!(address = %listener.local_addr()?, "collector listening");
      let shutdown = state.shutdown.clone();
      tokio::spawn(async move {
          terminated().await;
          shutdown.cancel();
      });
      let app = roost_sessions::router(state.clone()).route("/", get(|| async { Html(PLACEHOLDER) }));
      axum::serve(listener, app)
          .with_graceful_shutdown(state.shutdown.clone().cancelled_owned())
          .await?;
      Ok(())
  }

  async fn run_host(args: HostArgs) -> Result<()> {
      let mut cfg = HostConfig::new(args.collector, args.host_id, args.dev_token, args.data_dir);
      cfg.agents = args.agents.into_iter().collect();
      // Returning from main drops the runtime, which drops every session actor
      // and with it (kill_on_drop) every adapter process.
      tokio::select! {
          result = roost_host::run(cfg) => result,
          _ = terminated() => Ok(()),
      }
  }

  /// Resolves on SIGINT or SIGTERM.
  async fn terminated() {
      let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
      tokio::select! {
          _ = tokio::signal::ctrl_c() => {}
          _ = term.recv() => {}
      }
  }

  /// Ask a child to shut down cleanly.
  fn sigterm(child: &tokio::process::Child) {
      if let Some(pid) = child.id() {
          // SAFETY: plain kill(2) on a pid we spawned and still own.
          unsafe {
              libc::kill(pid as libc::pid_t, libc::SIGTERM);
          }
      }
  }

  /// Two child processes of this binary, exchanging the same frames as a
  /// remote host (architecture spec §3.3). The supervisor exits when either
  /// child exits; restart policy comes with the distribution work.
  async fn run_up(args: UpArgs) -> Result<()> {
      let exe = std::env::current_exe()?;
      let mut collector = tokio::process::Command::new(&exe)
          .args(["collector", "--listen", &args.listen])
          .arg("--data-dir")
          .arg(args.data_dir.join("collector"))
          .env("ROOST_DEV_TOKEN", &args.dev_token)
          .kill_on_drop(true)
          .spawn()?;
      let mut host_cmd = tokio::process::Command::new(&exe);
      host_cmd
          .args([
              "host",
              "run",
              "--collector",
              &format!("ws://{}/api/hosts/ws", args.listen),
          ])
          .arg("--data-dir")
          .arg(args.data_dir.join("host"))
          .env("ROOST_DEV_TOKEN", &args.dev_token)
          .kill_on_drop(true);
      for (name, command) in &args.agents {
          let mut spec = format!("{name}={}", command.program);
          for a in &command.args {
              spec.push(' ');
              spec.push_str(a);
          }
          host_cmd.arg("--agent").arg(spec);
      }
      let mut host = host_cmd.spawn()?;
      tokio::select! {
          status = collector.wait() => tracing::warn!(?status, "collector exited"),
          status = host.wait() => tracing::warn!(?status, "host exited"),
          _ = terminated() => {}
      }
      // Host first (it stops its adapters), then the collector.
      sigterm(&host);
      let _ = tokio::time::timeout(std::time::Duration::from_secs(10), host.wait()).await;
      sigterm(&collector);
      let _ = tokio::time::timeout(std::time::Duration::from_secs(10), collector.wait()).await;
      Ok(())
  }
  ```

- [ ] **Step 4: Run the tests**

  Run:

  ```bash
  cargo test -p roost
  ```

  Expected: 2 tests pass.

- [ ] **Step 5: Smoke test the all-in-one mode by hand**

  ```bash
  cargo build
  ./target/debug/roost up --listen 127.0.0.1:17117 --data-dir /tmp/roost-smoke --dev-token t \
    --agent "fake=$PWD/target/debug/roost-fake-acp" &
  sleep 3
  H='authorization: Bearer t'
  curl -s -H "$H" localhost:17117/api/hosts            # ["local"]
  SID=$(curl -s -H "$H" -H 'content-type: application/json' \
    -d '{"host_id":"local","agent":"fake","cwd":"/tmp"}' localhost:17117/api/sessions | sed 's/.*"session_id":"\([^"]*\)".*/\1/')
  curl -s -H "$H" -H 'content-type: application/json' \
    -d '{"content":[{"type":"text","text":"hi"}]}' localhost:17117/api/sessions/$SID/prompt
  sleep 1
  curl -s -H "$H" localhost:17117/api/sessions/$SID/events
  kill %1; sleep 2
  pgrep -fl 'roost-fake-acp|target/debug/roost ' || echo "no processes left"
  ```

  Expected: the events list shows `session_started`, `turn_started`, `user_turn`, two `acp_update`, `turn_ended`; after `kill` no roost or fake-adapter process remains.

- [ ] **Step 6: Final verification and commit**

  Run:

  ```bash
  cargo fmt --all --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace && cargo run -p roost-proto --bin gen -- --check
  ```

  Expected: clean; 36 tests pass.

  ```bash
  git add crates/roost Cargo.lock
  git commit -m "feat(cli): add roost binary with collector, host and all-in-one modes"
  ```

---

## After this plan

The next plans, in order, each against the ACP core spec: (1) resume, park, close and `resend_complete` reconciliation with the adapter exit watcher and idle reaper; (2) permission and elicitation with the pending-request set and answer queue; (3) real auth and pairing (kernel spec), replacing the development token; (4) the frontend shell and session view; (5) hats; (6) the gateway; (7) distribution.

---

_Generated with Claude AI — please review before distribution._

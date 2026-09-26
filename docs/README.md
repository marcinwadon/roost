# roost design documents

Status: design phase. Everything here is a draft awaiting the maintainer's
review; nothing is implemented yet.

## Reading order

1. [Architecture (umbrella spec)](specs/2026-09-25-roost-architecture-design.md)
   — product, topology, protocol principles, sessions, auth, hats, module
   boundaries, testing. Authoritative where subsystem specs disagree.
2. Subsystem specs:
   - [ACP core](specs/2026-09-26-acp-core-design.md) — host, adapters,
     host↔collector protocol, session state machine, storage, session API.
   - [Kernel](specs/2026-09-26-kernel-design.md) — storage, config, operator
     auth, pairing, hats, push, HTTP security.
   - [MCP gateway](specs/2026-09-26-mcp-gateway-design.md) — connections,
     OAuth, per-session tokens, streaming proxy, renderers.
   - [Frontend](specs/2026-09-26-frontend-design.md) — views, data layer,
     transcript fold, cards, PWA.
   - [Distribution](specs/2026-09-26-distribution-design.md) — binary,
     managed runtime and adapter manifest, release pipeline, services, doctor.
3. Evidence:
   - [Spike: per-session MCP over ACP](spikes/2026-09-25-per-session-mcp.md)
     and its [harness](../spikes/2026-09-25-mcp-per-session/).
4. Plans:
   - [Walking skeleton](plans/2026-09-26-walking-skeleton.md) — the first
     implementation plan (9 tasks, code verified by replaying the plan).

## Decisions waiting for the maintainer

Collected from the specs' open-question sections, most consequential first.

1. **Product name.** "roost" is taken on crates.io by a same-niche project
   that also ships a `roost` binary via its own Homebrew tap; a second
   same-niche project uses the name too (distribution §11). Keep or rename.
2. **`roost up` and credentials of several hats.** In the all-in-one install
   the collector runs as the same OS user as every agent, so any agent can read
   the master key and database (umbrella §8.4, kernel §10). The specs document
   this and recommend a separate collector; should v1 also support the OS
   keystore for the master key (kernel open question 3)?
3. **Per-hat "isolate agent user config".** Claude's strict MCP mode isolates
   MCP servers only; the user's `~/.claude` instructions, memory, hooks and
   plugins still load in every hat (umbrella §8.3). Worth a v1 option?
4. **Concurrent Codex processes sharing a composed `CODEX_HOME`** must be
   measured; until then Codex on a mixed host gets default-hat mounts only.
5. **Headless macOS hosts** — "requires a logged-in user" acceptable for v1?
   (distribution open question 2)
6. **Adapter set size** (~700 MB per set, mostly the bundled agent CLIs) — offer
   "use my own CLI" as a first-class, space-saving option? (distribution 3)
7. Smaller ones: attachment retention caps (ACP core 1), VAPID contact
   (kernel 1), multiple listeners (kernel 2), static-token header templates
   (gateway 3), transcript windowing threshold (frontend 1).

---

_Generated with Claude AI — please review before distribution._

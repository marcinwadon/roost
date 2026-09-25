# Spike harness: per-session MCP over ACP

Throwaway tooling behind
[`docs/spikes/2026-09-25-per-session-mcp.md`](../../docs/spikes/2026-09-25-per-session-mcp.md).
Not part of the product.

- `probe_mcp.py NAME PORT` — minimal streamable-HTTP MCP server with one tool;
  logs every JSON-RPC method it receives as a JSON line on stdout.
- `acp_drive.py` — minimal ACP client: `initialize` → `session/new` (or
  `session/load`) with the given MCP servers and optional `_meta` → one prompt,
  auto-allowing permissions; prints a JSON summary.
- `run.sh` — resets probe logs, drives one session, prints the summary and
  probe hit counts.
- `count.py` — summarises probe logs.

Sketch of a run (work dir defaults to `/tmp/roostspike`):

```sh
mkdir -p /tmp/roostspike/logs
python3 probe_mcp.py roostspike_session 18701 >> /tmp/roostspike/logs/session.log &
python3 probe_mcp.py roostspike_global  18702 >> /tmp/roostspike/logs/global.log &
claude mcp add --scope user --transport http roostspike_global http://127.0.0.1:18702/mcp
./run.sh claude-baseline /path/to/project "" claude-agent-acp
./run.sh claude-strict   /path/to/project \
  '{"claudeCode":{"options":{"extraArgs":{"strict-mcp-config":""}}}}' claude-agent-acp
claude mcp remove roostspike_global -s user
```

Probe logs must be opened in append mode (`>>`): truncating a file that a
process writes with `>` leaves it writing at its old offset.

Registering probes in the real per-user config modifies it; back it up first.
`codex mcp add/remove` rewrites `~/.codex/config.toml` and drops comments.

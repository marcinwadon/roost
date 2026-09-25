#!/usr/bin/env python3
"""Compose a roost-owned CODEX_HOME from a user's CODEX_HOME.

Every top-level entry of SRC is symlinked into DST except config.toml, which
is copied with all `mcp_servers` tables removed. The user's auth, AGENTS.md,
skills, session history and settings stay shared; only MCP servers go.

Spike quality: the TOML filter is line-based (drops `[mcp_servers...]` and
`[[mcp_servers...]]` tables up to the next non-mcp table header). A product
implementation must use a real TOML parser and also handle inline tables.

Usage: compose_codex_home.py SRC DST
"""
import os
import re
import sys

src, dst = map(os.path.abspath, sys.argv[1:3])
os.makedirs(dst, exist_ok=True)
for name in os.listdir(src):
    if name == "config.toml":
        continue
    link = os.path.join(dst, name)
    if not os.path.lexists(link):
        os.symlink(os.path.join(src, name), link)

header = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
out, skipping = [], False
cfg = os.path.join(src, "config.toml")
for line in open(cfg) if os.path.exists(cfg) else []:
    m = header.match(line)
    if m:
        key = m.group(1).strip().strip('"')
        skipping = key == "mcp_servers" or key.startswith("mcp_servers.")
    if not skipping:
        out.append(line)
with open(os.path.join(dst, "config.toml"), "w") as f:
    f.writelines(out)

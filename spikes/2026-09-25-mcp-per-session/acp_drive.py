#!/usr/bin/env python3
"""Drive one ACP session against an adapter and report what the agent sees.

Starts the adapter command given after `--`, runs initialize -> session/new
(with the MCP servers from --mcp) -> one session/prompt, auto-allows
permission requests, and prints a JSON summary: the agent's reply text and
every tool call title it made.

Usage:
  acp_drive.py --cwd DIR [--mcp name=url ...] [--meta JSON] [--load SID] [--prompt TEXT] -- ADAPTER [ARGS...]
"""
import argparse
import json
import subprocess
import sys
import threading
import time

p = argparse.ArgumentParser()
p.add_argument("--cwd", required=True)
p.add_argument("--mcp", action="append", default=[])
p.add_argument("--meta", default=None)
p.add_argument("--load", default=None, help="session id to session/load instead of session/new")
p.add_argument("--timeout", type=float, default=240)
p.add_argument("--prompt", default=(
    "List the exact names of ALL tools available to you whose name contains the "
    "substring 'roostspike' (no other tools). Then call each of those tools once "
    "and print each tool's output verbatim. Do not use any other tool."))
p.add_argument("cmd", nargs=argparse.REMAINDER)
a = p.parse_args()
cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd

proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=open("/tmp/roostspike-adapter-stderr.log", "ab"), text=True, bufsize=1)
lock = threading.Lock()
pending = {}
next_id = [0]
reply_text = []
tool_calls = []


def send(obj):
    with lock:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()


def call(method, params):
    next_id[0] += 1
    i = next_id[0]
    ev = threading.Event()
    pending[i] = [ev, None]
    send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
    if not ev.wait(a.timeout):
        raise TimeoutError(method)
    msg = pending.pop(i)[1]
    if "error" in msg:
        raise RuntimeError(f"{method}: {msg['error']}")
    return msg["result"]


def reader():
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" in msg and "method" not in msg:
            if msg["id"] in pending:
                pending[msg["id"]][1] = msg
                pending[msg["id"]][0].set()
            continue
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "session/update":
            u = params.get("update", {})
            kind = u.get("sessionUpdate")
            if kind == "agent_message_chunk" and u.get("content", {}).get("type") == "text":
                reply_text.append(u["content"]["text"])
            elif kind == "tool_call":
                tool_calls.append(u.get("title"))
        elif method == "session/request_permission":
            opts = params.get("options", [])
            pick = next((o for o in opts if o.get("kind") == "allow_once"), opts[0] if opts else None)
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "outcome": {"outcome": "selected", "optionId": pick["optionId"]} if pick else {"outcome": "cancelled"}}})
        elif "id" in msg:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unsupported in spike"}})


threading.Thread(target=reader, daemon=True).start()

servers = []
for spec in a.mcp:
    name, url = spec.split("=", 1)
    servers.append({"type": "http", "name": name, "url": url,
                    "headers": [{"name": "X-Roost-Probe", "value": name}]})

out = {"cmd": cmd[0].rsplit("/", 1)[-1], "cwd": a.cwd, "session_mcp": [s["name"] for s in servers]}
try:
    init = call("initialize", {"protocolVersion": 1, "clientCapabilities": {
        "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    out["agent"] = init.get("agentInfo")
    out["mcpCapabilities"] = init.get("agentCapabilities", {}).get("mcpCapabilities")
    params = {"cwd": a.cwd, "mcpServers": servers}
    if a.meta:
        params["_meta"] = json.loads(a.meta)
    if a.load:
        params["sessionId"] = a.load
        started = call("session/load", params)
        sid = a.load
    else:
        started = call("session/new", params)
        sid = started["sessionId"]
    models = (started or {}).get("models") or {}
    out["currentModelId"] = models.get("currentModelId")
    out["availableModels"] = [m.get("modelId") for m in models.get("availableModels", [])]
    out["sessionId"] = sid
    t0 = time.time()
    res = call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": a.prompt}]})
    out["stopReason"] = res.get("stopReason")
    out["seconds"] = round(time.time() - t0, 1)
except Exception as e:  # report, never hang
    out["error"] = repr(e)
out["tool_calls"] = tool_calls
out["reply"] = "".join(reply_text)
print(json.dumps(out, indent=2))
proc.terminate()

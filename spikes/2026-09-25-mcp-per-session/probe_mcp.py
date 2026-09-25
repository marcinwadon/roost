#!/usr/bin/env python3
"""Minimal streamable-HTTP MCP server used as a probe.

Exposes one tool, `<name>_probe`, and logs every JSON-RPC method it receives
to stdout as one JSON line. Seeing `tools/list` from a probe proves the agent
loaded that server; seeing `tools/call` proves the tool was usable.

Usage: probe_mcp.py NAME PORT
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME, PORT = sys.argv[1], int(sys.argv[2])
TOOL = f"{NAME}_probe"


def log(**kw):
    print(json.dumps({"t": round(time.time(), 3), "server": NAME, **kw}), flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        # No server-initiated stream; clients fall back to POST-only.
        self.send_response(405)
        self.end_headers()

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
        msgs = body if isinstance(body, list) else [body]
        replies = []
        for m in msgs:
            method = m.get("method")
            log(method=method, ua=self.headers.get("User-Agent"), probe_header=self.headers.get("X-Roost-Probe"))
            if "id" not in m:
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": m.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": NAME, "version": "0.0.1"},
                }
            elif method == "tools/list":
                result = {"tools": [{
                    "name": TOOL,
                    "description": f"Probe tool of the {NAME} server. Returns a fixed marker.",
                    "inputSchema": {"type": "object", "properties": {}},
                }]}
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": f"MARKER-{NAME.upper()}"}]}
            elif method == "ping":
                result = {}
            else:
                replies.append({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not found"}})
                continue
            replies.append({"jsonrpc": "2.0", "id": m["id"], "result": result})
        if not replies:
            self.send_response(202)
            self.end_headers()
            return
        out = json.dumps(replies if isinstance(body, list) else replies[0]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", f"{NAME}-1")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


log(event="listening", port=PORT)
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

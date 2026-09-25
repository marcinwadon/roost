#!/usr/bin/env bash
# run.sh LABEL CWD META_JSON_OR_EMPTY ADAPTER [extra adapter args...]
# Resets probe logs, drives one session with the session probe injected via
# ACP session/new, then prints the driver summary and probe hit counts.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
W="${ROOSTSPIKE_WORK:-/tmp/roostspike}"
label="$1"; cwd="$2"; meta="$3"; shift 3
for f in "$W"/logs/*.log; do : > "$f"; done
args=(--cwd "$cwd" --mcp roostspike_session=http://127.0.0.1:18701/mcp)
[ -n "$meta" ] && args+=(--meta "$meta")
python3 "$here/acp_drive.py" "${args[@]}" -- "$@" > "$W/$label.json" 2>&1
sleep 1
echo "### $label"
python3 -c "import json;d=json.load(open('$W/$label.json'));print('error:',d.get('error'));print('tool_calls:',d['tool_calls']);print('reply:',d['reply'][:700])"
python3 "$here/count.py" "$W"/logs/*.log

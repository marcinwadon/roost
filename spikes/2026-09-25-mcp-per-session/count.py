#!/usr/bin/env python3
"""Summarise probe logs: which methods each probe server received."""
import collections, json, sys
for path in sys.argv[1:]:
    c = collections.Counter()
    for line in open(path):
        try:
            m = json.loads(line).get("method")
        except ValueError:
            continue
        if m:
            c[m] += 1
    print(f"{path.rsplit('/', 1)[-1]:<14} {dict(c) or '-'}")

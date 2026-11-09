#!/usr/bin/env python3
"""Extract the last <svg>...</svg> block from an opencode NDJSON log (or raw text).

Usage: extract_svg.py <log-file> <out.svg>
Exit 0 on success, 1 if no SVG block found.
"""
import json
import re
import sys


def main():
    raw = open(sys.argv[1], 'rb').read().decode('utf-8', 'replace')
    chunks = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get('part') or {}
        if isinstance(part, dict) and part.get('type') == 'text':
            chunks.append(str(part.get('text', '')))
    blob = '\n'.join(chunks) if chunks else raw
    m = re.search(r'<svg\b.*?</svg>', blob, re.S | re.I)
    if not m:
        sys.exit(1)
    with open(sys.argv[2], 'w', encoding='utf-8') as fh:
        fh.write(m.group(0))


if __name__ == '__main__':
    main()

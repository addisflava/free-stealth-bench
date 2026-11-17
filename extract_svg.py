#!/usr/bin/env python3
"""Extract the last <svg>...</svg> block from an opencode NDJSON log (or raw text).

Usage: extract_svg.py <log-file> <out.svg>
Exit 0 on success, 1 if no SVG block found.
"""
import json
import re
import sys


def _text_from_response(obj):
    """Pull assistant text out of OpenAI/Anthropic-style JSON responses."""
    if not isinstance(obj, dict):
        return ''
    choices = obj.get('choices')
    if isinstance(choices, list) and choices:
        msg = choices[0].get('message') if isinstance(choices[0], dict) else None
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return '\n'.join(str(p.get('text', '')) for p in content
                             if isinstance(p, dict) and p.get('text'))
        if isinstance(choices[0], dict) and isinstance(choices[0].get('text'), str):
            return choices[0]['text']
    content = obj.get('content')
    if isinstance(content, list):
        return '\n'.join(str(b.get('text', '')) for b in content
                         if isinstance(b, dict) and b.get('type') == 'text')
    if isinstance(obj.get('output_text'), str):
        return obj['output_text']
    return ''


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
            continue
        if isinstance(ev, dict) and ev.get('type') == 'text' and isinstance(ev.get('text'), str):
            chunks.append(ev['text'])
            continue
        extracted = _text_from_response(ev)
        if extracted:
            chunks.append(extracted)
    blob = '\n'.join(chunks) if chunks else raw
    m = re.search(r'<svg\b.*?</svg>', blob, re.S | re.I)
    if not m:
        sys.exit(1)
    with open(sys.argv[2], 'w', encoding='utf-8') as fh:
        fh.write(m.group(0))


if __name__ == '__main__':
    main()

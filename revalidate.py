#!/usr/bin/env python3
"""Re-run the validator against existing results and update sidecars in place.

Use after validate_output.py fixes (e.g. external-ref detection). Only sidecars
whose SVG file exists are touched; missing/failed runs are left alone.

Usage: python3 revalidate.py [--dry-run]
"""
import glob
import json
import sys

import validate_output
from io import StringIO
import contextlib


def validate_bytes(raw: bytes):
    """Reuse validate_output.main() by feeding it a temp file path."""
    import tempfile, os
    with tempfile.NamedTemporaryFile('wb', suffix='.svg', delete=False) as fh:
        fh.write(raw)
        tmp = fh.name
    buf = StringIO()
    argv0, sys.argv = sys.argv[0], ['validate_output.py', tmp]
    try:
        with contextlib.redirect_stdout(buf):
            validate_output.main()
    finally:
        sys.argv = [argv0]
        os.unlink(tmp)
    out = json.loads(buf.getvalue())
    out.pop('file', None)
    return out


def main():
    dry = '--dry-run' in sys.argv[1:]
    changed = 0
    for p in sorted(glob.glob('results/*/*.json')):
        if '/summary-' in p or p.startswith('results/summary-'):
            continue
        try:
            rec = json.load(open(p, encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        svg = p[:-5] + '.svg'
        try:
            raw = open(svg, 'rb').read()
        except OSError:
            continue
        v = validate_bytes(raw)
        old = json.dumps(rec.get('validation'), sort_keys=True)
        rec['validation'] = v
        ok = bool(v.get('exists')) and bool(v.get('well_formed_xml')) and bool(v.get('root_is_svg'))
        rec['result_status'] = 'ok' if ok else 'invalid_svg'
        if json.dumps(old, sort_keys=True) != json.dumps(v, sort_keys=True):
            changed += 1
            if not dry:
                with open(p, 'w', encoding='utf-8') as fh:
                    json.dump(rec, fh, indent=2)
    print(f"revalidate: {changed} sidecar(s) {'would be ' if dry else ''}updated")


if __name__ == '__main__':
    main()

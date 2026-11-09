#!/usr/bin/env python3
"""Write the metadata sidecar for one run.

All values come from argv / environment / files — nothing is string-interpolated
into this source, so log contents can never break the parser.

Usage:
  record_run.py <out.svg> <log.ndjson> <validation.json|->
Env:
  TASK MODEL VARIANT EFFORT EFFORT_REQUESTED EFFORT_FALLBACK
  DATE_UTC OPENCODE_VERSION PROMPT_FILE PROMPT_SHA256
  WALL_SECONDS EXIT_CODE TIMED_OUT METHOD
"""
import datetime
import json
import os
import sys


def env(name, default=''):
    return os.environ.get(name, default)


def parse_log(log_path):
    stats = {'events': 0, 'steps': 0, 'session_id': None, 'error': None,
             'tokens': None}
    totals = {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'total': 0}
    saw_tokens = False
    try:
        fh = open(log_path, 'rb')
    except OSError:
        return stats
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line.startswith(b'{'):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            stats['events'] += 1
            if ev.get('sessionID'):
                stats['session_id'] = ev['sessionID']
            if ev.get('type') == 'error':
                err = ev.get('error') or {}
                stats['error'] = {'type': err.get('type'),
                                  'message': str(err.get('message') or '')[:500]}
            part = ev.get('part')
            if not isinstance(part, dict):
                continue
            if part.get('type') in ('step-start', 'step-finish'):
                stats['steps'] += 1
            tok = part.get('tokens') or part.get('usage')
            if isinstance(tok, dict):
                saw_tokens = True
                cache = tok.get('cache') if isinstance(tok.get('cache'), dict) else {}
                for k in ('input', 'output', 'reasoning', 'total'):
                    try:
                        totals[k] += int(tok.get(k) or 0)
                    except (TypeError, ValueError):
                        pass
                try:
                    totals['cache_read'] += int(tok.get('cache_read') or tok.get('cacheRead')
                                                 or cache.get('read') or 0)
                except (TypeError, ValueError):
                    pass
    if saw_tokens:
        totals['total'] = totals['total'] or (totals['input'] + totals['output'])
        stats['tokens'] = totals
    stats['steps'] //= 2
    return stats


def main():
    out_path, log_path, validation_path = sys.argv[1], sys.argv[2], sys.argv[3]
    validation = None
    if validation_path != '-':
        try:
            validation = json.load(open(validation_path, encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            validation = None

    stats = parse_log(log_path)
    method = env('METHOD')
    timed_out = env('TIMED_OUT') == '1'
    record = {
        'benchmark': 'svg-bench',
        'task': env('TASK'),
        'model': env('MODEL'),
        'variant': env('VARIANT') or None,
        'effort': env('EFFORT'),
        'effort_requested': env('EFFORT_REQUESTED'),
        'effort_fallback': env('EFFORT_FALLBACK') == '1',
        'date_utc': env('DATE_UTC'),
        'opencode_version': env('OPENCODE_VERSION'),
        'prompt_file': env('PROMPT_FILE'),
        'prompt_sha256': env('PROMPT_SHA256'),
        'wall_seconds': int(env('WALL_SECONDS', '0') or 0),
        'exit_code': int(env('EXIT_CODE', '0') or 0),
        'timed_out': timed_out,
        'collection_method': method,
        'svg_file': out_path,
        'raw_log': log_path,
        'validation': validation,
        'tokens': stats['tokens'],
        'steps': stats['steps'] or None,
        'session_id': stats['session_id'],
        'events': stats['events'],
        'error_type': (stats['error'] or {}).get('type'),
        'error_message': (stats['error'] or {}).get('message'),
        'result_status': None,
    }
    if method != 'missing':
        v = validation or {}
        ok = bool(v.get('exists')) and bool(v.get('well_formed_xml')) and bool(v.get('root_is_svg'))
        record['result_status'] = 'ok' if ok else 'invalid_svg'
    else:
        record['result_status'] = 'timeout' if timed_out else 'no_output'

    sidecar = out_path[:-4] + '.json' if out_path.endswith('.svg') else out_path + '.json'
    with open(sidecar, 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2)


if __name__ == '__main__':
    main()

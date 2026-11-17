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
  RUN_META PROVIDER HARNESS AGENT_VERSION

RUN_META is written by runner.py and carries the provider/harness, normalized
token usage and the effective effort. Environment values still win for the
fields run.sh owns (task, prompt hash, fallback flag).
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
    meta = {}
    meta_path = env('RUN_META')
    if meta_path:
        try:
            meta = json.load(open(meta_path, encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    # RUN_META is canonical: MODEL in the environment is the *target* (which
    # may be harness@provider/model), while the sidecar needs the bare
    # provider/model so the leaderboard can group harness variants together.
    model = str(meta.get('model') or '') or env('MODEL')
    provider = str(meta.get('provider') or '') or env('PROVIDER')
    if not provider and '/' in model:
        provider = model.split('/', 1)[0]
    harness = str(meta.get('harness') or '') or env('HARNESS') or provider
    if not meta and '@' in model:
        # No RUN_META (runner crashed): recover a harness-qualified target.
        h, rest = model.split('@', 1)
        if '/' in rest:
            harness = h
            model = rest
            provider = rest.split('/', 1)[0]
    agent_version = env('AGENT_VERSION') or str(meta.get('agent_version') or '')
    method = str(meta.get('collection_method') or env('METHOD'))
    timed_out = env('TIMED_OUT') == '1' or bool(meta.get('timed_out'))
    wall_seconds = meta.get('wall_seconds')
    if wall_seconds is None:
        wall_seconds = env('WALL_SECONDS', '0') or 0
    exit_code = meta.get('exit_code')
    if exit_code is None:
        exit_code = env('EXIT_CODE', '0') or 0
    tokens = meta.get('tokens') if meta.get('tokens') is not None else stats['tokens']
    steps = meta.get('steps') if meta.get('steps') is not None else (stats['steps'] or None)
    error_type = meta.get('error_type') or (stats['error'] or {}).get('type')
    error_message = meta.get('error_message') or (stats['error'] or {}).get('message')
    record = {
        'benchmark': 'free-stealth-bench',
        'task': env('TASK'),
        'provider': provider or None,
        'harness': harness or None,
        'target': str(meta.get('target') or '') or None,
        'model': model,
        'model_id': (str(meta.get('model_id') or '')
                     or (model.split('/', 1)[1] if '/' in model else model)),
        'variant': env('VARIANT') or None,
        'effort': env('EFFORT') or str(meta.get('effort') or ''),
        'effort_requested': env('EFFORT_REQUESTED') or str(meta.get('effort_requested') or ''),
        'effort_fallback': env('EFFORT_FALLBACK') == '1',
        'date_utc': env('DATE_UTC'),
        'opencode_version': (agent_version if harness == 'opencode'
                             else env('OPENCODE_VERSION')) or None,
        'agent_version': agent_version or None,
        'prompt_file': env('PROMPT_FILE'),
        'prompt_sha256': env('PROMPT_SHA256'),
        'wall_seconds': int(wall_seconds or 0),
        'exit_code': int(exit_code or 0),
        'timed_out': timed_out,
        'collection_method': method,
        'svg_file': out_path,
        'raw_log': log_path,
        'validation': validation,
        'tokens': tokens,
        'steps': steps,
        'session_id': meta.get('session_id') or stats['session_id'],
        'events': int(meta.get('events') or stats['events'] or 0),
        'error_type': error_type,
        'error_message': error_message,
        'result_status': None,
    }
    if method != 'missing':
        v = validation or {}
        ok = bool(v.get('exists')) and bool(v.get('well_formed_xml')) and bool(v.get('root_is_svg'))
        if ok and v.get('canvas_offcanvas'):
            # The file is valid XML, but a transform animation on the root
            # <svg> slides the whole scene out of the viewBox, so nothing is
            # visible. That is not a usable illustration.
            record['result_status'] = 'off_canvas'
        else:
            record['result_status'] = 'ok' if ok else 'invalid_svg'
    else:
        record['result_status'] = 'timeout' if timed_out else 'no_output'

    sidecar = out_path[:-4] + '.json' if out_path.endswith('.svg') else out_path + '.json'
    with open(sidecar, 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2)


if __name__ == '__main__':
    main()

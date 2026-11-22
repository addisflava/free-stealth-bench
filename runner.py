#!/usr/bin/env python3
"""Provider- and harness-agnostic runner for free-stealth-bench.

Providers and agent harnesses are declared in providers.yaml / harnesses.yaml.
This module discovers models, resolves effort, invokes the configured harness,
collects the SVG, and writes a normalized meta file for record_run.py.

Subcommands:
  list                 show providers and harnesses (and whether they are enabled)
  discover             print one target per line: provider/model (or harness@provider/model)
  effort               print the effort that will actually be used for a target
  run                  run one task for one target
  call                 one-shot text call (used by judge.py)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_FILE = os.environ.get('PROVIDERS_FILE') or os.environ.get('BENCH_PROVIDERS_FILE') \
    or os.path.join(ROOT, 'providers.yaml')
HARNESSES_FILE = os.environ.get('HARNESSES_FILE') or os.environ.get('BENCH_HARNESSES_FILE') \
    or os.path.join(ROOT, 'harnesses.yaml')
EXTRACT = os.path.join(ROOT, 'extract_svg.py')

# Transient provider failures get a bounded number of retries inside one run.
# A stalled agent (no text/tool call/completion) is killed by the no-progress
# watchdog instead of consuming the whole wall-clock budget.
DEFAULT_RETRIES = max(0, int(os.environ.get('BENCH_RETRIES', '2')))
DEFAULT_RETRY_DELAY = max(0.0, float(os.environ.get('BENCH_RETRY_DELAY', '5')))
DEFAULT_NO_PROGRESS_TIMEOUT = max(0, int(os.environ.get('BENCH_NO_PROGRESS_TIMEOUT', '240')))

TARGET_RE = re.compile(r'^(?:(?P<harness>[a-z0-9][a-z0-9._-]*)@)?'
                       r'(?P<provider>[a-z0-9][a-z0-9._-]*)/'
                       r'(?P<model>[A-Za-z0-9][A-Za-z0-9._:/+~#@-]{0,190})$')


def die(msg, code=4):
    print(f'runner: {msg}', file=sys.stderr)
    raise SystemExit(code)


def load_yaml(path, what):
    try:
        import yaml
    except ImportError:
        die('PyYAML is required (pip install pyyaml)')
    try:
        with open(path, encoding='utf-8') as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as e:
        die(f'cannot read {what} registry {path}: {e}')
    except Exception as e:  # noqa: BLE001 - surface YAML errors plainly
        die(f'cannot parse {what} registry {path}: {e}')
    if not isinstance(data, dict):
        die(f'{what} registry {path} must contain a mapping')
    return data


class Config:
    def __init__(self):
        self.providers_doc = load_yaml(PROVIDERS_FILE, 'provider')
        self.harnesses_doc = load_yaml(HARNESSES_FILE, 'harness')
        self.providers = self.providers_doc.get('providers') or {}
        self.harnesses = self.harnesses_doc.get('harnesses') or {}
        self.defaults = self.providers_doc.get('defaults') or {}
        if not isinstance(self.providers, dict) or not self.providers:
            die(f'no providers defined in {PROVIDERS_FILE}')
        if not isinstance(self.harnesses, dict) or not self.harnesses:
            die(f'no harnesses defined in {HARNESSES_FILE}')

    # ── target resolution ───────────────────────────────────────────────────
    def resolve(self, target):
        target = (target or '').strip()
        m = TARGET_RE.match(target)
        if not m:
            die(f'invalid target {target!r}; expected provider/model or harness@provider/model')
        pid = m.group('provider')
        model = m.group('model')
        provider = self.providers.get(pid)
        if not isinstance(provider, dict):
            die(f'unknown provider {pid!r} (add it to {PROVIDERS_FILE})')
        default_harness = provider.get('default_harness') or provider.get('harness')
        hid = m.group('harness') or default_harness
        if not hid:
            die(f'provider {pid!r} has no harness configured')
        harness = self.harnesses.get(hid)
        if not isinstance(harness, dict):
            die(f'unknown harness {hid!r} (add it to {HARNESSES_FILE})')
        alternates = provider.get('alternate_harnesses') or []
        if m.group('harness') and hid != default_harness and hid not in alternates:
            # Be permissive for explicit targets, but warn so typos surface.
            print(f'runner: warning: harness {hid!r} is not default/alternate for {pid!r}',
                  file=sys.stderr)
        canonical = target if m.group('harness') else f'{pid}/{model}'
        return {
            'target': canonical,
            'provider_id': pid,
            'provider': provider,
            'harness_id': hid,
            'harness': harness,
            'model_id': model,
            'model': f'{pid}/{model}',
        }

    def enabled(self, pid, provider):
        e = provider.get('enabled', 'auto')
        if e is True:
            return True
        if e is False:
            return False
        key_env = provider.get('api_key_env')
        if key_env:
            return bool(os.environ.get(key_env))
        return True

    def default_effort(self, provider):
        return str((provider.get('effort') or {}).get('default')
                   or self.defaults.get('effort') or 'high')

    def resolve_effort(self, t, requested=None):
        provider, harness = t['provider'], t['harness']
        requested = str(requested or self.default_effort(provider)).strip()
        if requested == 'default':
            return 'default'
        policy = provider.get('effort') or {}
        unsupported = {str(x) for x in (policy.get('unsupported_models') or [])}
        if t['model_id'] in unsupported:
            return 'default'
        style = harness.get('effort_style', 'none')
        if style == 'none':
            return 'default'
        if style == 'flag' and not harness.get('effort_flag'):
            return 'default'
        return requested


# ── discovery ───────────────────────────────────────────────────────────────
def _prefix(pid, line):
    line = line.strip()
    if not line:
        return ''
    if line.startswith(pid + '/'):
        return line
    return f'{pid}/{line}'


def _filter_targets(pid, lines, discover):
    inc = discover.get('include')
    exc = discover.get('exclude')
    out, seen = [], set()
    for line in lines:
        target = _prefix(pid, line)
        if not target or not TARGET_RE.match(target):
            continue
        if inc and not re.search(str(inc), target):
            continue
        if exc and re.search(str(exc), target):
            continue
        if target in seen:
            continue
        seen.add(target)
        out.append(target)
    return out


def _http_json(url, headers, timeout, method='GET', body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def discover_provider(pid, provider):
    d = provider.get('discover') or {}
    kind = d.get('type', 'none')
    if kind == 'none':
        return []
    if kind == 'static':
        return _filter_targets(pid, d.get('models') or [], d)
    if kind == 'command':
        cmd = d.get('command')
        if not cmd:
            return []
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f'runner: discovery for {pid} failed: {e}', file=sys.stderr)
            return []
        if proc.returncode != 0:
            print(f'runner: discovery for {pid} exited {proc.returncode}', file=sys.stderr)
        return _filter_targets(pid, proc.stdout.splitlines(), d)
    if kind == 'openai-models':
        base = str(provider.get('base_url') or '').rstrip('/')
        if not base:
            return []
        headers = {'Accept': 'application/json'}
        key_env = provider.get('api_key_env')
        key = os.environ.get(key_env, '') if key_env else ''
        if key:
            headers['Authorization'] = f'Bearer {key}'
        headers.update({str(k): str(v) for k, v in (provider.get('headers') or {}).items()})
        try:
            status, raw = _http_json(base + '/models', headers, 25)
        except Exception as e:  # noqa: BLE001 - discovery must never abort a run
            print(f'runner: model discovery for {pid} failed: {e}', file=sys.stderr)
            return []
        if status >= 400:
            print(f'runner: model discovery for {pid} returned HTTP {status}', file=sys.stderr)
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            print(f'runner: model discovery for {pid} returned non-JSON', file=sys.stderr)
            return []
        models = []
        for item in (data.get('data') or data.get('models') or []):
            if isinstance(item, dict):
                mid = item.get('id') or item.get('name')
                if mid:
                    models.append(str(mid))
            elif isinstance(item, str):
                models.append(item)
        return _filter_targets(pid, models, d)
    print(f'runner: unknown discover.type {kind!r} for {pid}', file=sys.stderr)
    return []


def discover_all(cfg, only_provider=None, all_harnesses=False, include_disabled=False):
    targets = []
    for pid, provider in cfg.providers.items():
        if not isinstance(provider, dict):
            continue
        if only_provider and pid != only_provider:
            continue
        if not include_disabled and not cfg.enabled(pid, provider):
            continue
        models = discover_provider(pid, provider)
        default_h = provider.get('default_harness') or provider.get('harness')
        alternates = [h for h in (provider.get('alternate_harnesses') or []) if h]
        for target in models:
            targets.append(target)
            if all_harnesses:
                for h in alternates:
                    if h != default_h:
                        targets.append(f'{h}@{target}')
    # de-duplicate, stable order
    seen, out = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── process helpers ─────────────────────────────────────────────────────────
# Events that mean the agent actually did something. A bare `step-start` is
# deliberately excluded: hung OpenCode sessions emit those over and over with
# no text/tool call, which used to burn the whole wall-clock budget.
PROGRESS_EVENT_TYPES = frozenset({'text', 'tool_use', 'step_finish', 'error',
                                  'result', 'message', 'thinking'})

# Provider errors that cannot be fixed by retrying the same request.
TERMINAL_ERROR_TYPES = frozenset({
    'config', 'provider.auth', 'provider.quota', 'provider.no-route',
    'provider.not-found', 'provider.permission',
})
TERMINAL_ERROR_TEXT = (
    'not available in your country',
    'free usage limit reached',
    'variant unavailable',
    'you must provide a message',
    'api key',
    'unauthorized',
)


def is_terminal_error(result):
    """True when another attempt of the same request cannot possibly help."""
    if result.get('effort_rejected'):
        return True
    kind = str(result.get('error_type') or '').lower()
    if kind in TERMINAL_ERROR_TYPES:
        return True
    msg = str(result.get('error_message') or '').lower()
    return any(snippet in msg for snippet in TERMINAL_ERROR_TEXT)


def _kill_group(proc):
    """Terminate a child process group, escalating to SIGKILL if needed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _new_log_progress(log_path, offset):
    """Return (saw_progress, new_offset) for bytes appended since `offset`."""
    try:
        with open(log_path, 'rb') as fh:
            fh.seek(offset)
            chunk = fh.read()
            new_offset = fh.tell()
    except OSError:
        return False, offset
    saw = False
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith(b'{'):
            # Plain-text harnesses stream non-NDJSON output: any content is
            # evidence of progress.
            saw = True
            continue
        try:
            ev = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if ev.get('type') in PROGRESS_EVENT_TYPES:
            saw = True
    return saw, new_offset


def spawn_logged(cmd, cwd, log_path, timeout, no_progress_timeout=0):
    """Run argv with stdout+stderr captured to log_path.

    Returns (exit_code, timed_out, stalled). `stalled` marks a kill caused by
    no progress (as opposed to hitting the overall wall-clock timeout).
    """
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    # Some harnesses (OpenCode) read PWD from the environment instead of the
    # process cwd, so keep it in sync or tools run in the caller's directory.
    env = os.environ.copy()
    env['PWD'] = os.path.abspath(cwd)
    with open(log_path, 'wb') as log:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env=env)
        except OSError as e:
            log.write(f'runner: failed to start {cmd[0]!r}: {e}\n'.encode())
            return 127, False, False
        # If the runner is interrupted, take the child (and its private
        # opencode server) down with it instead of leaving an orphan.
        previous = {}

        def _terminate(signum, _frame):
            _kill_group(proc)
            raise SystemExit(128 + signum)

        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        last_progress = time.monotonic()
        offset = 0
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                previous[sig] = signal.signal(sig, _terminate)
            while True:
                code = proc.poll()
                if code is not None:
                    return code, False, False
                progressed, offset = _new_log_progress(log_path, offset)
                now = time.monotonic()
                if progressed:
                    last_progress = now
                if deadline is not None and now >= deadline:
                    _kill_group(proc)
                    return 124, True, False
                if (no_progress_timeout and no_progress_timeout > 0
                        and now - last_progress >= no_progress_timeout):
                    _kill_group(proc)
                    return 124, True, True
                time.sleep(0.5)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def agent_version(harness):
    cmd = harness.get('version_command')
    if cmd:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            text = (out.stdout or out.stderr or '').strip()
            if text:
                first = text.splitlines()[0].strip()
                parts = first.split()
                return (parts[-1] if parts else first)[:120]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return str(harness.get('version') or harness.get('type') or '')[:120]


def placeholder_map(t, cfg, prompt, prompt_file, workdir, out_name, timeout, effort):
    return {
        '{provider}': t['provider_id'],
        '{model}': t['model_id'],
        '{target}': t['target'],
        '{harness}': t['harness_id'],
        '{effort}': effort,
        '{prompt}': prompt,
        '{prompt_file}': prompt_file,
        '{workdir}': workdir,
        '{output}': out_name,
        '{timeout}': str(timeout),
    }


def substitute(arg, mapping):
    for k, v in mapping.items():
        if k in arg:
            arg = arg.replace(k, v)
    return arg


def write_ndjson(log_path, text, tokens=None, error=None):
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, 'w', encoding='utf-8') as fh:
        if text:
            fh.write(json.dumps({'type': 'text',
                                 'part': {'type': 'text', 'text': text}}) + '\n')
        if tokens:
            fh.write(json.dumps({'type': 'step-finish',
                                 'part': {'type': 'step-finish', 'tokens': tokens}}) + '\n')
        if error:
            fh.write(json.dumps({'type': 'error', 'error': {
                'type': error.get('type'), 'message': error.get('message')}}) + '\n')


def collect_output(workdir, out_name, log_path, out_path):
    names = [out_name, 'output.svg']
    for name in names:
        cand = os.path.join(workdir, name)
        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            shutil.copyfile(cand, out_path)
            return 'output.svg'
    svgs = [f for f in glob.glob(os.path.join(workdir, '*.svg'))
            if os.path.getsize(f) > 0]
    if len(svgs) == 1:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        shutil.copyfile(svgs[0], out_path)
        return 'output.svg'
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    try:
        proc = subprocess.run([sys.executable, EXTRACT, log_path, out_path],
                              capture_output=True, timeout=60)
        if proc.returncode == 0 and os.path.getsize(out_path) > 0:
            return 'extracted'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'missing'


# ── adapters ────────────────────────────────────────────────────────────────
def run_opencode(t, cfg, prompt, prompt_file, workdir, log_path, timeout, effort,
                 no_progress_timeout=0):
    mapping = placeholder_map(t, cfg, prompt, prompt_file, workdir,
                              cfg.defaults.get('output', 'output.svg'), timeout, effort)
    if effort != 'default':
        # OpenCode's variant syntax is part of the model argument.
        mapping['{target}'] = f"{t['target']}#{effort}"
    cmd = [substitute(str(a), mapping) for a in (t['harness'].get('command') or [])]
    if not cmd:
        die(f"harness {t['harness_id']!r} has no command")
    code, timed_out, stalled = spawn_logged(cmd, workdir, log_path, timeout,
                                            no_progress_timeout)
    stats = parse_log_lenient(log_path)
    error = stats['error'] or {}
    if stalled and not error.get('type'):
        error = {'type': 'no_progress',
                 'message': 'agent produced no text, tool call or completion'}
    return {'exit_code': code, 'timed_out': timed_out, 'stalled': stalled,
            'tokens': stats['tokens'],
            'steps': stats['steps'] or None, 'session_id': stats['session_id'],
            'events': stats['events'],
            'error_type': error.get('type'),
            'error_message': error.get('message'),
            'effort_rejected': False}


def _openai_text(data):
    if not isinstance(data, dict):
        return ''
    choices = data.get('choices') or []
    if choices and isinstance(choices[0], dict):
        c = choices[0]
        msg = c.get('message') or {}
        content = msg.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return '\n'.join(str(p.get('text', '')) for p in content
                             if isinstance(p, dict) and p.get('text'))
        if isinstance(c.get('text'), str):
            return c['text']
    if isinstance(data.get('output_text'), str):
        return data['output_text']
    return ''


def _openai_tokens(usage):
    if not isinstance(usage, dict):
        return None
    def n(*keys):
        for k in keys:
            try:
                v = usage.get(k)
                if v is not None:
                    return int(v)
            except (TypeError, ValueError):
                pass
        return 0
    tok = {
        'input': n('prompt_tokens', 'input_tokens'),
        'output': n('completion_tokens', 'output_tokens'),
        'reasoning': n('reasoning_tokens'),
        'cache_read': n('cached_tokens'),
    }
    tok['total'] = n('total_tokens') or (tok['input'] + tok['output'])
    return tok if any(tok.values()) else None


def _anthropic_text(data):
    if not isinstance(data, dict):
        return ''
    parts = []
    for block in (data.get('content') or []):
        if isinstance(block, dict) and block.get('type') == 'text':
            parts.append(str(block.get('text', '')))
    if not parts and isinstance(data.get('completion'), str):
        parts.append(data['completion'])
    return '\n'.join(parts)


def _anthropic_tokens(usage):
    if not isinstance(usage, dict):
        return None
    def n(k):
        try:
            return int(usage.get(k) or 0)
        except (TypeError, ValueError):
            return 0
    tok = {
        'input': n('input_tokens'),
        'output': n('output_tokens'),
        'reasoning': n('reasoning_tokens'),
        'cache_read': n('cache_read_input_tokens'),
    }
    tok['total'] = tok['input'] + tok['output']
    return tok if any(tok.values()) else None


def _api_headers(provider, harness):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    key_env = provider.get('api_key_env')
    key = os.environ.get(key_env, '') if key_env else ''
    if key:
        headers['Authorization'] = f'Bearer {key}'
    headers.update({str(k): str(v) for k, v in (provider.get('headers') or {}).items()})
    return headers


def _effort_rejected(status, raw, harness, effort):
    if effort == 'default' or harness.get('effort_style') != 'reasoning_effort':
        return False
    return status in (400, 422) and bool(re.search(
        r'reasoning[._ ]?effort|effort|variant|unsupported', raw or '', re.I))


def run_openai(t, cfg, prompt, prompt_file, workdir, log_path, timeout, effort,
               no_progress_timeout=0):
    provider, harness = t['provider'], t['harness']
    base = str(provider.get('base_url') or '').rstrip('/')
    if not base:
        write_ndjson(log_path, '', error={'type': 'config', 'message': 'provider has no base_url'})
        return {'exit_code': 1, 'timed_out': False, 'tokens': None, 'steps': None,
                'session_id': None, 'events': 1, 'error_type': 'config',
                'error_message': 'provider has no base_url', 'effort_rejected': False}
    url = base + str(harness.get('path') or '/chat/completions')
    headers = _api_headers(provider, harness)
    body = {'model': t['model_id'],
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': False}
    body.update(provider.get('params') or {})
    if effort != 'default' and harness.get('effort_style') == 'reasoning_effort':
        body['reasoning_effort'] = effort
    start = time.time()
    status, raw, timed_out, err = 0, '', False, None
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode('utf-8', 'replace')
        err = f'HTTP {status}'
    except Exception as e:  # noqa: BLE001 - timeouts and connection failures both land here
        timed_out = isinstance(e, TimeoutError) or 'timed out' in str(e).lower()
        err = 'timeout' if timed_out else str(e)
    wall = max(0, int(time.time() - start))
    text, tokens, error = '', None, None
    if not err:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
            text = raw  # plain-text endpoint or proxy error page
        if isinstance(data, dict):
            text = _openai_text(data)
            tokens = _openai_tokens(data.get('usage'))
            if data.get('error'):
                error = data['error']
    if err or not text:
        error = error or {'type': 'http' if err else 'no_text',
                          'message': (err or 'empty completion') + (
                              f': {raw[:300]}' if raw and not text else '')}
    exit_code = 0 if text else (124 if timed_out else 1)
    write_ndjson(log_path, text, tokens, error)
    rejected = _effort_rejected(status, raw, harness, effort)
    if rejected:
        exit_code = 3
    return {'exit_code': exit_code, 'timed_out': timed_out, 'tokens': tokens,
            'steps': None, 'session_id': None, 'events': 1,
            'error_type': (error or {}).get('type'),
            'error_message': (error or {}).get('message'),
            'effort_rejected': rejected, 'wall_seconds': wall}


def run_anthropic(t, cfg, prompt, prompt_file, workdir, log_path, timeout, effort,
                  no_progress_timeout=0):
    provider, harness = t['provider'], t['harness']
    base = str(provider.get('base_url') or '').rstrip('/')
    if not base:
        write_ndjson(log_path, '', error={'type': 'config', 'message': 'provider has no base_url'})
        return {'exit_code': 1, 'timed_out': False, 'tokens': None, 'steps': None,
                'session_id': None, 'events': 1, 'error_type': 'config',
                'error_message': 'provider has no base_url', 'effort_rejected': False}
    url = base + str(harness.get('path') or '/messages')
    key_env = provider.get('api_key_env')
    key = os.environ.get(key_env, '') if key_env else ''
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
               'anthropic-version': str(provider.get('anthropic_version') or '2023-06-01')}
    if key:
        headers['x-api-key'] = key
    headers.update({str(k): str(v) for k, v in (provider.get('headers') or {}).items()})
    body = {'model': t['model_id'], 'max_tokens': 16000,
            'messages': [{'role': 'user', 'content': prompt}]}
    body.update(provider.get('params') or {})
    start = time.time()
    status, raw, timed_out, err = 0, '', False, None
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                     headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode('utf-8', 'replace')
        err = f'HTTP {status}'
    except Exception as e:  # noqa: BLE001
        timed_out = isinstance(e, TimeoutError) or 'timed out' in str(e).lower()
        err = 'timeout' if timed_out else str(e)
    wall = max(0, int(time.time() - start))
    text, tokens, error = '', None, None
    if not err:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
            text = raw
        if isinstance(data, dict):
            text = _anthropic_text(data)
            tokens = _anthropic_tokens(data.get('usage'))
            if data.get('error'):
                error = data['error']
    if err or not text:
        error = error or {'type': 'http' if err else 'no_text',
                          'message': (err or 'empty completion') + (
                              f': {raw[:300]}' if raw and not text else '')}
    exit_code = 0 if text else (124 if timed_out else 1)
    write_ndjson(log_path, text, tokens, error)
    return {'exit_code': exit_code, 'timed_out': timed_out, 'tokens': tokens,
            'steps': None, 'session_id': None, 'events': 1,
            'error_type': (error or {}).get('type'),
            'error_message': (error or {}).get('message'),
            'effort_rejected': False, 'wall_seconds': wall}


def run_command(t, cfg, prompt, prompt_file, workdir, log_path, timeout, effort,
                no_progress_timeout=0):
    mapping = placeholder_map(t, cfg, prompt, prompt_file, workdir,
                              cfg.defaults.get('output', 'output.svg'), timeout, effort)
    cmd = [substitute(str(a), mapping) for a in (t['harness'].get('command') or [])]
    if not cmd:
        die(f"harness {t['harness_id']!r} has no command")
    if effort != 'default' and t['harness'].get('effort_style') == 'flag':
        cmd += [substitute(str(a), mapping) for a in (t['harness'].get('effort_flag') or [])]
    code, timed_out, stalled = spawn_logged(cmd, workdir, log_path, timeout,
                                            no_progress_timeout)
    text = ''
    try:
        with open(log_path, 'rb') as fh:
            text = fh.read().decode('utf-8', 'replace')
    except OSError:
        pass
    effort_rejected = bool(
        effort != 'default' and code != 0 and re.search(
            r'variant|effort|reasoning[._ ]?level|unknown option|unsupported',
            text, re.I))
    if stalled:
        error_type, error_message = 'no_progress', 'agent produced no output'
    else:
        error_type = ('timeout' if timed_out else (None if code == 0 else f'exit_{code}'))
        error_message = '' if code == 0 else text[-400:]
    return {'exit_code': code, 'timed_out': timed_out, 'stalled': stalled,
            'tokens': None, 'steps': None, 'session_id': None, 'events': 0,
            'error_type': error_type, 'error_message': error_message,
            'effort_rejected': effort_rejected}


ADAPTERS = {
    'opencode': run_opencode,
    'openai': run_openai,
    'anthropic': run_anthropic,
    'command': run_command,
}


def parse_log_lenient(path):
    """Reuse record_run's OpenCode NDJSON parser when importable."""
    try:
        from record_run import parse_log
        return parse_log(path)
    except Exception:  # noqa: BLE001 - never let stats parsing break a run
        return {'events': 0, 'steps': 0, 'session_id': None, 'error': None, 'tokens': None}


# ── commands ────────────────────────────────────────────────────────────────
def cmd_list(args):
    cfg = Config()
    rows = []
    for pid, p in cfg.providers.items():
        if not isinstance(p, dict):
            continue
        enabled = cfg.enabled(pid, p)
        default_h = p.get('default_harness') or p.get('harness')
        rows.append({'provider': pid, 'enabled': enabled, 'harness': default_h,
                     'alternates': p.get('alternate_harnesses') or [],
                     'label': p.get('label') or pid,
                     'key_env': p.get('api_key_env') or '',
                     'discover': (p.get('discover') or {}).get('type', 'none')})
    if args.json:
        print(json.dumps({'providers': rows,
                          'harnesses': sorted(cfg.harnesses.keys())}, indent=2))
        return
    print('harnesses: ' + ', '.join(sorted(cfg.harnesses.keys())))
    print('providers:')
    for r in rows:
        state = 'enabled ' if r['enabled'] else 'disabled'
        alt = ('  alt=' + ','.join(r['alternates'])) if r['alternates'] else ''
        key = f"  key={r['key_env']}" if r['key_env'] else '  keyless'
        print(f"  {r['provider']:<16} {state}  harness={r['harness']}{alt}"
              f"  discover={r['discover']}{key}")


def cmd_discover(args):
    cfg = Config()
    if args.provider and args.provider not in cfg.providers:
        die(f'unknown provider {args.provider!r}')
    targets = discover_all(cfg, only_provider=args.provider,
                           all_harnesses=args.all_harnesses,
                           include_disabled=args.all)
    for t in targets:
        print(t)


def cmd_effort(args):
    cfg = Config()
    t = cfg.resolve(args.target)
    print(cfg.resolve_effort(t, args.requested))


def cmd_run(args):
    cfg = Config()
    t = cfg.resolve(args.target)
    harness = t['harness']
    adapter = ADAPTERS.get(str(harness.get('type')))
    if not adapter:
        die(f"harness {t['harness_id']!r} has unknown type {harness.get('type')!r}")
    prompt = args.prompt
    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding='utf-8') as fh:
                prompt = fh.read()
        except OSError as e:
            die(f'cannot read prompt file: {e}')
    if not prompt:
        die('prompt is empty (use --prompt or --prompt-file)')
    effort = cfg.resolve_effort(t, args.effort)
    timeout = int(args.timeout or cfg.defaults.get('timeout') or 900)
    retries = max(0, int(args.retries if args.retries is not None else DEFAULT_RETRIES))
    retry_delay = (float(args.retry_delay) if args.retry_delay is not None
                   else DEFAULT_RETRY_DELAY)
    no_progress = (int(args.no_progress_timeout) if args.no_progress_timeout is not None
                   else DEFAULT_NO_PROGRESS_TIMEOUT)
    attempt_timeout_arg = max(0, int(args.attempt_timeout or 0))
    out_name = str(t['provider'].get('output') or cfg.defaults.get('output') or 'output.svg')
    out_path = os.path.abspath(args.out)
    workdir = tempfile.mkdtemp(prefix='free-stealth-bench-')
    log_path = os.path.abspath(args.log or os.path.join(workdir, 'run.ndjson'))
    meta_path = os.path.abspath(args.meta or (log_path + '.meta.json'))

    # Never let a previous invocation of this (task, model, effort, date) leak
    # into this one: a stale SVG would be validated as this run's output, and a
    # stale meta would be read by run.sh if this process is interrupted.
    for stale in (out_path, meta_path):
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass

    start = time.time()
    result, method, retry_errors, attempts = None, 'missing', [], 0
    try:
        for index in range(retries + 1):
            elapsed = int(time.time() - start)
            remaining = timeout - elapsed
            if index > 0 and remaining <= 0:
                break
            attempt_timeout = attempt_timeout_arg or (remaining if timeout > 0 else 0)
            if attempt_timeout_arg and timeout > 0:
                attempt_timeout = min(attempt_timeout_arg, remaining)
            attempts = index + 1
            try:
                os.remove(out_path)
            except FileNotFoundError:
                pass
            result = adapter(t, cfg, prompt,
                             os.path.abspath(args.prompt_file) if args.prompt_file else '',
                             workdir, log_path, attempt_timeout, effort, no_progress)
            method = collect_output(workdir, out_name, log_path, out_path)
            if (method != 'missing' or result.get('effort_rejected')
                    or is_terminal_error(result)):
                break
            if index < retries:
                retry_errors.append({
                    'attempt': attempts,
                    'error_type': result.get('error_type'),
                    'error_message': str(result.get('error_message') or '')[:300],
                    'timed_out': bool(result.get('timed_out')),
                })
                delay = min(max(0.0, retry_delay) * (2 ** index),
                            max(0, timeout - int(time.time() - start)))
                if delay > 0:
                    time.sleep(delay)
        if result is None:  # defensive: the loop always runs at least once
            result = {'exit_code': 1, 'timed_out': False, 'error_type': 'no_attempt'}

        wall = int(time.time() - start)
        meta = {
            'benchmark': 'free-stealth-bench',
            'task': args.task or '',
            'provider': t['provider_id'],
            'harness': t['harness_id'],
            'target': t['target'],
            'model': t['model'],
            'model_id': t['model_id'],
            'effort': effort,
            'effort_requested': str(args.effort or cfg.default_effort(t['provider'])),
            'effort_fallback': False,
            'agent_version': agent_version(harness),
            'date_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'wall_seconds': wall,
            'exit_code': int(result.get('exit_code') if result.get('exit_code') is not None else 1),
            'timed_out': bool(result.get('timed_out')),
            'collection_method': method,
            'tokens': result.get('tokens'),
            'steps': result.get('steps'),
            'session_id': result.get('session_id'),
            'events': result.get('events') or 0,
            'error_type': result.get('error_type'),
            'error_message': result.get('error_message'),
            'effort_rejected': bool(result.get('effort_rejected')),
            'attempts': attempts,
            'retry_errors': retry_errors,
        }
        if meta['timed_out']:
            meta['exit_code'] = 124
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, indent=2)
        status = 'ok' if method != 'missing' else 'missing'
        print(f"runner: {t['target']}  effort={effort}  {status}  {wall}s"
              f"  attempts={attempts}  exit={meta['exit_code']}")
        if result.get('effort_rejected'):
            return 3
        return 0 if method != 'missing' else 2
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def cmd_call(args):
    """One-shot text call for judging. Writes the assistant text to --out."""
    cfg = Config()
    t = cfg.resolve(args.target)
    harness = t['harness']
    prompt = args.prompt
    if args.prompt_file:
        with open(args.prompt_file, encoding='utf-8') as fh:
            prompt = fh.read()
    effort = cfg.resolve_effort(t, args.effort or 'default')
    timeout = int(args.timeout or 600)
    workdir = tempfile.mkdtemp(prefix='free-stealth-call-')
    log_path = os.path.abspath(args.log or os.path.join(workdir, 'call.ndjson'))
    meta_path = os.path.abspath(args.meta or (log_path + '.meta.json'))
    try:
        adapter = ADAPTERS.get(str(harness.get('type')))
        if not adapter:
            die(f"harness {t['harness_id']!r} has unknown type {harness.get('type')!r}")
        # Text-only call: the harnesses accept prompt_file but not attachments
        # yet. OpenAI/Anthropic image judging can be added by extending this.
        result = adapter(t, cfg, prompt, os.path.abspath(args.prompt_file) if args.prompt_file else '',
                         workdir, log_path, timeout, effort)
        text = ''
        try:
            with open(log_path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith('{'):
                        continue
                    ev = json.loads(line)
                    part = ev.get('part') or {}
                    if isinstance(part, dict) and part.get('type') == 'text':
                        text += str(part.get('text', ''))
        except (OSError, json.JSONDecodeError):
            pass
        if args.out_meta:
            with open(meta_path, 'w', encoding='utf-8') as fh:
                json.dump({'provider': t['provider_id'], 'harness': t['harness_id'],
                           'target': t['target'], 'effort': effort,
                           'agent_version': agent_version(harness),
                           'tokens': result.get('tokens'),
                           'exit_code': result.get('exit_code'),
                           'timed_out': result.get('timed_out')}, fh, indent=2)
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return 0 if text else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Provider- and harness-agnostic SVG benchmark runner')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('list', help='show providers and harnesses')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('discover', help='print discovered targets')
    p.add_argument('--provider')
    p.add_argument('--all-harnesses', action='store_true',
                   help='also emit harness@provider/model for alternate harnesses')
    p.add_argument('--all', action='store_true', help='include disabled providers')
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser('effort', help='print the resolved effort for a target')
    p.add_argument('--target', required=True)
    p.add_argument('--requested')
    p.set_defaults(func=cmd_effort)

    p = sub.add_parser('run', help='run one task for one target')
    p.add_argument('--target', required=True)
    p.add_argument('--task')
    p.add_argument('--prompt')
    p.add_argument('--prompt-file')
    p.add_argument('--effort')
    p.add_argument('--timeout', type=int)
    p.add_argument('--retries', type=int,
                   help='extra attempts on transient failures (default: BENCH_RETRIES or 2)')
    p.add_argument('--retry-delay', type=float,
                   help='initial backoff in seconds, doubled per retry (default: 5)')
    p.add_argument('--attempt-timeout', type=int,
                   help='per-attempt wall clock; defaults to the remaining --timeout budget')
    p.add_argument('--no-progress-timeout', type=int,
                   help='kill an agent with no text/tool call for N seconds (0 disables; default: 240)')
    p.add_argument('--out', required=True)
    p.add_argument('--log')
    p.add_argument('--meta')
    p.set_defaults(func=cmd_run)

    p = sub.add_parser('call', help='one-shot text call (judging)')
    p.add_argument('--target', required=True)
    p.add_argument('--prompt')
    p.add_argument('--prompt-file')
    p.add_argument('--effort')
    p.add_argument('--timeout', type=int)
    p.add_argument('--out', required=True)
    p.add_argument('--log')
    p.add_argument('--meta')
    p.add_argument('--out-meta', action='store_true')
    p.set_defaults(func=cmd_call)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())

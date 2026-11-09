#!/usr/bin/env python3
"""svg-bench leaderboard server — whitelist-only static files + one guarded action.

Static serving (repo root):
  /                  → 302 to /site/
  /svg-bench/site/*  → the leaderboard page (html/css/js/data.json/data.js)
  /svg-bench/results/* → sidecars (.json) and artifacts (.svg) only

Action API (127.0.0.1 only, never exposed elsewhere):
  GET  /api/run      → {"state":"idle"|"running"|"finished", ...details, "log_tail":[...]}
  POST /api/run      → spawn a re-run of the benchmark
                       body: {"pairs":[["model","task"],...]}  OR
                             {"models":[...],"tasks":[...]}    (cartesian expansion) OR
                             {"dry_run":true, ...}             (validate only, spawn nothing)
                       Re-runs are always executed with --force. One run at a
                       time; provider/task whitelists; body capped at 64 KB.

Everything else → 404. No directory listing, no logs, no other files.
"""
import errno
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))   # svg-bench/
REPO = os.path.dirname(ROOT)                        # repo root
SITE_URL = '/svg-bench/site/'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BIND = '127.0.0.1'
LOGS = os.path.join(ROOT, 'logs')
RUN_STATE = os.path.join(LOGS, 'ui-run-state.json')
ACTIONS_MAX_BODY = 64 * 1024
ACTIONS_MAX_PAIRS = 44

MIME = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
        '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
        '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'}

PROVIDER_RE = re.compile(r'^(opencode|openrouter|tokenrouter|ollama|claude-code-router)/[a-zA-Z0-9._:/+~#-]{1,90}$')

KNOWN_TASKS = None
def known_tasks():
    global KNOWN_TASKS
    if KNOWN_TASKS is None:
        try:
            import yaml
            KNOWN_TASKS = [t['id'] for t in yaml.safe_load(open(os.path.join(ROOT, 'tasks.yaml'), encoding='utf-8'))['tasks']]
        except Exception:
            KNOWN_TASKS = ['sloth-excavator', 'horse-racing']
    return KNOWN_TASKS

def expand_pairs(body):
    pairs = []
    def add(model, task):
        if isinstance(model, str) and isinstance(task, str):
            pairs.append([model.strip(), task.strip()])
    if isinstance(body.get('pairs'), list):
        for p in body['pairs']:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                add(p[0], p[1])
    else:
        models = body.get('models') or []
        tasks = body.get('tasks') or [known_tasks()[0]]
        for m in models:
            for t in tasks:
                add(m, t)
    if not pairs:
        return None, 'no model|task pairs supplied'
    if len(pairs) > ACTIONS_MAX_PAIRS:
        return None, f'too many pairs ({len(pairs)} > {ACTIONS_MAX_PAIRS})'
    tasks_known = set(known_tasks())
    for model, task in pairs:
        if not PROVIDER_RE.match(model) or '..' in model:
            return None, f'model rejected by whitelist: {model!r}'
        if task not in tasks_known:
            return None, f'unknown task: {task!r} (known: {sorted(tasks_known)})'
    return pairs, None

def lock_state():
    """Return (running: bool, state: dict|None) from the ui-run state file."""
    try:
        st = json.load(open(RUN_STATE, encoding='utf-8'))
        pid = int(st.get('pid', 0))
        os.kill(pid, 0)
        return True, st
    except Exception:
        return False, None

def any_runner_active():
    running, _ = lock_state()
    if running:
        return True
    try:
        out = subprocess.run(['ps', '-eo', 'args='], capture_output=True, timeout=5, text=True).stdout
        for line in out.splitlines():
            if 'svg-bench/run.sh' in line and 'run-benchmark' not in line:
                return True
    except Exception:
        pass
    return False

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def read_log_tail(path, n=15):
    try:
        with open(path, 'rb') as fh:
            lines = fh.read().decode('utf-8', 'replace').splitlines()
        return [l for l in lines if l.strip()][-n:]
    except OSError:
        return []

def run_state_payload():
    running, st = lock_state()
    if st is None:
        if os.path.isfile(RUN_STATE):
            try:
                st = json.load(open(RUN_STATE, encoding='utf-8'))
                tail = read_log_tail(st.get('log', ''))
                done = sum(1 for l in tail if l.startswith('DONE'))
                return {'state': 'finished', 'pid': st.get('pid'), 'started_utc': st.get('started_utc'),
                        'pairs': st.get('pairs'), 'models': st.get('models'), 'tasks': st.get('tasks'),
                        'done': done, 'log_tail': tail, 'log': st.get('log')}
            except Exception:
                pass
        return {'state': 'idle'}
    try:
        started = datetime.fromisoformat(st['started_utc'].replace('Z', '+00:00'))
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds())
    except Exception:
        elapsed = None
    tail = read_log_tail(st.get('log', ''))
    done = sum(1 for l in tail if l.startswith('DONE'))
    return {'state': 'running', 'pid': st.get('pid'), 'started_utc': st.get('started_utc'),
            'elapsed_seconds': elapsed, 'pairs': st.get('pairs'), 'models': st.get('models'),
            'tasks': st.get('tasks'), 'done': done, 'total': len(st.get('pairs') or []),
            'log_tail': tail}

def spawn_run(pairs):
    os.makedirs(LOGS, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    log_rel = os.path.join('logs', f'ui-run-{ts}.log')
    log_path = os.path.join(ROOT, log_rel)
    fh = open(log_path, 'wb')
    pair_arg = ','.join(f'{m}|{t}' for m, t in pairs)
    proc = subprocess.Popen(['bash', 'run.sh', '--pairs', pair_arg, '--force'],
                            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                            start_new_session=True, stdin=subprocess.DEVNULL)
    st = {'pid': proc.pid, 'started_utc': now_iso(), 'pairs': pairs,
          'models': sorted({m for m, _ in pairs}), 'tasks': sorted({t for _, t in pairs}),
          'log': log_rel}
    with open(RUN_STATE, 'w', encoding='utf-8') as out:
        json.dump(st, out, indent=2)
    return proc.pid, log_rel

def allowed(path: str):
    """Map a URL path to a repo-root-relative file, or None to refuse."""
    p = unquote(path)
    if p.endswith('/'):
        p += 'index.html'
    if p.startswith('/svg-bench/site/'):
        # The static build puts artifacts under <site>/results/. Map that URL
        # back to the canonical results directory so local dev and GitHub Pages
        # use exactly the same relative URLs.
        if p.startswith('/svg-bench/site/results/'):
            rel = 'svg-bench/results/' + p[len('/svg-bench/site/results/'):]
        else:
            rel = p.lstrip('/')
        ext = os.path.splitext(rel)[1].lower()
        return rel if (ext in MIME and '/..' not in rel) else None
    if p.startswith('/svg-bench/results/'):
        rel = p.lstrip('/')
        ext = os.path.splitext(rel)[1].lower()
        return rel if (ext in ('.svg', '.json') and '/..' not in rel) else None
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = 'svgbench/1.0'

    def log_message(self, fmt, *args):
        pass  # silent; the leaderboard polls constantly

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel):
        path = os.path.normpath(os.path.join(REPO, rel))
        if not (path.startswith(REPO + os.sep) or path == REPO):
            self.send_error(404, 'not found')
            return
        if not os.path.isfile(path):
            self.send_error(404, 'not found')
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            body = open(path, 'rb').read()
        except OSError as e:
            self.send_error(500, str(e))
            return
        self.send_response(200)
        self.send_header('Content-Type', MIME.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        query = ('?' + parsed.query) if parsed.query else ''
        if p == '/' or p == '/site/' or p == '/site':
            self.send_response(302)
            self.send_header('Location', SITE_URL + query)
            self.end_headers()
            return
        if p == '/api/run':
            self._send_json(run_state_payload())
            return
        if p.startswith('/site/'):  # /site/x → /svg-bench/site/x
            self.send_response(302)
            self.send_header('Location', '/svg-bench' + p + query)
            self.end_headers()
            return
        rel = allowed(p)
        if rel is None:
            self.send_error(404, 'not found')
            return
        self._send_file(rel)

    def do_POST(self):
        p = urlparse(self.path).path
        if p != '/api/run':
            self.send_error(404, 'not found')
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > ACTIONS_MAX_BODY:
            self._send_json({'error': 'body missing or over 64 KB'}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({'error': 'invalid JSON body'}, 400)
            return
        pairs, err = expand_pairs(body)
        if err:
            self._send_json({'error': err}, 400)
            return
        if body.get('dry_run'):
            self._send_json({'dry_run': True, 'pairs': pairs,
                             'command': ['bash', 'run.sh', '--pairs', ','.join(f'{m}|{t}' for m, t in pairs), '--force']})
            return
        if any_runner_active():
            self._send_json({'error': 'a benchmark run is already active — wait for it to finish'}, 409)
            return
        pid, log_rel = spawn_run(pairs)
        self._send_json({'state': 'running', 'pid': pid, 'pairs': pairs, 'log': log_rel,
                         'started_utc': now_iso()}, 202)


if __name__ == '__main__':
    try:
        httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"port {PORT} already in use — pick another: python3 serve.py 8001")
        raise
    print(f"svg-bench leaderboard → http://{BIND}:{PORT}/site/   (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

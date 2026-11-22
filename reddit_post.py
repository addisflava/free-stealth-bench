#!/usr/bin/env python3
"""Announce newly-ranked models on the OpenCode community (Reddit).

Hook: called by publish.sh after discovery finds a new model and the judge has
ranked it. It reuses the *same* discovery / judge data as site/data.json, so the
post can never disagree with the leaderboard.

Behavior (all best-effort — it never breaks a publish run):

  1. Always write a ready-to-paste Markdown draft into posts/.
  2. If Reddit OAuth credentials are configured, also submit the post to the
     configured subreddit(s). Without credentials the draft is the whole output.

A state file (posts/.reddit-state.json) records which models were already
announced and which subreddits accepted a post, so re-running is idempotent and
a failed subreddit is retried on the next run.

Configuration (environment variables):

  NO_REDDIT=1            skip everything (explicit opt-out, like NO_PUBLISH)
  REDDIT_DRY_RUN=1       build drafts, but do not call the Reddit API
  REDDIT_CLIENT_ID       Reddit "script" app id
  REDDIT_CLIENT_SECRET   Reddit "script" app secret
  REDDIT_USERNAME        Reddit account username
  REDDIT_PASSWORD        Reddit account password
  REDDIT_SUBREDDITS      comma-separated targets (default: opencode,OpenCode)
  REDDIT_USER_AGENT      OAuth User-Agent (a distinct UA is required by Reddit)
  LEADERBOARD_URL        link placed in the post (default: derived from origin)
  REDDIT_TOP_N           ranking rows to include (default: 15)

All four REDDIT_* credentials must be present for auto-submitting; otherwise
only a draft is produced. Run manually with:

    python3 reddit_post.py                      # use discovery's "new" list
    python3 reddit_post.py --models a,b,c       # explicit models (publish.sh)
    python3 reddit_post.py --force --dry-run    # regenerate a draft
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from bench_config import ROOT
from leaderboard import load_discovery, load_judgments

POSTS_DIR = os.path.join(ROOT, 'posts')
STATE_PATH = os.path.join(POSTS_DIR, '.reddit-state.json')
DEFAULT_SUBREDDITS = ['opencode', 'OpenCode']

DIM_LABELS = {
    'output_quality': 'output quality',
    'instruction_following': 'instruction following',
    'creativity': 'creativity',
    'animation_fidelity': 'animation fidelity',
    'richness_detail': 'richness & detail',
    'composition_aesthetics': 'composition & aesthetics',
    'technical_validity': 'technical validity',
}


def env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ('', '0', 'false', 'no', 'off')


def model_key(name):
    """Case-insensitive identity for matching discovery names to judge names."""
    return str(name or '').strip().lower()


def short_name(name):
    return str(name or '').rsplit('/', 1)[-1].split('@')[0]


def leaderboard_url():
    url = os.environ.get('LEADERBOARD_URL', '').strip()
    if url:
        return url
    try:
        remote = subprocess.check_output(
            ['git', '-C', ROOT, 'remote', 'get-url', 'origin'],
            stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return ''
    m = re.search(r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$', remote)
    if not m:
        return ''
    return f"https://{m.group(1)}.github.io/{m.group(2)}/"


def find_ranking(model, ranking):
    key = model_key(model)
    short = short_name(key)
    for entry in ranking:
        ekey = model_key(entry.get('model'))
        if ekey == key or short_name(ekey) == short:
            return entry
    return None


def ranking_table(ranking, focus_key, limit):
    shown = list(ranking[:limit])
    if focus_key not in {model_key(e.get('model')) for e in shown}:
        focus = next((e for e in ranking if model_key(e.get('model')) == focus_key), None)
        if focus:
            shown.append(focus)
    width = min(max((len(str(e.get('model') or '')) for e in shown), default=20), 46)
    header = f"  #  {'model':<{width}}  score"
    lines = [header, '-' * len(header)]
    previous = None
    for entry in shown:
        rank = entry.get('rank')
        try:
            rank_i = int(rank)
        except (TypeError, ValueError):
            rank_i = None
        if previous is not None and rank_i is not None and rank_i > previous + 1:
            lines.append('  ..')
        name = str(entry.get('model') or '')
        score = entry.get('mean_composite')
        score_s = f"{score:.1f}" if isinstance(score, (int, float)) else '-'
        tag = '   <-- NEW' if model_key(name) == focus_key else ''
        rank_s = f"{rank_i:>3}" if rank_i is not None else '  ?'
        lines.append(f"{rank_s}  {name:<{width}}  {score_s:>5}{tag}")
        if rank_i is not None:
            previous = rank_i
    if len(ranking) > len(shown):
        lines.append(f"  .. {len(ranking) - len(shown)} more (see full leaderboard)")
    return '\n'.join(lines)


def describe(entry, total):
    """A short, data-derived description of the newly ranked model."""
    dims = entry.get('dimensions') or {}
    scored = sorted(((k, float(v)) for k, v in dims.items()
                     if isinstance(v, (int, float))), key=lambda x: x[1], reverse=True)
    score = entry.get('mean_composite')
    score_s = f"{score:.1f}" if isinstance(score, (int, float)) else 'n/a'
    rank = entry.get('rank', '?')
    parts = [f"**{entry.get('model')}** lands at **#{rank} of {total}** "
             f"with a mean composite score of **{score_s}/100**."]
    if scored:
        best_k, best_v = scored[0]
        worst_k, worst_v = scored[-1]
        parts.append(
            f"Its strongest dimension is {DIM_LABELS.get(best_k, best_k)} "
            f"({best_v:.1f}/5) and its weakest is "
            f"{DIM_LABELS.get(worst_k, worst_k)} ({worst_v:.1f}/5).")
    runs = entry.get('n_runs') or 0
    dnf = entry.get('dnf') or 0
    if runs:
        if dnf:
            parts.append(f"It finished {runs - dnf} of {runs} runs cleanly; "
                         f"{dnf} DNF run(s) scored zero.")
        else:
            parts.append(f"It completed all {runs} benchmark runs with no DNFs.")
    per_task = entry.get('per_task') or {}
    if per_task:
        bits = []
        for task, value in per_task.items():
            composite = value.get('composite') if isinstance(value, dict) else None
            bits.append(f"{task} ({composite:.1f})"
                        if isinstance(composite, (int, float)) else str(task))
        parts.append('Task scores: ' + ', '.join(bits) + '.')
    return ' '.join(parts)


def build_post(entry, judge, url, limit):
    """Return (title, markdown body) for a self post."""
    ranking = judge.get('ranking') or []
    focus_key = model_key(entry.get('model'))
    name = short_name(entry.get('model'))
    rank = entry.get('rank', '?')
    total = len(ranking)
    score = entry.get('mean_composite')
    score_s = f"{score:.1f}" if isinstance(score, (int, float)) else 'n/a'
    title = f"New free model benchmarked: {name} ranked #{rank}/{total} ({score_s}/100)"

    meta = judge.get('meta') or {}
    judge_model = judge.get('judge_model') or meta.get('judge_model') or 'automated judge'
    rubric = judge.get('rubric_version') or 'v1'
    # Short human labels beat the long prompt blurbs for a Reddit post.
    task_names = [t.replace('-', ' ') for t in (entry.get('per_task') or {}).keys()]

    link = f"Full leaderboard: {url}" if url else ''
    body = f"""New-model alert for **{entry.get('model')}**, freshly ranked by the \
automated free-model benchmark [free-stealth-bench].

## Leaderboard ranking

    {ranking_table(ranking, focus_key, limit).replace(chr(10), chr(10) + '    ')}

{link}

## About {name}

- **Rank:** #{rank} of {total}
- **Score:** {score_s}/100 (mean composite)
- **Runs:** {entry.get('n_runs', 0)} (DNF: {entry.get('dnf', 0)})
- **Provider / harness:** {entry.get('provider', '?')} / {entry.get('harness', '?')}
- **Tested on:** {', '.join(task_names) if task_names else 'n/a'}
- **Scored by:** {judge_model} (rubric {rubric})

{describe(entry, total)}

---
*Automated announcement generated from the benchmark's judge results. Rankings \
reflect this date, this provider endpoint and this harness — free models change often.*"""
    return title, body


def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(POSTS_DIR, exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def reddit_token(cfg):
    data = urllib.parse.urlencode({
        'grant_type': 'password',
        'username': cfg['username'],
        'password': cfg['password'],
    }).encode()
    basic = base64.b64encode(
        f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    req = urllib.request.Request(
        'https://www.reddit.com/api/v1/access_token',
        data=data,
        headers={
            'Authorization': f'Basic {basic}',
            'User-Agent': cfg['user_agent'],
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    token = payload.get('access_token')
    if not token:
        raise RuntimeError(f'no access_token in Reddit response: {payload}')
    return token


def submit_post(token, cfg, subreddit, title, text):
    data = urllib.parse.urlencode({
        'api_type': 'json',
        'kind': 'self',
        'sr': subreddit,
        'title': title,
        'text': text,
        'resubmit': 'false',
    }).encode()
    req = urllib.request.Request(
        'https://oauth.reddit.com/api/submit',
        data=data,
        headers={
            'Authorization': f'bearer {token}',
            'User-Agent': cfg['user_agent'],
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    errors = (payload.get('json') or {}).get('errors') or []
    if errors:
        raise RuntimeError(f'Reddit returned errors: {errors}')
    return (payload.get('json') or {}).get('data') or {}


def subreddit_list():
    raw = os.environ.get('REDDIT_SUBREDDITS') or ','.join(DEFAULT_SUBREDDITS)
    subs = [s.strip().lstrip('r/') for s in raw.split(',') if s.strip()]
    # Reddit subreddit names are case-insensitive: r/opencode and r/OpenCode
    # are the same community. Keep both in the config but submit only once.
    seen, unique = set(), []
    for sub in subs:
        if sub.lower() not in seen:
            seen.add(sub.lower())
            unique.append(sub)
    return unique


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--models', help='comma-separated model names (from publish.sh)')
    parser.add_argument('--force', action='store_true', help='regenerate an existing draft')
    parser.add_argument('--no-post', action='store_true', help='only write drafts')
    parser.add_argument('--dry-run', action='store_true', help='only write drafts')
    args = parser.parse_args()

    if env_flag('NO_REDDIT'):
        print('reddit: NO_REDDIT=1 set, skipping')
        return 0

    models = [m.strip() for m in (args.models or '').split(',') if m.strip()]
    if not models:
        discovery = load_discovery() or {}
        models = list(discovery.get('new') or [])
    if not models:
        print('reddit: no new models to announce')
        return 0

    judge = load_judgments()
    ranking = (judge or {}).get('ranking') or []
    if not judge or not ranking:
        print('reddit: no judge ranking available yet; skipping')
        return 0

    url = leaderboard_url()
    try:
        limit = max(1, int(os.environ.get('REDDIT_TOP_N', '15')))
    except ValueError:
        limit = 15

    cfg = {
        'client_id': os.environ.get('REDDIT_CLIENT_ID', '').strip(),
        'client_secret': os.environ.get('REDDIT_CLIENT_SECRET', '').strip(),
        'username': os.environ.get('REDDIT_USERNAME', '').strip(),
        'password': os.environ.get('REDDIT_PASSWORD', '').strip(),
        'user_agent': os.environ.get('REDDIT_USER_AGENT', '').strip()
                      or f"free-stealth-bench-announcer/1.0 (by /u/{os.environ.get('REDDIT_USERNAME', 'unknown')})",
    }
    can_post = (all(cfg[k] for k in ('client_id', 'client_secret', 'username', 'password'))
                and not args.no_post and not args.dry_run
                and not env_flag('REDDIT_DRY_RUN'))
    subs = subreddit_list()

    state = load_state()
    token = None
    if can_post:
        try:
            token = reddit_token(cfg)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            print(f"reddit: OAuth failed, drafts only: {exc}", file=sys.stderr)
            can_post = False

    os.makedirs(POSTS_DIR, exist_ok=True)
    date = datetime.now(timezone.utc).date().isoformat()
    for model in models:
        entry = find_ranking(model, ranking)
        # Use the judge's canonical model name as the state key so publish.sh
        # ("hy4-flash-free") and a manual run ("opencode/hy4-flash-free") cannot
        # create two drafts for the same model.
        key = model_key(entry.get('model')) if entry else model_key(model)
        record = dict(state.get(key) or {})
        if record.get('draft') and not args.force:
            print(f"reddit: {model} already announced ({record['draft']}); use --force to regenerate")
            continue
        if not entry:
            print(f"reddit: {model} is not ranked by the judge yet; skipping")
            continue

        title, body = build_post(entry, judge, url, limit)
        slug = re.sub(r'[^A-Za-z0-9._-]+', '-', entry.get('model')).strip('-') or 'model'
        filename = f"{date}-{slug}.md"
        path = os.path.join(POSTS_DIR, filename)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(f'# {title}\n\n{body}\n')
        rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
        print(f'reddit: wrote draft {rel}')

        posted = list(record.get('posted') or [])
        if can_post and token:
            already = {s.lower() for s in posted}
            for sub in subs:
                if sub.lower() in already:
                    continue
                try:
                    submit_post(token, cfg, sub, title, body)
                    posted.append(sub)
                    print(f'reddit: posted to r/{sub}')
                except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
                    print(f"reddit: could not post to r/{sub}: {exc}", file=sys.stderr)

        record.update({
            'model': entry.get('model'),
            'draft': rel,
            'title': title,
            'rank': entry.get('rank'),
            'score': entry.get('mean_composite'),
            'posted': posted,
            'updated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        })
        state[key] = record
        save_state(state)

    if not can_post and not env_flag('REDDIT_DRY_RUN'):
        print('reddit: no OAuth credentials configured; drafts are ready to paste manually')
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Generate svg-bench/site/data.json from the sidecars in results/.

The page itself (site/index.html etc.) is static — this script is the only
"build step". Re-run it any time results change:

    cd svg-bench && python3 leaderboard.py

If svg-bench/ratings.json exists (an exported ratings file committed to the
repo), its ratings are embedded as `seed_ratings` — the page uses them as the
default for every visitor whose browser has no local ratings.

If svg-bench/judgments.json exists (written by judge.py), its automated judge
scores and ranking are embedded as `judge`. They live in a separate file so a
benchmark re-run can refresh data.json without re-running the judge.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, 'results')
SITE = os.path.join(ROOT, 'site')
TASKS_YAML = os.path.join(ROOT, 'tasks.yaml')


def load_public_reviews():
    """Load optional public-review seed data committed to the repo.

    The live site normally reads public reviews from Supabase (configured in
    site/config.js). reviews.json is useful for a static snapshot, for tests,
    and as a fallback when no backend has been configured yet.
    """
    path = os.path.join(SITE, 'reviews.json')
    if not os.path.isfile(path):
        return []
    try:
        raw = json.load(open(path, encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        print(f"leaderboard: reviews.json ignored ({e})", file=sys.stderr)
        return []
    rows = raw.get('reviews') if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            stars = int(r.get('stars') or 0)
        except (TypeError, ValueError):
            stars = 0
        if not r.get('run_id') or not (1 <= stars <= 5):
            continue
        out.append({
            'id': str(r.get('id') or f"seed-{len(out)}"),
            'run_id': str(r['run_id']),
            'model': r.get('model'),
            'task': r.get('task'),
            'stars': stars,
            'note': str(r.get('note') or '')[:2000],
            'reviewer': str(r.get('reviewer') or 'anonymous')[:60],
            'created_at': r.get('created_at'),
            'source': 'seed',
        })
    return out


def load_judgments():
    """Load automated judge results committed to the repo (judgments.json).

    Returns a compact, UI-ready object, or None when no judging has been done
    yet (GitHub Pages builds before any judge run must still render).
    """
    path = os.path.join(ROOT, 'judgments.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"leaderboard: judgments.json ignored ({e})", file=sys.stderr)
        return None
    if not isinstance(raw, dict):
        return None
    by_run = {}
    for run_id, e in (raw.get('runs') or {}).items():
        if not isinstance(e, dict) or not e.get('judged'):
            continue
        by_run[str(run_id)] = {
            'composite': e.get('composite'),
            'scores': e.get('scores') or {},
            'reasons': e.get('reasons') or {},
            'technical_validity': e.get('technical_validity'),
            'technical_checks': e.get('technical_checks') or {},
            'dnf': bool(e.get('dnf')),
            'input_mode': e.get('input_mode'),
            'judge_model': e.get('judge_model'),
            'parse_ok': bool(e.get('parse_ok')),
            'parse_note': e.get('parse_note') or '',
            'frames': e.get('frames'),
            'render_ok': e.get('render_ok'),
        }
    meta = raw.get('meta') or {}
    return {
        'rubric_version': raw.get('rubric_version'),
        'judge_model': raw.get('judge_model'),
        'weights': raw.get('weights') or {},
        'dimensions': raw.get('dimensions') or [],
        'updated_utc': raw.get('updated_utc'),
        'meta': {
            'probe': meta.get('probe'),
            'n_judged': meta.get('n_judged', len(by_run)),
            'n_dnf': meta.get('n_dnf', 0),
            'blind_note': meta.get('blind_note', ''),
            'judge_is_contestant_note': meta.get('judge_is_contestant_note', ''),
        },
        'by_run': by_run,
        'ranking': raw.get('ranking') or [],
    }


def load_tasks_blurbs():
    try:
        import yaml
        with open(TASKS_YAML, encoding='utf-8') as fh:
            tasks = yaml.safe_load(fh)['tasks']
        return {t['id']: {
            'blurb': ' '.join((t.get('blurb') or '').split()),
        } for t in tasks}
    except Exception as e:  # noqa: BLE001 - blurbs are decorative
        print(f"leaderboard: tasks.yaml ignored ({e})", file=sys.stderr)
        return {}


def load_discovery():
    """Latest free-model discovery snapshot (written by run.sh), plus models
    first seen within the last 7 days."""
    snap_dir = os.path.join(ROOT, 'logs')
    files = sorted(glob.glob(os.path.join(snap_dir, 'models-snapshot-*.txt')))
    # pick newest non-empty snapshot
    chosen = None
    for c in reversed(files):
        if os.path.getsize(c) > 0:
            chosen = c
            break
    if not chosen:
        return None
    try:
        latest_models = [ln.strip() for ln in open(chosen, encoding='utf-8') if ln.strip()]
    except OSError:
        return None
    base = os.path.basename(chosen)
    m = re.search(r'models-snapshot-(\d{4}-\d{2}-\d{2})\.txt', base)
    latest_date = datetime.strptime(m.group(1), '%Y-%m-%d').date() if m else datetime.fromtimestamp(
        os.path.getmtime(chosen), timezone.utc).date()
    cutoff = latest_date - timedelta(days=7)
    # models present in any snapshot dated within the last 7 days (but before latest)
    older_models = set()
    for c in files:
        mm = re.search(r'models-snapshot-(\d{4}-\d{2}-\d{2})\.txt', os.path.basename(c))
        if not mm:
            continue
        d = datetime.strptime(mm.group(1), '%Y-%m-%d').date()
        if d >= latest_date or d < cutoff:
            continue
        try:
            for ln in open(c, encoding='utf-8'):
                ln = ln.strip()
                if ln:
                    older_models.add(ln)
        except OSError:
            continue
    new_models = [m for m in latest_models if m not in older_models]
    return {
        'source': 'opencode models',
        'snapshot_date': latest_date.isoformat(),
        'snapshot_file': os.path.relpath(chosen, ROOT).replace(os.sep, '/'),
        'available': latest_models,
        'new': new_models,
    }


def main():
    os.makedirs(SITE, exist_ok=True)
    tasks = load_tasks_blurbs()
    discovery = load_discovery()
    runs = []
    for p in sorted(glob.glob(os.path.join(RESULTS, '*', '*.json'))):
        base = os.path.basename(p)
        if base.startswith('summary-'):
            continue
        try:
            r = json.load(open(p, encoding='utf-8'))
        except json.JSONDecodeError:
            print(f"leaderboard: skipping unreadable sidecar {p}", file=sys.stderr)
            continue
        task = r.get('task') or os.path.basename(os.path.dirname(p))
        svg_path = p[:-5] + '.svg'
        has_svg = os.path.isfile(svg_path)
        v = r.get('validation') or {}
        model = r.get('model') or ''
        provider, _, model_id = model.partition('/')
        run_id = f"{task}__{base[:-5]}"  # task-prefixed: unique across tasks
        runs.append({
            'id': run_id,
            'task': task,
            'model': model,
            'provider': provider or 'unknown',
            'model_id': model_id or model,
            'variant': r.get('variant'),
            'effort': r.get('effort'),
            'effort_requested': r.get('effort_requested'),
            'effort_fallback': bool(r.get('effort_fallback')),
            'timed_out': bool(r.get('timed_out')),
            'date_utc': r.get('date_utc'),
            'opencode_version': r.get('opencode_version'),
            'prompt_sha256': r.get('prompt_sha256'),
            'prompt_file': r.get('prompt_file'),
            'wall_seconds': r.get('wall_seconds'),
            'tokens': r.get('tokens'),
            'steps': r.get('steps'),
            'collection_method': r.get('collection_method'),
            'result_status': r.get('result_status'),
            'error_type': r.get('error_type'),
            'error_message': r.get('error_message'),
            'svg_rel': ('../results/' + os.path.relpath(svg_path, RESULTS).replace(os.sep, '/')) if has_svg else None,
            'size_bytes': v.get('size_bytes'),
            'elements_total': v.get('elements_total'),
            'animation_elements': v.get('animation_elements'),
            'gradient_defs': v.get('gradient_defs'),
            'external_refs': v.get('external_refs'),
            'script_blocks': v.get('script_blocks'),
            'viewBox': v.get('viewBox'),
        })
    runs.sort(key=lambda r: (r['task'], r['model'], r['effort'] or ''))

    seed_ratings = None
    ratings_path = os.path.join(ROOT, 'ratings.json')
    if os.path.isfile(ratings_path):
        try:
            seed_ratings = json.load(open(ratings_path, encoding='utf-8'))
            print(f"leaderboard: {len(seed_ratings)} seed rating(s) loaded from ratings.json")
        except json.JSONDecodeError:
            print("leaderboard: ratings.json unreadable, ignoring", file=sys.stderr)

    ok = sum(1 for r in runs if r['result_status'] == 'ok')
    public_reviews = load_public_reviews()
    judge = load_judgments()
    data = {
        'benchmark': 'svg-bench',
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'tasks': tasks,
        'discovery': discovery,
        'runs': runs,
        'reviews': public_reviews,
        'summary': {
            'runs': len(runs),
            'ok': ok,
            'failed': len(runs) - ok,
            'models': len({r['model'] for r in runs}),
            'tasks': sorted({r['task'] for r in runs}),
            'reviews': len(public_reviews),
            'judged': judge['meta']['n_judged'] if judge else 0,
        },
        'seed_ratings': seed_ratings,
        'judge': judge,
    }
    out = os.path.join(SITE, 'data.json')
    with open(out + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(out + '.tmp', out)
    # Embedded fallback (window.__SVG_BENCH__) so the page also works from file://
    with open(os.path.join(SITE, 'data.js') + '.tmp', 'w', encoding='utf-8') as fh:
        fh.write('window.__SVG_BENCH__ = ')
        json.dump(data, fh, separators=(',', ':'))
        fh.write(';')
    os.replace(os.path.join(SITE, 'data.js') + '.tmp', os.path.join(SITE, 'data.js'))
    print(f"leaderboard: wrote {os.path.relpath(out, ROOT)} + data.js — {len(runs)} runs "
          f"({ok} ok) across {data['summary']['models']} models"
          + (f", {len(public_reviews)} public review(s)" if public_reviews else "")
          + (f", {data['summary']['judged']} judged" if judge else ""))


if __name__ == '__main__':
    main()

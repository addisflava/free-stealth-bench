#!/usr/bin/env python3
"""Automated LLM-as-judge ranking for svg-bench.

Scores every run on seven dimensions (six from a blind free-model judge, one
computed deterministically), combines them into a weighted composite, and ranks
the models. The judge only ever sees an anonymous "Submission A": no model name,
run id, date, path or sidecar identity is ever put in front of it.

    python3 judge.py --probe          # verify a free model accepts a PNG
    python3 judge.py --dry-run        # show what would be judged
    python3 judge.py --limit 5        # judge a few runs
    python3 judge.py                 # judge everything unjudged
    python3 judge.py --report         # re-rank from cache, no model calls
    python3 judge.py --force          # re-probe and re-judge everything

No paid models are used. If no free model accepts an image, judging falls back
to reading the SVG source only and records input_mode="text" on each judgment.
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(ROOT, 'results')
LOGS = os.path.join(ROOT, 'logs')
PROMPTS = os.path.join(ROOT, 'prompts')
TASKS_YAML = os.path.join(ROOT, 'tasks.yaml')
STORE_PATH = os.path.join(ROOT, 'judgments.json')
REPORT_PATH = os.path.join(RESULTS, 'judge-report.md')

RUBRIC_VERSION = 'v1'
LLM_DIMS = ('output_quality', 'instruction_following', 'creativity',
            'animation_fidelity', 'richness_detail', 'composition_aesthetics')
TECHNICAL_DIM = 'technical_validity'
DIMENSIONS = LLM_DIMS + (TECHNICAL_DIM,)

# Weights must sum to 1.0. Tuned so subjective quality and brief adherence
# dominate, while the objective technical check still acts as a floor.
WEIGHTS = {
    'output_quality': 0.22,
    'instruction_following': 0.20,
    'creativity': 0.10,
    'animation_fidelity': 0.12,
    'richness_detail': 0.10,
    'composition_aesthetics': 0.11,
    'technical_validity': 0.15,
}

JUDGE_MODEL = 'opencode/nemotron-3.5-super-vl-free'
CANDIDATES = (JUDGE_MODEL, 'opencode/glm-5.3-flash-free')
FRAME_BUDGETS = (400, 1500, 3000)   # Chrome --virtual-time-budget, milliseconds
FRAME_WIDTH = 512
SVG_CAP = 6000                      # chars of SVG source inlined into the prompt
CALL_TIMEOUT = 300

GENERIC_PROMPT = (
    'Create a single, standalone, animated SVG illustration of the requested '
    'scene, with several continuously animated elements, and save it as '
    'output.svg. The full task prompt was not available for this run; judge '
    'against the requirements that are visible in the SVG itself.'
)

# Content-level facts only. Never include model, date, run id, paths, tokens,
# wall time, or anything else that could identify the contestant.
SOURCE_FACT_KEYS = ('size_bytes', 'elements_total', 'element_counts',
                    'gradient_defs', 'animation_elements', 'keyframes_blocks',
                    'script_blocks', 'external_refs', 'viewBox',
                    'well_formed_xml', 'root_is_svg', 'has_xmlns')

DIM_HELP = {
    'output_quality':
        'Visual quality of the rendered illustration: is it a competent, '
        'appealing drawing of the requested subject? Are shapes recognizable, '
        'or is anything blank, clipped, malformed or visually confusing?',
    'instruction_following':
        'How completely does it satisfy EVERY hard requirement and every '
        '"must include" item in the task prompt? Penalize heavily for a wrong '
        'subject, missing required elements, wrong canvas, external assets, '
        'JavaScript, or fewer than the required animated elements.',
    'creativity':
        'Originality and imagination: unexpected details, charming touches, '
        'inventive composition or motion, while still fitting the brief. '
        'Generic or template output scores low.',
    'animation_fidelity':
        'Does motion actually WORK and match the brief, rather than merely '
        'exist? Horses must genuinely translate across the track, a digging '
        'cycle must cycle. Static or bobbing-in-place motion, or elements that '
        'animate off-screen and vanish, score low.',
    'richness_detail':
        'Scene complexity: layered shapes, gradients, shading, background '
        'detail, distinct elements and depth. Sparse or flat output scores low.',
    'composition_aesthetics':
        'Layout, framing, balance, color harmony, legibility and overall '
        'visual polish. Cluttered, cramped or clashing work scores low.',
}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def new_store():
    return {
        'schema': 'svgbench-judgments/1',
        'rubric_version': RUBRIC_VERSION,
        'weights': WEIGHTS,
        'dimensions': list(DIMENSIONS),
        'judge_model': JUDGE_MODEL,
        'updated_utc': None,
        'meta': {
            'probe': None,
            'blind_note': ('The judge sees only an anonymous "Submission A": '
                           'task prompt, SVG source, objective source facts, '
                           'and rendered frames. No model name, run id, date, '
                           'path or sidecar identity is provided.'),
            'judge_is_contestant_note': ('The free judge model is also a '
                                         'contestant in this benchmark. Blind '
                                         'presentation mitigates but does not '
                                         'eliminate self-preference bias; the '
                                         'judge model is recorded per run and '
                                         'can be changed with --judge-model.'),
            'n_judged': 0,
            'n_dnf': 0,
        },
        'judgments': {},   # cache_key -> cached LLM judgment
        'runs': {},        # run_id -> per-run result (also the site join key)
        'ranking': [],
    }


def load_store(path):
    try:
        with open(path, encoding='utf-8') as fh:
            store = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return new_store()
    if not isinstance(store, dict):
        return new_store()
    for key in ('judgments', 'runs'):
        if not isinstance(store.get(key), dict):
            store[key] = {}
    if not isinstance(store.get('ranking'), list):
        store['ranking'] = []
    if not isinstance(store.get('meta'), dict):
        store['meta'] = new_store()['meta']
    return store


def write_store(store, path):
    store['updated_utc'] = now_utc()
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(store, fh, indent=1)
    os.replace(tmp, path)


def iter_sidecars():
    """Yield every run sidecar, keyed the same way as leaderboard.py."""
    for p in sorted(glob.glob(os.path.join(RESULTS, '*', '*.json'))):
        base = os.path.basename(p)
        if base.startswith('summary-'):
            continue
        try:
            with open(p, encoding='utf-8') as fh:
                sidecar = json.load(fh)
        except (OSError, json.JSONDecodeError):
            print(f"judge: skipping unreadable sidecar {p}", file=sys.stderr)
            continue
        task = sidecar.get('task') or os.path.basename(os.path.dirname(p))
        yield {
            'run_id': f"{task}__{base[:-5]}",
            'task': task,
            'model': sidecar.get('model') or '',
            'date_utc': sidecar.get('date_utc'),
            'result_status': sidecar.get('result_status'),
            'validation': sidecar.get('validation') or {},
            'prompt_file': sidecar.get('prompt_file'),
            'sidecar': sidecar,
            'svg_path': p[:-5] + '.svg',
        }


def load_prompt_text(task, prompt_file):
    candidates = []
    if prompt_file and prompt_file != '<command-line>':
        candidates.append(os.path.join(ROOT, prompt_file))
    candidates.append(os.path.join(PROMPTS, f"{task}.txt"))
    for path in candidates:
        try:
            with open(path, encoding='utf-8') as fh:
                text = fh.read().strip()
        except OSError:
            continue
        if text:
            return text
    try:
        import yaml
        with open(TASKS_YAML, encoding='utf-8') as fh:
            tasks = yaml.safe_load(fh)['tasks']
        for t in tasks:
            if t.get('id') == task and t.get('blurb'):
                return ('The full task prompt was not stored for this run. '
                        'The registered task description was: '
                        + ' '.join(t['blurb'].split()))
    except Exception:  # noqa: BLE001 - blurbs are decorative
        pass
    return GENERIC_PROMPT


def chrome_binary():
    for name in ('google-chrome', 'google-chrome-stable', 'chromium',
                 'chromium-browser'):
        found = shutil.which(name)
        if found:
            return found
    return None


def render_contact_sheet(svg_abs, tmpdir):
    """Render the SVG at three animation times and stack them into one PNG.

    A single frame is unreliable: elements can be mid-translation and off
    canvas. Returns (sheet_path, render_ok, frames). render_ok is a cheap
    non-blank check on the captured frames.
    """
    from PIL import Image, ImageDraw, ImageStat

    chrome = chrome_binary()
    if not chrome:
        return None, False, 0
    uri = 'file://' + svg_abs.replace(os.sep, '/')
    frames = []
    render_ok = False
    for vt in FRAME_BUDGETS:
        png = os.path.join(tmpdir, f'frame_{vt}.png')
        cmd = [chrome, '--headless', '--no-sandbox', '--disable-gpu',
               '--disable-dev-shm-usage', '--hide-scrollbars',
               f'--screenshot={png}', '--window-size=1200,800',
               f'--virtual-time-budget={vt}', uri]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if not os.path.isfile(png):
            continue
        try:
            img = Image.open(png).convert('RGB')
        except OSError:
            continue
        if max(ImageStat.Stat(img).stddev) > 3:
            render_ok = True
        width = FRAME_WIDTH
        height = max(1, round(img.height * width / img.width))
        img = img.resize((width, height))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 92, 18), fill=(0, 0, 0))
        draw.text((4, 3), f't={vt}ms', fill=(255, 255, 255))
        frames.append(img)
    if not frames:
        return None, False, 0
    total_h = sum(f.height for f in frames)
    sheet = Image.new('RGB', (FRAME_WIDTH, total_h), (24, 24, 24))
    y = 0
    for f in frames:
        sheet.paste(f, (0, y))
        y += f.height
    sheet_path = os.path.join(tmpdir, 'submission.png')
    sheet.save(sheet_path)
    return sheet_path, render_ok, len(frames)


def source_facts_block(validation):
    facts = {k: validation.get(k) for k in SOURCE_FACT_KEYS
             if validation.get(k) is not None}
    return json.dumps(facts, indent=1, sort_keys=True)


def build_judge_prompt(prompt_text, svg_text, source_facts, input_mode, truncated):
    if input_mode == 'image':
        visual = (
            'An image is attached. It stacks three screenshots of the same SVG '
            'captured at three different moments of its animation loop '
            '(top = earliest, bottom = latest). Use it to judge how the artwork '
            'actually looks and whether the motion really works.'
        )
    else:
        visual = (
            'No rendered frames are available for this submission. Judge from '
            'the SVG source and the objective source facts below; be cautious '
            'about visual-quality claims you cannot verify.'
        )

    dims = '\n'.join(
        f'  "{k}": {{"score": <1-5>, "reason": "<~40 words>"}},  '
        f'# {DIM_HELP[k]}'
        for k in LLM_DIMS
    )
    return f"""You are an impartial judge for an SVG illustration benchmark.

You are given ONE anonymous submission, called "Submission A". Judge ONLY the material below. Do not try to identify who or what produced it: no such information is provided, and guessing is pointless.

{visual}

<TASK PROMPT>
{prompt_text}
</TASK PROMPT>

<SVG SOURCE FACTS (objective measurements of the source, not an opinion)>
{source_facts}
</SVG SOURCE FACTS>

<SVG SOURCE{" (truncated to fit)" if truncated else ""}>
{svg_text}
</SVG SOURCE>

Score each of these six dimensions with an integer 1-5, where 1 = missing / broken / absent, 2 = poor, 3 = acceptable but plainly flawed, 4 = good with minor flaws, 5 = excellent / near-complete:
{dims}

Rules:
- Score the six dimensions independently; do not let one dimension set all the others.
- Do NOT reward sheer file length, element count or verbosity by itself.
- Be strict but fair: reserve 5 for genuinely excellent work, and penalize missing hard requirements under instruction_following.
- Give a short, concrete reason (about 40 words max) for each score, citing what you actually see.

Reply with ONLY one fenced JSON object and no prose before or after it:

```json
{{
  "output_quality": {{"score": 1, "reason": "..."}},
  "instruction_following": {{"score": 1, "reason": "..."}},
  "creativity": {{"score": 1, "reason": "..."}},
  "animation_fidelity": {{"score": 1, "reason": "..."}},
  "richness_detail": {{"score": 1, "reason": "..."}},
  "composition_aesthetics": {{"score": 1, "reason": "..."}},
  "summary": "<one sentence>"
}}
```"""


def collect_text(log_path):
    """Concatenate text parts from an opencode --format json NDJSON stream."""
    chunks, errors = [], []
    try:
        with open(log_path, 'rb') as fh:
            raw = fh.read().decode('utf-8', 'replace')
    except OSError:
        return '', errors
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get('type') == 'error':
            err = ev.get('error') or {}
            errors.append(str(err.get('message') or err.get('type') or 'error')[:300])
        part = ev.get('part') or {}
        if isinstance(part, dict) and part.get('type') == 'text':
            chunks.append(str(part.get('text', '')))
    return '\n'.join(chunks), errors


def iter_json_objects(text):
    """Yield balanced top-level {...} substrings, ignoring braces in strings."""
    start, depth, in_str, esc = None, 0, False, False
    for i, c in enumerate(text):
        if start is None:
            if c == '{':
                start, depth, in_str, esc = i, 1, False, False
            continue
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                yield text[start:i + 1]
                start = None


def parse_answer(blob):
    """Extract the judge's JSON verdict. Never raises; degrades to parse_ok=False."""
    candidates = []
    for obj in iter_json_objects(blob):
        try:
            data = json.loads(obj)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            candidates.append(data)
    chosen = None
    for data in reversed(candidates):
        if sum(1 for k in LLM_DIMS if k in data) >= 3:
            chosen = data
            break
    if chosen is None:
        return {'parse_ok': False,
                'scores': {k: 0 for k in LLM_DIMS},
                'reasons': {k: '' for k in LLM_DIMS},
                'summary': ''}
    scores, reasons = {}, {}
    for k in LLM_DIMS:
        node = chosen.get(k)
        if isinstance(node, dict):
            raw_score = node.get('score', node.get('stars', node.get('rating')))
            reason = node.get('reason') or node.get('note') or node.get('comment') or ''
        else:
            raw_score, reason = node, ''
        try:
            raw_score = float(raw_score)
        except (TypeError, ValueError):
            raw_score = 1.0
        scores[k] = int(min(5, max(1, round(raw_score))))
        reasons[k] = str(reason)[:300]
    return {'parse_ok': True, 'scores': scores, 'reasons': reasons,
            'summary': str(chosen.get('summary') or '')[:300]}


def run_judge(prompt, model, sheet_path, input_mode, log_path):
    """One blind opencode call. Returns (text, errors, exit_code)."""
    cwd = tempfile.mkdtemp(prefix='judge-cwd-', dir=LOGS)
    try:
        cmd = ['opencode', 'run', '--format', 'json', '--auto', '-m', model]
        if input_mode == 'image' and sheet_path:
            anonymous = os.path.join(cwd, 'submission.png')
            shutil.copyfile(sheet_path, anonymous)
            # opencode's background server resolves -f itself, so a relative
            # path is looked up in the wrong cwd. Use the absolute path of the
            # anonymously named copy (no model name appears anywhere in it).
            cmd += ['-f', anonymous]
        cmd.append(prompt)
        try:
            with open(log_path, 'wb') as log:
                proc = subprocess.run(cmd, cwd=cwd, stdout=log,
                                      stderr=subprocess.STDOUT,
                                      timeout=CALL_TIMEOUT)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        except OSError as e:
            with open(log_path, 'w', encoding='utf-8') as log:
                log.write(str(e))
            return '', [str(e)], 1
        text, errors = collect_text(log_path)
        return text, errors, exit_code
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def technical_validity(validation, render_ok):
    """Deterministic 1..5 score from the sidecar plus our own render check."""
    checks = {
        'valid_xml': bool(validation.get('well_formed_xml') and validation.get('root_is_svg')),
        'declared_svg': bool(validation.get('has_xmlns') and validation.get('has_viewbox_or_size')),
        'self_contained': (int(validation.get('external_refs') or 0) == 0
                           and int(validation.get('script_blocks') or 0) == 0),
        'rendered': bool(render_ok),
    }
    return 1 + sum(checks.values()), checks


def composite(scores):
    # weighted mean over the 1–5 dimensions, scaled to a 0–100 score
    return round(sum(WEIGHTS[k] * scores.get(k, 0) for k in DIMENSIONS) * 20, 2)


def probe_models(models):
    """Check whether a free model can actually see an attached PNG.

    Returns {'candidate', 'image_ok', 'mode', 'detail'}. When no model passes,
    mode='text' and judging falls back to SVG source only.
    """
    from PIL import Image

    tmpdir = tempfile.mkdtemp(prefix='judge-probe-', dir=LOGS)
    result = {'candidate': models[0], 'image_ok': False, 'mode': 'text',
              'detail': 'no successful probe'}
    try:
        probe_png = os.path.join(tmpdir, 'probe.png')
        Image.new('RGB', (64, 64), (220, 20, 20)).save(probe_png)
        prompt = ('An image is attached. Reply with ONLY one fenced JSON '
                  'object with a single key "color" whose value is the '
                  'dominant color name of the attached image.')
        first_responder = None
        for model in models:
            log = os.path.join(LOGS, 'judge__probe.ndjson')
            text, errors, code = run_judge(prompt, model, probe_png, 'image', log)
            if first_responder is None and text.strip():
                first_responder = model
            answer = text.lower()
            ok = code == 0 and not errors and 'red' in answer
            print(f"judge: probe {model}: image_ok={ok} "
                  f"(exit={code}, errors={len(errors)})")
            if ok:
                result = {'candidate': model, 'image_ok': True, 'mode': 'image',
                          'detail': 'identified the attached red probe image'}
                break
        if result['mode'] == 'text':
            result['candidate'] = first_responder or models[0]
            result['detail'] = ('no free model accepted the PNG attachment; '
                                'falling back to SVG-source-only judging')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return result


def sync_runs(store):
    """Make sure every current sidecar has a runs[] entry; invalidate stale ones."""
    for run in iter_sidecars():
        validation = run['validation']
        sha = validation.get('sha256')
        svg_exists = os.path.isfile(run['svg_path'])
        dnf = (run['result_status'] != 'ok' or not validation or not svg_exists)
        entry = store['runs'].get(run['run_id'])
        if entry is None:
            entry = {'judged': False, 'composite': None}
            store['runs'][run['run_id']] = entry
        if (entry.get('svg_sha256') != sha
                or (entry.get('judged') and dnf != entry.get('dnf'))
                or (entry.get('judged') and entry.get('parse_ok') is False)):
            # A previously failed judge call is not a real score: retry it.
            entry['judged'] = False
            entry['composite'] = None
        entry.update({
            'task': run['task'],
            'model': run['model'],
            'date_utc': run['date_utc'],
            'result_status': run['result_status'],
            'dnf': dnf,
            'svg_sha256': sha,
        })
    return store['runs']


def judge_one(run, store, judge_model, input_mode, force):
    """Judge a single run (or assign deterministic zeros for a DNF)."""
    run_id = run['run_id']
    entry = store['runs'][run_id]
    v = run['validation']
    sha = v.get('sha256') or 'none'

    if entry['dnf']:
        entry.update({
            'judged': True, 'composite': 0.0, 'dnf': True,
            'scores': {k: 0 for k in DIMENSIONS},
            'reasons': {k: '' for k in LLM_DIMS},
            'technical_validity': 0, 'technical_checks': {},
            'input_mode': 'none', 'judge_model': None, 'parse_ok': True,
            'cache_key': None,
        })
        return 'dnf'

    cache_key = '|'.join((sha, RUBRIC_VERSION, judge_model, input_mode))
    entry['cache_key'] = cache_key
    cached = store['judgments'].get(cache_key)
    if cached and cached.get('parse_ok') is False:
        # Old stores may contain a failed call cached as a zero score.
        del store['judgments'][cache_key]
        cached = None
    if cached and not force:
        _apply_cached(entry, cached, judge_model, input_mode)
        return 'cached'

    tmpdir = tempfile.mkdtemp(prefix='judge-frames-', dir=LOGS)
    try:
        sheet, render_ok, frames = render_contact_sheet(run['svg_path'], tmpdir)
        try:
            with open(run['svg_path'], encoding='utf-8', errors='replace') as fh:
                svg_text = fh.read()
        except OSError:
            svg_text = ''
        truncated = len(svg_text) > SVG_CAP
        if truncated:
            head = svg_text[:int(SVG_CAP * 0.7)]
            tail = svg_text[-int(SVG_CAP * 0.3):]
            svg_text = (head + '\n... [SVG SOURCE TRUNCATED] ...\n' + tail)

        prompt = build_judge_prompt(
            load_prompt_text(run['task'], run['prompt_file']),
            svg_text, source_facts_block(v), input_mode, truncated)
        log_path = os.path.join(LOGS, f"judge__{run_id}.ndjson")
        text, errors, code = run_judge(
            prompt, judge_model, sheet, input_mode, log_path)
        verdict = parse_answer(text)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if not verdict['parse_ok']:
        # A free endpoint dropped the connection or the model produced no
        # usable JSON. Do NOT cache a fake zero: leave this run unjudged so the
        # next invocation retries it. Failed calls never enter the ranking.
        entry.update({
            'judged': False,
            'composite': None,
            'cache_key': cache_key,
            'parse_ok': False,
            'parse_note': 'judge call returned no usable JSON — retried on the next run',
            'judge_errors': errors[:3],
            'input_mode': input_mode,
            'judge_model': judge_model,
        })
        return 'failed'

    tech_score, tech_checks = technical_validity(v, render_ok)
    scores = dict(verdict['scores'])
    scores[TECHNICAL_DIM] = tech_score
    result = {
        'scores': scores,
        'reasons': verdict['reasons'],
        'summary': verdict['summary'],
        'technical_validity': tech_score,
        'technical_checks': tech_checks,
        'composite': composite(scores),
        'parse_ok': verdict['parse_ok'],
        'frames': frames,
        'render_ok': render_ok,
        'judged_utc': now_utc(),
        'input_mode': input_mode,
        'judge_exit_code': code,
        'judge_errors': errors[:3],
    }
    store['judgments'][cache_key] = result
    _apply_cached(entry, result, judge_model, input_mode)
    return 'judged'


def _apply_cached(entry, result, judge_model, input_mode):
    scores = dict(result['scores'])
    scores.setdefault(TECHNICAL_DIM, result.get('technical_validity', 0))
    entry.update({
        'judged': True,
        'composite': result['composite'],
        'scores': scores,
        'reasons': dict(result.get('reasons') or {}),
        'technical_validity': result.get('technical_validity'),
        'technical_checks': result.get('technical_checks') or {},
        'input_mode': input_mode,
        'judge_model': judge_model,
        'parse_ok': bool(result.get('parse_ok')),
        'frames': result.get('frames'),
        'render_ok': result.get('render_ok'),
        'parse_note': '' if result.get('parse_ok') else 'judge answer could not be parsed',
    })


def recompute_ranking(store):
    """One row per model, using its newest run per task (mirrors app.js)."""
    runs = store['runs']
    latest = {}   # (model, task) -> run_id
    for run_id, entry in runs.items():
        if not entry.get('judged'):
            continue
        key = (entry['model'], entry['task'])
        prev = latest.get(key)
        if prev is None or str(entry.get('date_utc') or '') >= str(runs[prev].get('date_utc') or ''):
            latest[key] = run_id

    by_model = {}
    for run_id in latest.values():
        e = runs[run_id]
        by_model.setdefault(e['model'], []).append((e['task'], e))

    ranking = []
    for model, pairs in by_model.items():
        dims = {}
        for k in DIMENSIONS:
            vals = [e['scores'].get(k, 0) for _, e in pairs
                    if isinstance(e.get('scores'), dict)]
            dims[k] = round(sum(vals) / len(vals), 2) if vals else 0.0
        # recompute composite from raw dimension scores so the /100 rescale
        # applies even to runs judged before the formula changed
        for _, e in pairs:
            e['composite'] = composite(e.get('scores') or {})
        composites = [e['composite'] for _, e in pairs]
        ranking.append({
            'rank': 0,
            'model': model,
            'mean_composite': round(sum(composites) / len(composites), 3),
            'n_runs': len(pairs),
            'dnf': sum(1 for _, e in pairs if e.get('dnf')),
            'dimensions': dims,
            'per_task': {
                task: {'composite': e['composite'],
                       'scores': e.get('scores'), 'dnf': bool(e.get('dnf'))}
                for task, e in pairs
            },
        })
    ranking.sort(key=lambda r: (-r['mean_composite'], r['model']))
    for i, row in enumerate(ranking, 1):
        row['rank'] = i
    store['ranking'] = ranking
    store['meta']['n_judged'] = sum(1 for e in runs.values() if e.get('judged'))
    store['meta']['n_dnf'] = sum(1 for e in runs.values() if e.get('judged') and e.get('dnf'))
    return ranking


def short(model):
    return model.rsplit('/', 1)[-1]


def print_report(store):
    ranking = store['ranking']
    meta = store['meta']
    probe = meta.get('probe') or {}
    print()
    print('svg-bench automated judge ranking  '
          f"(rubric {store.get('rubric_version')}, judge {short(store.get('judge_model') or '?')}, "
          f"mode {probe.get('mode') or 'unknown'})")
    print(f"{len(ranking)} models · {meta.get('n_judged')} judged · "
          f"{meta.get('n_dnf')} DNF · updated {store.get('updated_utc')}")
    if not ranking:
        print('No judged runs yet. Run `python3 judge.py --limit 5` first.')
        return ''
    header = (f"{'#':>3}  {'model':<38} {'score':>6} {'qual':>5} {'instr':>5} "
              f"{'crea':>5} {'anim':>5} {'rich':>5} {'aesth':>5} {'tech':>5} {'dnf':>4}")
    lines = [header, '-' * len(header)]
    for r in ranking:
        d = r['dimensions']
        lines.append(
            f"{r['rank']:>3}  {short(r['model']):<38} {r['mean_composite']:>6.2f} "
            f"{d.get('output_quality', 0):>5.1f} {d.get('instruction_following', 0):>5.1f} "
            f"{d.get('creativity', 0):>5.1f} {d.get('animation_fidelity', 0):>5.1f} "
            f"{d.get('richness_detail', 0):>5.1f} {d.get('composition_aesthetics', 0):>5.1f} "
            f"{d.get('technical_validity', 0):>5.1f} {r['dnf']:>4}")
    table = '\n'.join(lines)
    print(table)

    md = [
        '# svg-bench automated judge ranking', '',
        f"Rubric `{store.get('rubric_version')}` · judge "
        f"`{store.get('judge_model')}` · input mode `{probe.get('mode') or 'unknown'}` "
        f"· updated {store.get('updated_utc')}", '',
        'Weights: ' + ', '.join(f"{k}={v}" for k, v in WEIGHTS.items()), '',
        '```', table, '```', '',
        'Dimensions: output_quality, instruction_following, creativity, '
        'animation_fidelity, richness_detail, composition_aesthetics '
        '(1-5, judged blind) and technical_validity (1-5, deterministic). '
        'DNF runs score 0. See `judgments.json` for per-run reasons.', '',
    ]
    return '\n'.join(md)


def write_report(md):
    if not md:
        return
    os.makedirs(RESULTS, exist_ok=True)
    with open(REPORT_PATH, 'w', encoding='utf-8') as fh:
        fh.write(md)


def main():
    ap = argparse.ArgumentParser(description='Automated blind LLM judge for svg-bench')
    ap.add_argument('--judge-model', default=None,
                    help='free model to judge with (default: probe the free candidates)')
    ap.add_argument('--force', action='store_true', help='re-probe and re-judge everything')
    ap.add_argument('--limit', type=int, default=0, help='max new judge calls this run (0 = all)')
    ap.add_argument('--dry-run', action='store_true', help='show the plan; make no model calls')
    ap.add_argument('--probe', action='store_true', help='only probe vision support, then exit')
    ap.add_argument('--report', '--recompute', action='store_true',
                    help='re-rank from cache only; no model calls, no rendering')
    ap.add_argument('--output', default=STORE_PATH, help='path to judgments.json')
    args = ap.parse_args()

    os.makedirs(LOGS, exist_ok=True)
    store = load_store(args.output)
    store['judge_model'] = args.judge_model or JUDGE_MODEL
    store['rubric_version'] = RUBRIC_VERSION
    store['weights'] = WEIGHTS
    store['dimensions'] = list(DIMENSIONS)

    models = [args.judge_model] if args.judge_model else list(CANDIDATES)

    if args.report:
        sync_runs(store)
        recompute_ranking(store)
        write_store(store, args.output)
        write_report(print_report(store))
        return 0

    probe = store['meta'].get('probe')
    stale_probe = bool(args.judge_model and probe
                       and probe.get('candidate') != args.judge_model)
    if args.probe or args.force or stale_probe or not probe:
        if args.dry_run:
            print('judge: dry-run — would probe: ' + ', '.join(models))
            probe = probe or {'candidate': models[0], 'image_ok': False,
                              'mode': 'text', 'detail': 'dry-run, unprobed'}
        else:
            probe = probe_models(models)
        store['meta']['probe'] = probe
        store['judge_model'] = probe['candidate']
        if args.probe:
            write_store(store, args.output)
            print(f"judge: probe result {json.dumps(probe, indent=1)}")
            return 0

    judge_model = probe['candidate']
    input_mode = probe['mode']
    store['judge_model'] = judge_model

    by_id = {r['run_id']: r for r in iter_sidecars()}
    runs = sync_runs(store)
    pending = [rid for rid, e in runs.items() if not e.get('judged')]
    if args.force:
        pending = list(runs)
    print(f"judge: {len(runs)} runs, {len(pending)} to judge "
          f"(model={judge_model}, mode={input_mode}, rubric={RUBRIC_VERSION})")

    if args.dry_run:
        for rid in pending[:args.limit or len(pending)]:
            e = runs[rid]
            print(f"  would {'skip (DNF)' if e['dnf'] else 'judge'}: {rid}")
        print('judge: dry-run — no calls made, nothing written')
        return 0

    counts = {'judged': 0, 'cached': 0, 'dnf': 0, 'failed': 0}
    for rid in pending:
        if args.limit and counts['judged'] >= args.limit:
            break
        run = by_id.get(rid)
        if run is None:
            continue
        outcome = judge_one(run, store, judge_model, input_mode, args.force)
        counts[outcome] = counts.get(outcome, 0) + 1
        e = store['runs'][rid]
        score = e.get('composite')
        label = f"{score:.2f}" if isinstance(score, (int, float)) else '—'
        print(f"  [{outcome:<6}] {label:>5}  {rid}")

    recompute_ranking(store)
    write_store(store, args.output)
    print(f"judge: {counts['judged']} new calls, {counts['cached']} cached, "
          f"{counts['dnf']} DNF, {counts['failed']} failed (will retry)")
    write_report(print_report(store))
    print(f"judge: wrote {os.path.relpath(args.output, ROOT)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

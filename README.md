# svg-bench

> **A blind benchmark for free LLMs on animated-SVG illustration — with an automated, free LLM judge built in.**

Every free model currently served by the `opencode` provider gets the **same
prompt**, in a **fresh empty directory**, with **no reference artwork** stored
or passed in. Models produce their work blind, and we then score it two ways:
deterministic validity checks, plus an **automated, blind LLM judge** that ranks
models on output quality, instruction-following, creativity, animation fidelity,
richness, composition and technical validity — producing a single **composite
score out of 100**.

```
prompts/<task>.txt ──▶ opencode run -m <model>[#<effort>]  (cwd: fresh mktemp dir)
                              │
                              ▼
          results/<task>/<model>__<effort>__<YYYY-MM-DD>.svg
          results/<task>/<model>__<effort>__<YYYY-MM-DD>.json   (sidecar metadata)
          logs/<task>__<model>__<effort>__<date>.ndjson          (raw agent stream, gitignored)
```

No reference SVG is kept in the repo or copied into a run directory. Judging is
done from the generated artifacts alone.

## Why "blind"?

LLMs (including the judge) are contestants here, and they are also graded. To
keep the comparison fair:

- Each run happens in an empty scratch directory — no leaked prior work, no
  reference art.
- The judge never sees a model name, run id, date, path, tokens or any sidecar
  field. It only sees an anonymous *"Submission A"*: the task prompt, capped SVG
  source, objective source facts, and rendered frames.
- The judge model is recorded per run and can be swapped with `--judge-model`.

## Features

- **Zero-cost judging.** The judge is a free vision model
  (`opencode/nemotron-3.5-super-vl-free` by default). No paid endpoints, ever.
- **Reproducible.** Judgment is cached by `svg_sha256 | rubric | judge model |
  input mode`, so re-running costs nothing; `--report` re-ranks with zero calls.
- **Static leaderboard, no backend required.** A model × task matrix, a scored
  **Ranking** tab, sortable **Rank** column, `#rank` badges, and a per-run
  dimension breakdown — all from one `data.json`. GitHub Pages compatible.
- **Human + automated.** Visitors can still leave star ratings and public
  reviews alongside the automated scores.
- **Honest about failure.** Timeouts, rate-limits and invalid output are
  recorded as `result_status`, not hidden — and count as DNF (score 0) in the
  ranking.

## Quick start

```bash
./run.sh --dry-run                       # show the matrix, spend nothing
./run.sh                                 # everything: discovered free models x all tasks
./run.sh --tasks horse-racing            # one task
./run.sh --models opencode/beam-free     # one model (comma-separated list)
./run.sh --effort high                   # effort for capability-capable models (default)
./run.sh --timeout 1200                  # wall-clock budget per model+task (default 900s)
./run.sh --force                         # redo even if today's file exists

# Automated judging (free models only — no paid endpoints)
./run.sh judge                           # judge every unjudged run, then rebuild data.json
./run.sh judge --probe                   # check whether a free model accepts rendered frames
./run.sh judge --limit 5                 # judge only 5 runs (cheap smoke test)
./run.sh judge --dry-run                 # show what would be judged, spend nothing
./run.sh judge --force                   # re-probe and re-judge everything
./run.sh judge --report                  # re-rank from cache, no model calls
./run.sh judge --judge-model opencode/glm-5.3-flash-free
./run.sh --judge                         # run the benchmark, then judge the results
```

Same-day results are **skipped** by default (resume semantics); `--force`
re-runs them. Failed runs are recorded, not hidden: a provider error, timeout
or missing/invalid SVG appears in the sidecar as `result_status`.

## Ad-hoc prompts from the command line

Any prompt can be run as a one-off task — no tasks.yaml entry needed:

```bash
./run.sh --prompt "A crescent moon over quiet pines" --task-id moon-pines
./run.sh --prompt "$(cat my-prompt.txt)" --task-id my-task   # multi-line is fine
```

- `--task-id` is sanitised to `[a-zA-Z0-9._-]`; omitted, it defaults to `adhoc`.
- `--prompt` cannot be combined with `--tasks`.
- Artifacts land in `results/<task-id>/` exactly like registered tasks; the
  sidecar records `prompt_sha256` and `prompt_file: "<command-line>"`.
- `--prompt "..." --new-only` composes with the flag below.

## `--new-only` — run only newly found free models

```bash
./run.sh --new-only        # dry-run shows RUN/SKIP(new-only) too
```

Discovery is live (`opencode models`). With `--new-only`, any
`(task, model, effort)` that already has a sidecar under `results/` (any
date, any task) is skipped, so:

- models that **newly appear** in the free tier are benchmarked on every task selected;
- models already recorded (including failed ones — a sidecar counts as a record) are not re-run;
- `--force` ignores both resume rules and re-runs everything; `--force --tasks horse-racing` re-runs just one task.

Typical loop for a long-running benchmark on a weekly moving target:

```bash
./run.sh --new-only                  # whatever is new since last time
./run.sh --new-only --dry-run        # check what would run first
```

## Models under test

Discovered **live** at run start from `opencode models`, filtered to
`^opencode/*-free$` plus `big-pickle` (the opencode provider's free tier).
The snapshot is written to `logs/models-snapshot-<date>.txt` for provenance.

One effort per model (the `#<effort>` variant syntax of opencode v2):

| Model | Effort used | Why |
|---|---|---|
| beam-free, flint-40b-free, glm-5.3-flash-free, deepseek-v4.1-flash-free, nemotron-3.5-super-vl-free, pulse-alpha-free | `high` | effort-capable |
| big-pickle, mimo-v2.6-flash-free, nemotron-3.5-lightning-free | `default` | no effort knob in provider metadata |
| crouton-alpha-free | `high` | support unverified; if the provider rejects the variant, the runner retries once with `default` and marks `effort_fallback: true` *(region-blocked from the build machine; recorded as an error there)* |

Models without effort support are run with no variant; their results are
comparable to "high-effort" runs of other models only in the loose sense that
both are single fixed settings.

## Filename convention

`results/<task>/<provider>_<model>__<effort>__<YYYY-MM-DD>.svg`

e.g. `results/sloth-excavator/opencode_beam-free__high__2026-11-05.svg`

The task is the directory, the model and effort are in the filename, and the
date is `date -u +%Y-%m-%d`. The companion `.json` sidecar carries everything
else that is permanently important:

- `model`, `variant`, `effort`, `effort_requested`, `effort_fallback`
- `date_utc`, `opencode_version`, `prompt_sha256` (identical prompt ⇒ identical hash)
- `wall_seconds`, `steps`, `events`, `session_id`, `tokens` (in/out/reasoning/cache)
- `collection_method` (`output.svg` = model wrote the file; `extracted` = SVG
  recovered from the agent transcript; `missing`)
- `validation`: `size_bytes`, `sha256`, `well_formed_xml`, `root_is_svg`,
  `viewBox`, `elements_total`, `element_counts`, `animation_elements`,
  `gradient_defs`, `script_blocks`, `external_refs`, `rendered`
- `result_status`: `ok` | `invalid_svg` | `timeout` | `no_output`
- `error_type` / `error_message` when the provider refused the request

`results/summary-<date>.json` aggregates all sidecars into one matrix.

## Judging (automated, blind, free)

Validity checks are deterministic, but *quality* of an SVG is not. `judge.py`
adds an automated, **blind** LLM judge alongside the existing human star ratings
and public reviews. It never uses a paid endpoints: the default judge is a free
vision model (`opencode/nemotron-3.5-super-vl-free`).

One command judges everything unjudged and rebuilds the leaderboard data:

```bash
./run.sh judge        # or: python3 judge.py && python3 leaderboard.py
```

Results go to `judgments.json` (commit it — GitHub Pages rebuilds `data.json`
from it) and a readable ranked table to `results/judge-report.md`. Scores appear
on the site as a sortable **Rank** column (score out of 100), a `#rank` badge in
the matrix name cell, a per-run dimension breakdown in the preview, and a
dedicated **Ranking** tab.

### What is scored

Seven dimensions, each 1–5, combined into a weighted composite scaled to **0–100**:

| Dimension | Weight | Source |
|---|---|---|
| `output_quality` | 0.22 | judge — does the rendered artwork look good? |
| `instruction_following` | 0.20 | judge — every hard requirement and "must include" |
| `creativity` | 0.10 | judge — originality while still on-brief |
| `animation_fidelity` | 0.12 | judge — motion actually works, not just exists |
| `richness_detail` | 0.10 | judge — layered shapes, gradients, detail |
| `composition_aesthetics` | 0.11 | judge — layout, color, polish |
| `technical_validity` | 0.15 | deterministic — never sent to the judge |

`composite = round(Σ weight × score × 20, 2)` → a score from 0 to 100 (it hits 0
only for DNF runs). `technical_validity` itself is 1–5 and computed deterministically:

`technical_validity = 1 + (valid XML & SVG root) + (xmlns & viewBox/size) +
(no external refs & no scripts) + (rendered OK)`, clamped to 1–5.

Runs that failed (`result_status != ok`, missing SVG) are marked DNF, skip the
judge call entirely, and score 0 — they still count in the model's ranking
average.

Ranking mirrors the site's matrix: for each model, the newest run per task (by
`date_utc`) is used, and the mean composite across those tasks decides the rank.

### How judging works

1. **Render.** Headless Chrome captures the SVG at three animation times
   (`--virtual-time-budget` 400 / 1500 / 3000 ms); one frame is unreliable
   because animated elements can be mid-translation and off-canvas. The three
   frames are stacked into one PNG with PIL.
2. **Probe.** `--probe` attaches a small red PNG to each free candidate model
   and asks its colour. If a model sees it, `input_mode = image`; otherwise
   `input_mode = text` and judging falls back to the SVG source only (visual
   scores are then less reliable, and this is shown in the UI).
3. **Blind prompt.** The judge sees only an anonymous *"Submission A"*: the
   exact task prompt, capped SVG source, objective source facts, and the rendered
   frames. No model name, run id, date, path, tokens or sidecar identity — the
   run id embeds the model name, so it is deliberately withheld. The attached
   image is copied to an anonymously named `submission.png`.
4. **Parse.** `opencode run --format json` NDJSON text parts are concatenated;
   the last balanced JSON object is parsed, scores clamped to 1–5, and a
   malformed answer is recorded as `parse_ok: false` instead of crashing.
5. **Cache.** Judgments are keyed by
   `svg_sha256 | rubric_version | judge_model | input_mode`, so re-running is
   free. Change any of those (or use `--force`) to re-judge. A transient
   provider error (free endpoints drop connections) is **not** cached as a
   zero: the run stays unjudged and is retried on the next invocation.
   `--report` recomputes the ranking from the cache with zero model calls.

Because `leaderboard.py` runs after every benchmark run, judge data lives in
`judgments.json` rather than in the sidecars. Unjudged runs simply show "not
judged"; the site renders fine when no judging has happened yet.

Caveat: the free judge model is itself a contestant in this benchmark. Blind
presentation mitigates but does not remove self-preference bias; the judge model
is recorded per run and can be swapped with `--judge-model`. Absolute 1–5
rubric scores are cheaper and reproducible but noisier than pairwise comparison.

## Leaderboard (`site/`)

Self-updating, static ratings leaderboard for the artifacts. One command:

```bash
./run.sh result           # regenerate data.json + serve http://127.0.0.1:8000/site/
./run.sh result 8001      # different port (auto-picks next port if taken)
./run.sh --result         # same as above
```

`run.sh` refreshes `site/data.json` after **every single run**, and the page
re-fetches it every 10s — leave the tab open and new results simply appear
(the tabs split currently-free models from every model ever tested, and a
**Ranking** tab shows the automated scores). The server (`serve.py`) is
whitelist-only for static files: it serves `/site` and `/results` artifacts — no
logs, no other files. Its only write action is the local-only admin re-run API
on `127.0.0.1`; GitHub Pages has no such endpoint and normal visitors never see
the admin buttons.

Features: a model × task matrix — one row per model (shown once), one artwork column per task — so each task's SVG sits in its own column; hover or click any cell to load it in the huge live preview stage; star ratings and notes are yours, stored locally, sortable and filterable; an Export/Import ratings flow; per-model availability badges; zero dependencies, GitHub Pages compatible. The matrix is sorted by the automated **Rank** score by default, and each row carries a `#rank` badge. In the preview pane, a corner arrow (↗) on the artwork opens the raw SVG in a new window; matrix cells have a ▶ control to re-run the animation in the preview.

The model **name cell is the scorecard**: it shows the benchmark result (`valid/total`), the per-task result chips, the public-review average/count, and the automated rank + score.

## GitHub Pages

The site is plain static files. `build_site.py` assembles a deployable tree with
the artifacts beside the page, so relative `results/...` URLs work on GitHub
Pages project sites (including `https://<user>.github.io/<repo>/`).

```bash
python3 build_site.py              # -> svg-bench/_site
python3 build_site.py --out _site  # custom output
```

Deployment is handled by `.github/workflows/pages.yml`:

- **This monorepo:** the root workflow deploys the terminal-bench site at `/`
  and this leaderboard at `/svg-bench/`.
- **Standalone repository:** copy/keep `svg-bench/.github/workflows/pages.yml`
  at the root of that repository; it deploys this page at `/`.

Set Pages to **GitHub Actions** in the repository settings. No server, build
step, database or Netlify configuration is required.

## Public reviews (Supabase)

Every visitor can read and submit public reviews; the aggregate appears in each
model's name cell, and the preview lists every review with its reviewer, stars,
date and note. The backend is a single Supabase table:

1. Run `svg-bench/supabase.sql` in the Supabase SQL editor.
2. Put the project URL and **anon** key in `svg-bench/site/config.js`:

   ```js
   window.SVG_BENCH_CONFIG = {
     supabase: {
       url: 'https://YOUR_PROJECT.supabase.co',
       anonKey: 'YOUR_PUBLIC_ANON_KEY',
       table: 'svgbench_reviews'
     }
   };
   ```

   Or inject them during the Pages build by setting repository variables
   `SUPABASE_URL`, `SUPABASE_ANON_KEY` and optionally
   `SUPABASE_REVIEWS_TABLE`.

The anon key is public; row-level security in `supabase.sql` limits it to
reading non-revoked reviews and inserting new ones. Moderate spam by setting
`revoked = true` with the service-role key. If no backend is configured the
page still renders and falls back to private/local ratings.

## Admin-only re-runs

`↻ Re-run failed`, `↻ Re-run all`, and the per-cell `↻` buttons are **not**
shown to normal visitors. They only appear when the page is served by the local
`serve.py` server and the admin flag is enabled:

```bash
./run.sh result
# open http://127.0.0.1:8000/site/?admin=1
```

On GitHub Pages there is no local action API, so the buttons stay hidden even if
the query string is edited. Run benchmarks locally (or from a maintainer-only
workflow) and push the resulting artifacts; Pages redeploys automatically.

## Adding a task

1. `prompts/<id>.txt` — self-contained prompt, ends with "save it as `output.svg`".
2. Add an entry to `tasks.yaml` (`id`, `prompt`, `blurb`).
3. `./run.sh --tasks <id>`.

Adding a model: nothing to do — discovery is live. To force specific models,
use `--models a,b,c`.

## Caveats (read before quoting numbers)

- **Free models evaporate.** The list changes weekly. A score describes *this
  date, this build of opencode, this provider* — not a fixed thing. The snapshot
  and per-sidecar `date_utc` exist so a reader can tell what was measured.
- **svg-bench is one prompt per task.** It measures a specific kind of
  generative illustration, not general capability.
- **`result_status: no_output` is a real result**, not a harness bug: free
  endpoints rate-limit, region-block and silently fail. Those events are
  recorded with the provider's own error message.
- Crouton-alpha-free is region-blocked from the machine this was built on
  (`provider.auth` / `RegionError`); it will appear in results as an error
  until run from an allowed region.

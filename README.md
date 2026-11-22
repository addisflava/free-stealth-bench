# free-stealth-bench

> **A blind benchmark for LLMs on animated-SVG illustration — with an automated, blind LLM judge and a self-updating leaderboard.**

Every model gets the **same prompt**, in a **fresh empty directory**, with **no reference artwork** stored or passed in. Models produce their work blind, and the benchmark scores it two ways: deterministic validity checks, plus an **automated, blind LLM judge** that ranks output quality, instruction-following, creativity, animation fidelity, richness, composition and technical validity.

The runner is **provider- and harness-agnostic**. The same benchmark can drive the OpenCode CLI, direct OpenAI/Anthropic-compatible APIs, Codex/Claude/Gemini CLIs, or any custom command-line agent. Adding a provider or an agent is a YAML entry, not a code change.

```
prompts/<task>.txt ──► runner.py ──► provider/model  or  harness@provider/model
                                     (fresh mktemp dir, no reference art)
                                            │
                                            ▼
       results/<task>/<provider>_<model>__<effort>__<YYYY-MM-DD>.svg
       results/<task>/<provider>_<model>__<effort>__<YYYY-MM-DD>.json   (sidecar)
       logs/<task>__<provider>_<model>__<effort>__<date>.ndjson         (raw stream)
       logs/... .meta.json                                               (normalized run meta)
```

No reference SVG is kept in the repository or copied into a run directory. Judging is done from the generated artifacts alone.

## Why "blind"?

LLMs (including the judge) are contestants here, and they are also graded. To keep the comparison fair:

- Each run happens in an empty scratch directory — no leaked prior work, no reference art.
- The judge never sees a model name, run id, date, path, tokens or any sidecar field. It only sees an anonymous *"Submission A"*: the task prompt, capped SVG source, objective source facts, and rendered frames.
- The judge model is recorded per run and can be swapped with `--judge-model`.
- Every model in a comparison gets byte-identical prompt text. The runner does not add per-model hints.

## Features

- **Any provider, any harness.** Providers and agent CLIs live in `providers.yaml` and `harnesses.yaml`. Cloud APIs activate when their key is exported; local endpoints are opt-in; alternate harnesses are selected with `harness@provider/model`.
- **Zero-cost judging.** The default judge is a free vision model (`opencode/nemotron-3.5-super-vl-free`), with other free vision models as fallback candidates. No paid endpoints are required. New outputs are scored and the ranking is refreshed automatically at the end of every `./run.sh` (skip with `--no-judge`).
- **Reproducible.** Judgment is cached by `svg_sha256 | rubric | judge model | input mode`, so re-running costs nothing; `--report` re-ranks with zero calls.
- **Static leaderboard, no backend required.** A model × task matrix, a scored **Ranking** tab, sortable scores, `#rank` badges, and per-run dimension breakdowns — all from one `data.json`. GitHub Pages compatible.
- **Human + automated.** Visitors can leave **signed −5 … +5** private ratings and public reviews alongside the automated scores.
- **Honest about failure.** Timeouts, rate-limits, invalid output and provider refusals are recorded as `result_status`, not hidden — and count as DNF (score 0) in the ranking.

## Quick start

```bash
./run.sh --dry-run                       # show the plan, spend nothing
./run.sh                                 # every discovered target × every task
./run.sh --tasks horse-racing            # one task
./run.sh --models opencode/beam-free     # one target (comma-separated list)
./run.sh --models opencode/beam-free,opencode/glm-5.3-flash-free
./run.sh --provider openrouter           # discovery limited to one provider
./run.sh --all-harnesses                 # default + alternate harness per provider
./run.sh --effort high                   # override the per-provider effort default
./run.sh --timeout 1200                  # per-run wall-clock budget (default 900s)
./run.sh --retries 2                     # extra attempts on transient provider failures
./run.sh --no-progress-timeout 240       # kill a stalled agent after Ns without output (0=off)
./run.sh --quarantine-threshold 3        # skip pairs with N consecutive DNFs (0=off)
./run.sh --force                         # redo even if today's file exists

New outputs are judged automatically at the end of every `./run.sh` and the ranking is refreshed; pass `--no-judge` to benchmark without judging.

# Automated judging (free models only — no paid endpoints)
./run.sh --no-judge                      # benchmark only, skip judging
./run.sh judge                           # judge every unjudged run, then rebuild data.json
./run.sh judge --probe                   # check whether a free model accepts rendered frames
./run.sh judge --limit 5                 # judge only 5 runs (cheap smoke test)
./run.sh judge --dry-run                 # show what would be judged, spend nothing
./run.sh judge --force                   # re-probe and re-judge everything
./run.sh judge --report                  # re-rank from cache, no model calls
./run.sh judge --judge-model opencode/glm-5.3-flash-free
```

A `(task, model, effort)` whose newest sidecar is `result_status: ok` is **skipped** on the next `./run.sh` (resume semantics). Runs that failed, timed out, produced invalid SVG or no SVG are **retried automatically**. Failed runs are recorded, not hidden: a provider error, timeout or missing/invalid SVG appears in the sidecar as `result_status`.

### Reliability safeguards

Free endpoints drop connections, return 502/504 and stall mid-stream. The runner handles that without wasting the whole wall-clock budget:

- **No-progress watchdog** (default 240s): a stalled agent — emitting only `step-start` events, no text/tool call/completion — is killed early and recorded as `timeout` with `error_type: no_progress`. Plain-text harness output counts as progress too.
- **Bounded retries** (default 2 extra attempts): transport errors, 502/504, stalls and silent failures are retried with exponential backoff (default 5s, 10s). The total run still fits inside `--timeout`; auth, quota, invalid-variant and config errors are terminal and are never retried.
- **DNF quarantine** (default 3): a pair with 3 consecutive non-`ok` sidecars is skipped on later invocations instead of burning 15 minutes each time. Use `--force` or `--quarantine-threshold 0` to retry it anyway.

The sidecar records `attempts` and `retry_errors` for retried runs. Before every run the runner deletes the old SVG and meta for that date, so an interrupted rerun can never inherit a stale result. All three knobs can also be set via `BENCH_NO_PROGRESS_TIMEOUT`, `BENCH_RETRIES`, `BENCH_RETRY_DELAY`, `BENCH_ATTEMPT_TIMEOUT` and `DNF_QUARANTINE_THRESHOLD`.

## Providers and agent harnesses

### Target syntax

| Target | Meaning |
|---|---|
| `provider/model` | Run the model through the provider's default harness. |
| `harness@provider/model` | Run the same model through an alternate harness. |

Examples:

```bash
opencode/deepseek-v4.1-flash-free            # OpenCode CLI, default harness
openai/gpt-5.2                               # direct OpenAI-compatible API
codex@openai/gpt-5.2-codex                   # Codex CLI, using OpenAI's model id
claude-code@anthropic/claude-sonnet-4-5      # Claude Code CLI
gemini-cli@google/gemini-2.5-pro             # Gemini CLI
```

The same model can appear more than once when it is run through different harnesses. The leaderboard groups rows by **`(harness, model)`**, so `openai/gpt-5.2` and `codex@openai/gpt-5.2` are separate entries with separate artifacts, scores and ranks.

### The two registries

`providers.yaml` describes **endpoints**; `harnesses.yaml` describes **how an agent is invoked**.

A provider entry:

```yaml
providers:
  my-provider:
    label: My Provider
    harness: openai                 # default harness id
    alternate_harnesses: [codex]    # optional, selectable with harness@...
    enabled: auto                   # true | false | auto (auto = key is set)
    base_url: https://api.example.com/v1
    api_key_env: MY_PROVIDER_API_KEY
    headers:                        # optional HTTP headers
      X-Title: free-stealth-bench
    discover:
      type: openai-models           # command | openai-models | static | none
      include: 'my-model-prefix'     # optional regex
      exclude: 'embedding|tts'       # optional regex
    effort:
      default: high
      unsupported_models: [some-model-without-reasoning]
```

The bundled cloud providers are `enabled: auto`: they join discovery as soon as the matching key is present.

| Provider | Key | Notes |
|---|---|---|
| `openai` | `OPENAI_API_KEY` | OpenAI-compatible `/chat/completions`; Codex CLI as an alternate harness. |
| `openrouter` | `OPENROUTER_API_KEY` | Free tier only by default (`:free$` discovery filter). |
| `groq` | `GROQ_API_KEY` | OpenAI-compatible. |
| `deepseek` | `DEEPSEEK_API_KEY` | OpenAI-compatible. |
| `google` | `GEMINI_API_KEY` | Gemini via the OpenAI-compatible endpoint; Gemini CLI as an alternate. |
| `anthropic` | `ANTHROPIC_API_KEY` | Anthropic Messages API; Claude Code CLI as an alternate. |
| `ollama` | *(keyless)* | Local; `enabled: false` by default. |
| `lmstudio` | *(keyless)* | Local; `enabled: false` by default. |
| `codex-cli` | *(Codex login)* | Static model list; `enabled: false` by default. |

Keys are read from the environment only. They are never written into a sidecar or the leaderboard. You can point the runner at alternate registries:

```bash
PROVIDERS_FILE=/path/to/providers.yaml \
HARNESSES_FILE=/path/to/harnesses.yaml \
./run.sh --tasks sloth-excavator
```

### Harness types

`harnesses.yaml` ships with five adapter families:

| Type | What it does | Log |
|---|---|---|
| `opencode` | `opencode run --format json --auto -m <model>[#effort] <prompt>` | NDJSON |
| `openai` | `POST <base_url>/chat/completions` | normalized NDJSON |
| `anthropic` | `POST <base_url>/messages` | normalized NDJSON |
| `command` | runs an arbitrary argv list in the fresh working directory | captured stdout |
| *presets* | `codex`, `claude-code`, `gemini-cli` are `command` harnesses | captured stdout |

The `command` adapter has no shell; each argv item is substituted independently. Supported placeholders:

| Placeholder | Value |
|---|---|
| `{provider}` | provider id (`openai`) |
| `{model}` | bare model id (`gpt-5.2`) |
| `{target}` | canonical target (`openai/gpt-5.2`), or `harness@provider/model` |
| `{harness}` | harness id (`codex`) |
| `{effort}` | resolved effort (`high` / `default`) |
| `{prompt}` | full prompt text |
| `{prompt_file}` | path to a file containing the prompt |
| `{workdir}` | fresh empty working directory |
| `{output}` | expected output filename (`output.svg`) |
| `{timeout}` | per-run wall-clock seconds |

Example custom agent:

```yaml
harnesses:
  my-agent:
    type: command
    command: ["my-agent", "--model", "{model}", "--cwd", "{workdir}",
              "--prompt-file", "{prompt_file}", "--out", "{output}"]
    version_command: ["my-agent", "--version"]
    effort_style: flag
    effort_flag: ["--effort", "{effort}"]
```

Then point a provider at it:

```yaml
providers:
  my-models:
    harness: my-agent
    enabled: true
    discover:
      type: static
      models: [my-model-a, my-model-b]
```

```bash
./run.sh --models my-models/my-model-a --tasks sloth-excavator
```

The harness must write `output.svg` in `{workdir}` (or, for the API adapters, return the SVG in the response). If the agent writes nothing, the run is recorded as `no_output`; if it writes invalid XML, as `invalid_svg`.

### Effort

Effort is resolved in this order:

1. `--effort <value>` on the command line (an explicit override).
2. The provider's `effort.default`.
3. The registry-wide `defaults.effort` (default `high`).

The harness must support the knob: `variant` (OpenCode appends `#effort`), `reasoning_effort` (OpenAI-style body field), `flag` (extra argv), or `none` (run at `default`). Models listed in `effort.unsupported_models` always run at `default`. If the provider rejects the variant, the run is retried once at `default` and the sidecar records `effort_fallback: true`.

### Discovery

`runner.py discover` understands:

- `command` — run an argv, one model per line (OpenCode does this).
- `openai-models` — `GET <base_url>/models`, parse OpenAI's `data[].id` shape.
- `static` — a YAML list (useful for CLIs with closed model lists).
- `none` — no discovery; pass explicit `--models` targets.

Inspect what the runner sees before spending anything:

```bash
python3 runner.py list
python3 runner.py discover
python3 runner.py discover --provider openai --all-harnesses
python3 runner.py effort --target codex@openai/gpt-5.2-codex
```

## Models under test

Discovery is live at run start. The default OpenCode provider keeps the free tier plus `big-pickle`:

```yaml
include: '^opencode/(?:[a-z0-9._-]+-free|big-pickle)$'
```

Widen or remove that filter in `providers.yaml` to include paid OpenCode models. The latest unfiltered discovery is written to `logs/models-snapshot-<date>.txt` for provenance; only `discover` runs without `--provider` or `--all-harnesses` write this public snapshot, so a filtered run cannot accidentally shrink the **current models** list. The leaderboard's **current models** tab uses that snapshot, while **all models** keeps every model ever tested.

The original effort mapping for the OpenCode free tier (from `providers.yaml`):

| Models | Effort |
|---|---|
| `beam-free`, `flint-40b-free`, `glm-5.3-flash-free`, `deepseek-v4.1-flash-free`, `nemotron-3.5-super-vl-free`, `pulse-alpha-free` | `high` |
| `big-pickle`, `iceman-free`, `longcat-2.5-free`, `mimo-v2.6-flash-free`, `nemotron-3.5-lightning-free` | `default` (`high` variant rejected by the provider) |
| `crouton-alpha-free` | `high`; region-blocked on the build machine, recorded as an error there |

Models without an effort knob are run with no variant; their results are comparable to "high-effort" runs of other models only in the loose sense that both are single fixed settings.

## Filename convention

```
results/<task>/<provider>_<model>__<effort>__<YYYY-MM-DD>.svg
```

Examples:

```
results/sloth-excavator/opencode_beam-free__high__2026-11-05.svg
results/sloth-excavator/openai_gpt-5.2__high__2026-11-06.svg
results/horse-racing/codex_openai_gpt-5.2-codex__default__2026-11-06.svg
```

For alternate harnesses the slug is `harness_provider_model` (the `@` and `/` become `_`), so two harnesses on the same model never overwrite each other. The task is the directory, the provider/model and effort are in the filename, and the date is `date -u +%Y-%m-%d`.

The companion `.json` sidecar carries everything permanently important:

- `provider`, `harness`, `target`, `model`, `model_id`
- `variant`, `effort`, `effort_requested`, `effort_fallback`
- `date_utc`, `agent_version`, `prompt_sha256` (identical prompt ⇒ identical hash)
- `wall_seconds`, `steps`, `events`, `session_id`, `tokens` (in/out/reasoning/cache), `attempts`, `retry_errors`
- `collection_method` (`output.svg` = agent wrote the file; `extracted` = SVG recovered from the transcript/response; `missing`)
- `validation`: `size_bytes`, `sha256`, `well_formed_xml`, `root_is_svg`, `viewBox`, `elements_total`, `element_counts`, `animation_elements`, `gradient_defs`, `script_blocks`, `external_refs`, `rendered`
- `result_status`: `ok` | `invalid_svg` | `timeout` | `no_output`
- `error_type` / `error_message` when the provider refused the request

`results/summary-<date>.json` aggregates all sidecars into one matrix.

Old sidecars without `provider` / `harness` / `target` still load: the leaderboard infers `provider` from the model prefix and the `harness` from that provider's default harness in `providers.yaml` (for example `openrouter` → `openai`), using `provider/model` as the target when the harness is that default. This keeps historical artifacts compatible without ever mislabelling a provider as an agent.

## Judging (automated, blind, free)

Validity checks are deterministic, but *quality* of an SVG is not. `judge.py` adds an automated, **blind** LLM judge alongside the human ratings and public reviews. It never uses paid endpoints: the default judge is a free vision model (`opencode/nemotron-3.5-super-vl-free`). New outputs are judged automatically at the end of every `./run.sh`; re-rank manually with:

```bash
./run.sh judge        # or: python3 judge.py && python3 leaderboard.py
```

Results go to `judgments.json` (commit it — GitHub Pages rebuilds `data.json` from it) and a readable ranked table to `results/judge-report.md`. Scores appear on the site as a sortable **Rank** column, a `#rank` badge in the matrix name cell, a per-run dimension breakdown in the preview, and a dedicated **Ranking** tab.

**Known limitation:** the judge invokes the OpenCode CLI (`opencode run --format json`) to reach the free judge model. Judging with a non-OpenCode harness is not wired in yet; `runner.py call` already knows how to make a one-shot text call for OpenAI/Anthropic/command harnesses, but `judge.py` does not use it. This affects the *judge*, not the benchmark contestants.

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

`composite = round(Σ weight × score × 20, 2)` → a score from 0 to 100 (it hits 0 only for DNF runs). `technical_validity` itself is 1–5 and computed deterministically:

`technical_validity = 1 + (valid XML & SVG root) + (xmlns & viewBox/size) + (no external refs & no scripts) + (rendered OK)`, clamped to 1–5.

Runs that failed (`result_status != ok`, missing SVG) are marked DNF, skip the judge call entirely, and score 0 — they still count in the model's ranking average.

Ranking uses the newest run per task for each **`(harness, model)`** pair. That means `openai/gpt-5.2` and `codex@openai/gpt-5.2` can rank differently even though they share a model id: they are different agent pipelines producing different artifacts.

### How judging works

1. **Render.** Headless Chrome captures the SVG at three animation times (`--virtual-time-budget` 400 / 1500 / 3000 ms); one frame is unreliable because animated elements can be mid-translation and off-canvas. The three frames are stacked into one PNG with PIL.
2. **Probe.** `--probe` attaches a small red PNG to each free candidate model and asks its colour. If a model sees it, `input_mode = image`; otherwise `input_mode = text` and judging falls back to the SVG source only (visual scores are then less reliable, and this is shown in the UI).
3. **Blind prompt.** The judge sees only an anonymous *"Submission A"*: the exact task prompt, capped SVG source, objective source facts, and the rendered frames. No model name, run id, date, path, tokens or sidecar identity — the run id embeds the model name, so it is deliberately withheld. The attached image is copied to an anonymously named `submission.png`.
4. **Parse.** `opencode run --format json` NDJSON text parts are concatenated; the last balanced JSON object is parsed, scores clamped to 1–5, and a malformed answer is recorded as `parse_ok: false` instead of crashing.
5. **Cache.** Judgments are keyed by `svg_sha256 | rubric_version | judge_model | input_mode`, so re-running is free. Change any of those (or use `--force`) to re-judge. A transient provider error is **not** cached as a zero: the run stays unjudged and is retried on the next invocation. `--report` recomputes the ranking from the cache with zero model calls.

Caveat: the free judge model is itself a contestant in this benchmark. Blind presentation mitigates but does not remove self-preference bias; the judge model is recorded per run and can be swapped with `--judge-model`. Absolute 1–5 rubric scores are cheaper and reproducible but noisier than pairwise comparison.

## Leaderboard (`site/`)

Self-updating, static ratings leaderboard for the artifacts. One command:

```bash
./run.sh result           # regenerate data.json + serve http://127.0.0.1:8000/site/
./run.sh result 8001      # different port (auto-picks next port if taken)
./run.sh --result         # same as above
```

`run.sh` refreshes `site/data.json` after **every single run**, and the page re-fetches it every 10s — leave the tab open and new results simply appear. The server (`serve.py`) is whitelist-only for static files: it serves `/site` and `/results` artifacts — no logs, no other files. Its only write action is the local-only admin re-run API; GitHub Pages has no such endpoint and normal visitors never see the admin buttons.

Features:

- **Tabs:** **current models** (free and listed in the latest discovery snapshot), **all models** (every target ever tested), and **new** (only targets with a NEW badge; the tab is hidden when there are none). The **Ranking** tab shows the automated judge table.
- **A model × task matrix**, one row per `(harness, model)` and one artwork column per task. Hover the artwork (not the whole cell) to load it in the live preview stage; click to pin it.
- **Provider and harness tags** in each name cell. Both are always shown: the provider is the endpoint that served the model, the harness is the agent that ran it (e.g. `opencode`, `openai`, `codex`). A `codex@openai/...` row is therefore visibly distinct from `openai/...`.
- **Provider and harness filters**, plus task, status, search and "unrated only".
- **Signed reviews:** private ratings are **−5 … +5** (0 = neutral), shown as stars or crosses. Public reviews use the same signed scale.
- **Two scores:** the calibrated judge score and the **total** score after reviews. The primary `#N · total` badge shows the post-review position; the muted `judge #N` badge shows the judge-only position.

### How the displayed score is calculated

1. The judge's raw composite (0–100) is calibrated linearly so the strongest judged entry scores **65**, matching public-benchmark ranges. Every other entry scales by the same factor; the raw value is preserved in tooltips and the Ranking table.
2. Reviews add or subtract directly: `review_delta = mean(signed stars) × 5`, so one person at −5 moves the score by −25 and one at +5 by +25.
3. `total = clamp(0, 100, calibrated_judge + review_delta)`.
4. If an entry has reviews but no judge score yet, it starts from a **50** baseline and adds the delta.
5. Rank is recomputed over the resulting totals, so the number after the review can differ from the judge-only rank.

## Public reviews (Supabase)

Every visitor can read and submit public reviews; the aggregate appears in each model's name cell, and the preview lists every review with its reviewer, stars, date and note. The backend is a single Supabase table:

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

   Or inject them during the Pages build by setting repository variables `SUPABASE_URL`, `SUPABASE_ANON_KEY` and optionally `SUPABASE_REVIEWS_TABLE`.

The anon key is public; row-level security in `supabase.sql` limits it to reading non-revoked reviews and inserting new ones. The table's `stars` constraint accepts **−5 … +5** (an idempotent `alter` block updates older deployments). Moderate spam by setting `revoked = true` with the service-role key. If no backend is configured the page still renders and falls back to private/local ratings.

## GitHub Pages

The site is plain static files. `build_site.py` assembles a deployable tree with the artifacts beside the page, so relative `results/...` URLs work on GitHub Pages project sites (including `https://<user>.github.io/<repo>/`).

```bash
python3 build_site.py              # -> svg-bench/_site
python3 build_site.py --out _site  # custom output
```

Deployment is handled by `.github/workflows/pages.yml`:

- **This monorepo:** the root workflow deploys the terminal-bench site at `/` and this leaderboard at `/svg-bench/`.
- **Standalone repository:** copy/keep `svg-bench/.github/workflows/pages.yml` at the root of that repository; it deploys this page at `/`.

Set Pages to **GitHub Actions** in the repository settings. No server, build step, database or Netlify configuration is required.

## Reddit announcements

When discovery finds a new model and the judge has ranked it, `publish.sh` also calls `reddit_post.py`. It reuses the same discovery snapshot and judge results that build `site/data.json`, so the post always matches the live leaderboard.

- **Always** writes a ready-to-paste Markdown draft to `posts/<date>-<model>.md`. The draft contains the leaderboard ranking (with the new model marked `<-- NEW`) and a short, data-derived description: rank, score, strongest/weakest rubric dimensions, DNF count and per-task scores.
- **Optionally** submits the post to Reddit when all four OAuth credentials are configured (a registered Reddit *script* app):

  | Variable | Meaning |
  | --- | --- |
  | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Reddit script-app credentials |
  | `REDDIT_USERNAME`, `REDDIT_PASSWORD` | Account the post is submitted from |
  | `REDDIT_SUBREDDITS` | Comma-separated targets, default `opencode,OpenCode` |
  | `REDDIT_USER_AGENT` | Distinct OAuth User-Agent (required by Reddit) |
  | `LEADERBOARD_URL` | Link in the post; otherwise derived from the `origin` remote |
  | `REDDIT_TOP_N` | Number of ranking rows shown, default `15` |
  | `NO_REDDIT=1` | Skip this step entirely (like `NO_PUBLISH`) |
  | `REDDIT_DRY_RUN=1` | Write drafts without calling the Reddit API |

Reddit subreddit names are case-insensitive: `r/opencode` and `r/OpenCode` resolve to the same community, so the configured targets are de-duplicated before submitting (one post per community). Add a genuinely different subreddit to `REDDIT_SUBREDDITS` to cross-post.

`posts/.reddit-state.json` records which models were announced and which subreddits accepted a post, so re-runs never duplicate a draft or a submission; a subreddit that failed is retried on the next run. The whole step is best-effort — a missing credential or a Reddit outage only leaves a draft and never fails publishing. To generate a draft by hand:

```bash
python3 reddit_post.py                       # discovery's "new" models
python3 reddit_post.py --models opencode/new-model-free --dry-run
```

## Admin-only re-runs

`↻ Re-run failed`, `↻ Re-run all`, and the per-cell `↻` buttons are **not** shown to normal visitors. They only appear when the page is served by the local `serve.py` server and the admin flag is enabled:

```bash
./run.sh result
# open http://127.0.0.1:8000/site/?admin=1
```

The re-run API accepts `provider/model` and `harness@provider/model` targets and passes them back to `run.sh`, so an alternate harness can be re-run without hand-editing the results tree. On GitHub Pages there is no local action API, so the buttons stay hidden even if the query string is edited.

## Adding a task, provider or model

**Task**

1. `prompts/<id>.txt` — self-contained prompt, ends with "save it as `output.svg`".
2. Add an entry to `tasks.yaml` (`id`, `prompt`, `blurb`).
3. `./run.sh --tasks <id>`.

**Provider**

Add a block to `providers.yaml` with a `harness`, `base_url` (for API harnesses), a discovery mode and an effort policy. Export the key named by `api_key_env`. No Python changes.

**Agent harness**

Add a `command` block to `harnesses.yaml` with the argv template and placeholder substitutions. Point a provider at it. No Python changes.

**Model**

Nothing to do when the provider supports discovery. To force specific models, use `--models a,b,c` or a static `discover.models` list.

## Caveats (read before quoting numbers)

- **Free models evaporate.** The list changes weekly. A score describes *this date, this provider endpoint, this model and this harness* — not a fixed thing. The snapshot and per-sidecar `date_utc` exist so a reader can tell what was measured.
- **Harnesses differ.** The same model can produce different artifacts through OpenCode, a raw API, or a CLI agent. Rows are keyed by `(harness, model)` on purpose.
- **free-stealth-bench is one prompt per task.** It measures a specific kind of generative illustration, not general capability.
- **`result_status: no_output` is a real result**, not a harness bug: free endpoints rate-limit, region-block and silently fail. Those events are recorded with the provider's own error message.
- **The judge is OpenCode-only for now.** Contestants can use any harness; the judge still calls the OpenCode CLI. Swapping in `runner.py call` is the next step for fully harness-agnostic judging.
- **Crouton-alpha-free is region-blocked** from the machine this was built on (`provider.auth` / `RegionError`); it appears as an error until run from an allowed region.

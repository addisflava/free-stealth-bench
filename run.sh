#!/usr/bin/env bash
# Benchmark models from any configured provider and agent harness on the SVG
# tasks. Providers and harnesses live in providers.yaml / harnesses.yaml; adding
# one needs no code changes.
#
# Every model gets the SAME prompt, in a FRESH EMPTY directory. No reference
# artwork exists or is passed in. The harness only collects the file the model
# writes and runs deterministic validity checks on it.
#
# Usage:
#   ./run.sh                          # all discovered targets, all tasks in tasks.yaml
#   ./run.sh --tasks sloth-excavator  # one task
#   ./run.sh --models opencode/beam-free,opencode/glm-5.3-flash-free
#   ./run.sh --models codex@openai/gpt-5.2-codex   # alternate agent harness
#   ./run.sh --provider openrouter    # discovery limited to one provider
#   ./run.sh --all-harnesses          # default + alternate harness per provider
#   ./run.sh --effort high            # override the per-provider effort default
#   ./run.sh --timeout 1200           # per-run wall-clock seconds (default: 900)
#   ./run.sh --force                  # re-run even if today's output exists
#   ./run.sh --prompt "Draw a fox" [--task-id fox]   # ad-hoc task from the command line
#   ./run.sh --new-only               # run only (task, model, effort) with no prior record
#   ./run.sh --pairs "opencode/beam-free|sloth-excavator,opencode/mimo-v2.6-flash-free|horse-racing"
#                                     # run exactly these model|task pairs (comma-separated)
#   ./run.sh --dry-run                # show the plan, spend nothing
#   ./run.sh result [port]            # refresh leaderboard data and open/serve the results web page
#   ./run.sh --result [port]          # same as `result`
#   ./run.sh judge [judge.py flags]   # re-run judging/ranking only (no benchmark runs)
#     judge flags: --probe | --limit N | --dry-run | --force | --report | --judge-model M
#
# New outputs are judged automatically after the run and the ranking is refreshed.
# Use --no-judge to benchmark without judging.
#
# Resume semantics: a (task, model, effort) whose newest sidecar says
# result_status=ok is skipped. Failed, invalid, timed-out or missing-output runs
# are re-run on the next invocation. --force re-runs everything. With
# --new-only, any (task, model, effort) that has a sidecar recorded under
# results/ (any date) is skipped — so models newly appeared in the live list get
# benchmarked while recorded runs are not re-done.

set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

# ── Results web page shortcut ──
open_result_page() {
  local PORT="${1:-8000}"
  python3 "$ROOT/leaderboard.py" || exit 1
  echo
  if command -v ss >/dev/null 2>&1 && ss -tlnH 2>/dev/null | grep -qE ":${PORT}(\\s|$)"; then
    echo "port ${PORT} already in use — serving on $((PORT + 1)) instead"
    PORT=$((PORT + 1))
  fi
  python3 "$ROOT/serve.py" "$PORT" &
  local SERVER_PID=$!
  trap 'kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null' INT TERM EXIT
  # Give the server a moment to bind before opening the browser.
  sleep 1
  local URL="http://127.0.0.1:${PORT}/site/"
  if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
    if command -v wslview >/dev/null 2>&1; then
      wslview "$URL" >/dev/null 2>&1 &
    elif command -v cmd.exe >/dev/null 2>&1; then
      cmd.exe /c start "" "$URL" >/dev/null 2>&1 &
    elif command -v explorer.exe >/dev/null 2>&1; then
      explorer.exe "$URL" >/dev/null 2>&1 &
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 &
    fi
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 &
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m webbrowser "$URL" >/dev/null 2>&1 &
  fi
  wait "$SERVER_PID"
}

case "${1:-}" in
  result|results)
    shift
    open_result_page "${1:-8000}"
    ;;
  judge)
    shift
    python3 "$ROOT/judge.py" "$@" || exit 1
    python3 "$ROOT/leaderboard.py" || exit 1
    exit 0
    ;;
esac

EFFORT=""   # empty = use each provider's default (providers.yaml)
TIMEOUT=900
ONLY_MODELS=""
ONLY_TASKS=""
PROMPT_TEXT=""
ADHOC_ID=""
NEW_ONLY=0
PAIRS=""
ONLY_PROVIDER=""
ALL_HARNESSES=0
DRY_RUN=0
FORCE=0
NO_JUDGE=0
ADHOC_PROMPT_FILE=""
ADHOC_PROMPT_MARKER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --models|--targets) ONLY_MODELS="$2"; shift 2 ;;
    --tasks)   ONLY_TASKS="$2"; shift 2 ;;
    --provider) ONLY_PROVIDER="$2"; shift 2 ;;
    --all-harnesses) ALL_HARNESSES=1; shift ;;
    --effort)  EFFORT="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --force)   FORCE=1; shift ;;
    --prompt)  PROMPT_TEXT="$2"; shift 2 ;;
    --task-id) ADHOC_ID="$2"; shift 2 ;;
    --new-only) NEW_ONLY=1; shift ;;
    --pairs)   PAIRS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --result|--results)
      shift
      PORT="${1:-8000}"
      case "$PORT" in --*) PORT=8000 ;; esac
      open_result_page "$PORT"
      ;;
    --judge) NO_JUDGE=0; shift ;;   # kept for compatibility; judging is the default
    --no-judge) NO_JUDGE=1; shift ;;
    -h|--help) sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
if ! python3 -c 'import yaml' 2>/dev/null; then
  echo "PyYAML is required: pip install pyyaml" >&2; exit 1
fi
if ! command -v opencode >/dev/null 2>&1; then
  echo "note: opencode is not in PATH; only non-opencode targets can run" >&2
fi

OC_VERSION="$(opencode --version 2>/dev/null | awk '{print $NF}')"
TODAY="$(date -u +%Y-%m-%d)"
mkdir -p results logs

# ── Model discovery: live list, filtered to free models on the opencode provider.
#    `opencode models` intermittently returns empty output on a cache refresh,
#    so retry, then fall back to the newest non-empty snapshot, then to the
#    2026-11-05 known set. Never overwrite a good snapshot with nothing.
if [ -n "$ONLY_MODELS" ]; then
  IFS=',' read -ra MODELS <<<"$ONLY_MODELS"
else
  MODELS=()
  DISCOVER_ARGS=(discover)
  [ -n "$ONLY_PROVIDER" ] && DISCOVER_ARGS+=(--provider "$ONLY_PROVIDER")
  [ "$ALL_HARNESSES" -eq 1 ] && DISCOVER_ARGS+=(--all-harnesses)
  for attempt in 1 2 3 4 5; do
    mapfile -t MODELS < <(python3 "$ROOT/runner.py" "${DISCOVER_ARGS[@]}" 2>/dev/null | sort -u)
    [ "${#MODELS[@]}" -gt 0 ] && break
    echo "  discovery attempt ${attempt} returned nothing; retrying in 2s..." >&2
    sleep 2
  done
  # Only unfiltered discovery is authoritative for the public "available" list.
  if [ "${#MODELS[@]}" -gt 0 ] && [ -z "$ONLY_PROVIDER" ] && [ "$ALL_HARNESSES" -eq 0 ]; then
    printf '%s\n' "${MODELS[@]}" > "logs/models-snapshot-${TODAY}.txt.tmp"
    mv "logs/models-snapshot-${TODAY}.txt.tmp" "logs/models-snapshot-${TODAY}.txt"
  fi
  if [ "${#MODELS[@]}" -eq 0 ] && [ -z "$ONLY_PROVIDER" ] && [ "$ALL_HARNESSES" -eq 0 ]; then
    fallback="$(ls -t logs/models-snapshot-*.txt 2>/dev/null | while read -r f; do [ -s "$f" ] && { echo "$f"; break; }; done)"
    if [ -n "$fallback" ]; then
      mapfile -t MODELS < "$fallback"
      echo "  WARNING: live discovery failed; using snapshot ${fallback}" >&2
    else
      MODELS=(opencode/beam-free opencode/big-pickle opencode/crouton-alpha-free \
              opencode/deepseek-v4.1-flash-free opencode/flint-40b-free opencode/glm-5.3-flash-free \
              opencode/mimo-v2.6-flash-free opencode/nemotron-3.5-lightning-free \
              opencode/nemotron-3.5-super-vl-free opencode/pulse-alpha-free)
      echo "  WARNING: live discovery failed; using built-in 2026-11-05 free-model list" >&2
    fi
  fi
fi
[ "${#MODELS[@]}" -gt 0 ] || { echo "no models discovered" >&2; exit 1; }

if [ -n "$PROMPT_TEXT" ]; then
  # ── Ad-hoc task: prompt passed on the command line ──
  [ -z "$ONLY_TASKS" ] || { echo "--prompt cannot be combined with --tasks" >&2; exit 2; }
  ADHOC_ID_RAW="${ADHOC_ID:-adhoc}"
  ADHOC_ID_SAFE="$(printf '%s' "$ADHOC_ID_RAW" | tr -c 'a-zA-Z0-9._-' '-' | sed 's/^-*//;s/-*$//')"
  [ -n "$ADHOC_ID_SAFE" ] || { echo "--task-id is empty after sanitization" >&2; exit 2; }
  ADHOC_PROMPT_FILE="$(mktemp /tmp/svgbench-prompt.XXXXXX)"
  printf '%s' "$PROMPT_TEXT" > "$ADHOC_PROMPT_FILE"
  ADHOC_PROMPT_MARKER="<command-line>"
  trap 'rm -f "$ADHOC_PROMPT_FILE"' EXIT
  TASKS=("$ADHOC_ID_SAFE"$'\t'"$ADHOC_PROMPT_FILE")
else
  # ── Task list from tasks.yaml ──
  mapfile -t ALL_TASKS < <(python3 - <<'PY'
import yaml
for t in yaml.safe_load(open('tasks.yaml'))['tasks']:
    print(f"{t['id']}\t{t['prompt']}")
PY
)
  if [ -n "$ONLY_TASKS" ]; then
    IFS=',' read -ra WANT <<<"$ONLY_TASKS"
    TASKS=()
    for t in "${ALL_TASKS[@]}"; do
      case " ${WANT[*]} " in
        *" ${t%%$'\t'*} "*) TASKS+=("$t") ;;
      esac
    done
  else
    TASKS=("${ALL_TASKS[@]}")
  fi
fi
[ "${#TASKS[@]}" -gt 0 ] || { echo "no tasks found" >&2; exit 1; }

# ── Effort policy comes from providers.yaml: effort-capable harnesses get
#    --effort; models listed as unsupported (and harnesses without an effort
#    knob) run at "default".
effort_for() {
  local resolved
  if [ -n "$EFFORT" ]; then
    resolved="$(python3 "$ROOT/runner.py" effort --target "$1" --requested "$EFFORT" 2>/dev/null)"
  else
    resolved="$(python3 "$ROOT/runner.py" effort --target "$1" 2>/dev/null)"
  fi
  printf '%s' "${resolved:-default}"
}

model_slug() { printf '%s' "$1" | tr '/@' '__'; }

existing_record() {  # $1=task $2=slug $3=effort -> 0 if a sidecar for that combo exists (any date)
  [ -n "$(find "results/$1" -maxdepth 1 -name "${2}__${3}__*.json" -print -quit 2>/dev/null)" ]
}

latest_status() {  # $1=task $2=slug $3=effort -> status of the newest sidecar
  local latest
  latest="$(find "results/$1" -maxdepth 1 -name "${2}__${3}__*.json" -print 2>/dev/null | sort | tail -1)"
  [ -n "$latest" ] || { echo "none"; return; }
  python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("result_status") or "unknown")
except Exception:
    print("unknown")' "$latest" 2>/dev/null || echo "unknown"
}

record_ok() {  # $1=task $2=slug $3=effort -> 0 when the newest run is ok and its SVG exists
  local status latest
  status="$(latest_status "$1" "$2" "$3")"
  [ "$status" = "ok" ] || return 1
  latest="$(find "results/$1" -maxdepth 1 -name "${2}__${3}__*.json" -print 2>/dev/null | sort | tail -1)"
  [ -f "${latest%.json}.svg" ]
}

mode=""; [ "$NEW_ONLY" -eq 1 ] && mode=" | new-only"
echo "free-stealth-bench | effort=${EFFORT:-per-provider} | timeout=${TIMEOUT}s | ${#MODELS[@]} target(s) x ${#TASKS[@]} task(s)${mode}"
echo

# ── Build the execution sequence (pairs mode or cartesian product) ──
SEQ=()
if [ -n "$PAIRS" ]; then
  IFS=',' read -ra RAW_PAIRS <<<"$PAIRS"
  for p in "${RAW_PAIRS[@]}"; do
    p="$(printf '%s' "$p" | tr -d '[:space:]')"
    [ -z "$p" ] && continue
    if [[ "$p" != *\|* ]]; then continue; fi
    SEQ+=("${p##*|}|${p%%|*}")
  done
else
  for entry in "${TASKS[@]}"; do
    task="${entry%%$'\t'*}"
    for model in "${MODELS[@]}"; do SEQ+=("${task}|${model}"); done
  done
fi

# ── Dry run: print the matrix, exit ──
if [ "$DRY_RUN" -eq 1 ]; then
  for item in "${SEQ[@]}"; do
    task="${item%%|*}"; model="${item##*|}"
    eff="$(effort_for "$model")"
    suffix=""; [ "$eff" != "default" ] && suffix="#$eff"
    out="results/${task}/$(model_slug "$model")__${eff}__${TODAY}.svg"
    skip=0
    if [ "$FORCE" -eq 0 ]; then
      if [ "$NEW_ONLY" -eq 1 ]; then
        existing_record "$task" "$(model_slug "$model")" "$eff" && skip=1
      elif record_ok "$task" "$(model_slug "$model")" "$eff"; then
        skip=1
      fi
    fi
    if [ "$skip" -eq 1 ]; then
      printf 'SKIP  %-18s %-40s -> %s\n' "$task" "${model}${suffix}" "$out"
    else
      printf 'RUN   %-18s %-40s -> %s\n' "$task" "${model}${suffix}" "$out"
    fi
  done
  echo
  echo "prompt SHA-256:"
  for entry in "${TASKS[@]}"; do
    task="${entry%%$'\t'*}"; prompt="${entry##*$'\t'}"
    printf '  %s  %s  %s\n' "$(sha256sum "$prompt" | cut -c1-12)" "$task" "$(sha256sum "$prompt" | cut -d' ' -f1)"
  done
  echo "(dry-run: automated judging skipped — nothing was scored)"
  exit 0
fi

run_one() {  # $1=task $2=prompt_file $3=model
  local task="$1" prompt_file="$2" model="$3"
  local eff slug out log variant_suffix
  eff="$(effort_for "$model")"
  variant_suffix=""; [ "$eff" != "default" ] && variant_suffix="#$eff"
  slug="$(model_slug "$model")"
  mkdir -p "results/${task}"
  out="results/${task}/${slug}__${eff}__${TODAY}.svg"
  log="logs/${task}__${slug}__${eff}__${TODAY}.ndjson"

  if [ "$FORCE" -eq 0 ]; then
    if [ "$NEW_ONLY" -eq 1 ]; then
      if existing_record "$task" "$slug" "$eff"; then
        echo "SKIP  ${task}  ${model} (new-only: already recorded)"
        return 0
      fi
    elif record_ok "$task" "$slug" "$eff"; then
      echo "SKIP  ${task}  ${model} (${eff}; latest run ok; --force to redo)"
      return 0
    fi
  fi

  local prompt_sha start end wall_seconds exit_code timed_out runner_code
  prompt_sha="$(sha256sum "$prompt_file" | cut -d' ' -f1)"

  attempt() {  # $1=target $2=logfile $3=effort ; sets globals
    local target="$1" logfile="$2" effort_used="$3"
    local meta_path="$ROOT/$logfile.meta.json"
    start="$(date +%s)"
    python3 "$ROOT/runner.py" run --target "$target" --task "$task" \
        --prompt-file "$prompt_file" --effort "$effort_used" --timeout "$TIMEOUT" \
        --out "$ROOT/$out" --log "$ROOT/$logfile" --meta "$meta_path" >/dev/null 2>&1
    runner_code=$?
    end="$(date +%s)"
    wall_seconds=$(( end - start ))
    timed_out=0; [ "$runner_code" -eq 124 ] && timed_out=1
    method="missing"
    if [ -f "$meta_path" ]; then
      read -r method timed_out wall_seconds exit_code < <(python3 - "$meta_path" <<'PY'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    m = {}
print(m.get('collection_method') or 'missing',
      1 if m.get('timed_out') else 0,
      int(m.get('wall_seconds') or 0),
      int(m.get('exit_code') or 0))
PY
)
    fi
  }

  printf 'RUN   %-15s %-40s effort=%-8s timeout=%ss\n' "$task" "$model" "$eff" "$TIMEOUT"
  printf '      log: %s\n' "$log"

  # Heartbeat so a slow model does not look like a hung process.
  local hb_pid hb_start
  hb_start="$(date +%s)"
  (
    while sleep 60; do
      printf '  ..  %-15s %-40s still running (%ss)\n' \
        "$task" "$model" "$(( $(date +%s) - hb_start ))"
    done
  ) &
  hb_pid=$!

  method=""
  runner_code=0
  attempt "$model" "$log" "$eff"

  # ── Effort fallback: provider rejected the variant -> one retry with default ──
  local effort_fallback=0
  if [ "$method" = "missing" ] && [ "$eff" != "default" ] && { [ "$runner_code" -eq 3 ] || grep -qiE 'variant|effort|reasoning.level|unknown option|unsupported' "$ROOT/$log" 2>/dev/null; }; then
    echo "  .. variant '${eff}' rejected by provider; retrying once with default"
    eff="default"; variant_suffix=""
    effort_fallback=1
    out="results/${task}/${slug}__default__${TODAY}.svg"
    log="logs/${task}__${slug}__default__${TODAY}.ndjson"
    attempt "$model" "$log" "$eff"
  fi

  kill "$hb_pid" 2>/dev/null
  wait "$hb_pid" 2>/dev/null

  # ── Validate (best effort) ──
  local validation_file="-"
  if [ "$method" != "missing" ]; then
    validation_file="$(mktemp /tmp/svgbench-validation.XXXXXX)"
    python3 validate_output.py "$ROOT/$out" >"$validation_file" 2>/dev/null || validation_file="-"
  fi

  # ── History marker: for --prompt runs, don't leak the temp file path ──
  local prompt_file_env="$prompt_file"
  [ -n "$ADHOC_PROMPT_MARKER" ] && prompt_file_env="$ADHOC_PROMPT_MARKER"

  # ── Sidecar metadata ──
  DATE_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  TASK="$task" MODEL="$model" VARIANT="${variant_suffix#\#}" EFFORT="$eff" \
  EFFORT_REQUESTED="$EFFORT" EFFORT_FALLBACK="$effort_fallback" \
  OPENCODE_VERSION="$OC_VERSION" PROMPT_FILE="$prompt_file_env" PROMPT_SHA256="$prompt_sha" \
  WALL_SECONDS="$wall_seconds" EXIT_CODE="$exit_code" TIMED_OUT="$timed_out" METHOD="$method" \
  RUN_META="$ROOT/$log.meta.json" \
    python3 record_run.py "$ROOT/$out" "$ROOT/$log" "$validation_file"
  [ "$validation_file" = "-" ] || rm -f "$validation_file"

  local sidecar="${out%.svg}.json" status
  status="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["result_status"])' "$sidecar" 2>/dev/null || echo unknown)"
  printf 'DONE  %-15s %-40s effort=%-8s status=%-12s %ss\n' "$task" "$model" "$eff" "$status" "$wall_seconds"
  # keep the leaderboard page current: regenerate its data after every run
  ( cd "$ROOT" && python3 leaderboard.py >/dev/null 2>&1 ) || echo "  .. leaderboard refresh failed (run: python3 leaderboard.py)"
}

for item in "${SEQ[@]}"; do
  task="${item%%|*}"; model="${item##*|}"
  prompt_file=""
  for entry in "${TASKS[@]}"; do
    if [ "${entry%%$'\t'*}" = "$task" ]; then prompt_file="${entry##*$'\t'}"; break; fi
  done
  if [ -z "$prompt_file" ]; then
    echo "  pairs: skipping unknown task '${task}' in '${item}'" >&2
    continue
  fi
  run_one "$task" "$prompt_file" "$model"
done

# ── Aggregate a machine-readable summary of all sidecars ──
python3 - "$ROOT" "$TODAY" <<'PY'
import glob, json, sys, os
root, today = sys.argv[1], sys.argv[2]
rows = []
for p in sorted(glob.glob(os.path.join(root, 'results', '*', '*__*.json'))):
    base = os.path.basename(p)
    if base.startswith('summary-'):
        continue
    try:
        r = json.load(open(p))
    except json.JSONDecodeError:
        continue
    v = r.get('validation') or {}
    rows.append({
        'task': r.get('task'),
        'model': r.get('model'),
        'provider': r.get('provider'),
        'harness': r.get('harness'),
        'effort': r.get('effort'),
        'effort_fallback': r.get('effort_fallback'),
        'date_utc': r.get('date_utc'),
        'result_status': r.get('result_status'),
        'collection_method': r.get('collection_method'),
        'wall_seconds': r.get('wall_seconds'),
        'tokens': r.get('tokens'),
        'size_bytes': v.get('size_bytes'),
        'elements_total': v.get('elements_total'),
        'animation_elements': v.get('animation_elements'),
        'well_formed_xml': v.get('well_formed_xml'),
        'sha256': v.get('sha256'),
        'sidecar': os.path.relpath(p, root),
    })
out = os.path.join(root, 'results', f'summary-{today}.json')
with open(out, 'w', encoding='utf-8') as fh:
    json.dump({'benchmark': 'free-stealth-bench', 'date_utc': today, 'runs': rows}, fh, indent=2)
print(f"\nsummary written: {os.path.relpath(out, root)}")
ok = sum(1 for r in rows if r['result_status'] == 'ok')
print(f"runs recorded: {len(rows)} | ok: {ok} | other: {len(rows) - ok}")
PY

# ── Automated judging (free models only, no paid endpoints) ──
# New outputs are scored and the ranking is refreshed after every run. Judge
# failures are reported but never abort a benchmark — the next run retries.
if [ "$NO_JUDGE" -eq 0 ]; then
  echo
  echo "== automated judging =="
  python3 "$ROOT/judge.py" || echo "  .. judge failed (ranking not updated); run: ./run.sh judge"
  python3 "$ROOT/leaderboard.py" || echo "  .. leaderboard refresh failed (run: python3 leaderboard.py)"

  # ── Publish to the live webpage ──
  # Discovery found the current model list and judging just finished, so commit
  # and push the results to refresh the GitHub Pages leaderboard. Idempotent:
  # skips when there is nothing new. Set NO_PUBLISH=1 to benchmark without
  # touching the remote.
  echo
  echo "== publishing to GitHub Pages =="
  bash "$ROOT/publish.sh" || echo "  .. publish failed (re-run with: ./publish.sh)"
fi

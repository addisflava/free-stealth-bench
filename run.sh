#!/usr/bin/env bash
# Benchmark all currently-served opencode free models on the SVG tasks.
#
# Every model gets the SAME prompt, in a FRESH EMPTY directory. No reference
# artwork exists or is passed in. The harness only collects the file the model
# writes and runs deterministic validity checks on it.
#
# Usage:
#   ./run.sh                          # all discovered free models, all tasks in tasks.yaml
#   ./run.sh --tasks sloth-excavator  # one task
#   ./run.sh --models opencode/beam-free,opencode/glm-5.3-flash-free
#   ./run.sh --effort high            # effort for effort-capable models (default: high)
#   ./run.sh --timeout 1200           # per-run wall-clock seconds (default: 900)
#   ./run.sh --force                  # re-run even if today's output exists
#   ./run.sh --prompt "Draw a fox" [--task-id fox]   # ad-hoc task from the command line
#   ./run.sh --new-only               # run only (task, model, effort) with no prior record
#   ./run.sh --pairs "opencode/beam-free|sloth-excavator,opencode/mimo-v2.6-flash-free|horse-racing"
#                                     # run exactly these model|task pairs (comma-separated)
#   ./run.sh --dry-run                # show the plan, spend nothing
#   ./run.sh result [port]            # refresh leaderboard data and open/serve the results web page
#   ./run.sh --result [port]          # same as `result`
#   ./run.sh judge [judge.py flags]   # automated blind LLM judging + ranking (no benchmark runs)
#   ./run.sh --judge                  # run the benchmark, then judge the results
#     judge flags: --probe | --limit N | --dry-run | --force | --report | --judge-model M
#
# Resume semantics: a result file for (task, model, effort) is skipped unless
# --force is given. With --new-only, any (task, model, effort) that has a sidecar
# recorded under results/ (any date) is skipped — so models newly appeared in the
# live free list get benchmarked while already-recorded runs are not re-done.
#
# Resume semantics: a same-day result file for (task, model, effort) is skipped
# unless --force is given.

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

EFFORT="high"
TIMEOUT=900
ONLY_MODELS=""
ONLY_TASKS=""
PROMPT_TEXT=""
ADHOC_ID=""
NEW_ONLY=0
PAIRS=""
DRY_RUN=0
FORCE=0
DO_JUDGE=0
ADHOC_PROMPT_FILE=""
ADHOC_PROMPT_MARKER=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --models)  ONLY_MODELS="$2"; shift 2 ;;
    --tasks)   ONLY_TASKS="$2"; shift 2 ;;
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
    --judge) DO_JUDGE=1; shift ;;
    -h|--help) sed -n '2,/^set -uo pipefail/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v opencode >/dev/null || { echo "opencode not found in PATH" >&2; exit 1; }
command -v python3  >/dev/null || { echo "python3 not found" >&2; exit 1; }

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
  for attempt in 1 2 3 4 5; do
    mapfile -t MODELS < <(opencode models 2>/dev/null | grep -E '^opencode/[a-z0-9._-]*(-free|_free)$|^opencode/big-pickle$' | sort -u)
    [ "${#MODELS[@]}" -gt 0 ] && break
    echo "  discovery attempt ${attempt} returned nothing; retrying in 2s..." >&2
    sleep 2
  done
  if [ "${#MODELS[@]}" -gt 0 ]; then
    printf '%s\n' "${MODELS[@]}" > "logs/models-snapshot-${TODAY}.txt.tmp"
    mv "logs/models-snapshot-${TODAY}.txt.tmp" "logs/models-snapshot-${TODAY}.txt"
  else
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

# ── Effort policy: one effort for effort-capable models; "default" (no variant)
#    for models that have no effort knob and for any model whose effort support
#    is unverified. Known effort-less models: big-pickle, mimo-v2.6-flash-free,
#    nemotron-3.5-lightning-free. Known to accept #high: beam, flint-40b,
#    glm-5.3-flash, deepseek-v4.1-flash, nemotron-3.5-super-vl, pulse-alpha.
#    Unknown to this machine: crouton-alpha-free (region-blocked here anyway).
effort_for() {
  case "$1" in
    opencode/big-pickle|opencode/mimo-v2.6-flash-free|opencode/nemotron-3.5-lightning-free)
      echo "default" ;;
    *)
      echo "$EFFORT" ;;
  esac
}

model_slug() { printf '%s' "$1" | tr '/' '_'; }

existing_record() {  # $1=task $2=slug $3=effort -> 0 if a sidecar for that combo exists (any date)
  [ -n "$(find "results/$1" -maxdepth 1 -name "${2}__${3}__*.json" -print -quit 2>/dev/null)" ]
}

mode=""; [ "$NEW_ONLY" -eq 1 ] && mode=" | new-only"
echo "opencode ${OC_VERSION} | effort=${EFFORT} | timeout=${TIMEOUT}s | ${#MODELS[@]} model(s) x ${#TASKS[@]} task(s)${mode}"
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
      elif ls results/"${task}"/"$(model_slug "$model")"__"${eff}"__*.svg >/dev/null 2>&1; then
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
  [ "$DO_JUDGE" -eq 1 ] && echo "(dry-run: automated judging skipped — nothing was scored)"
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
    elif ls results/"${task}"/"${slug}"__"${eff}"__*.svg >/dev/null 2>&1; then
      echo "SKIP  ${task}  ${model} (${eff}; existing output; --force to redo)"
      return 0
    fi
  fi

  local prompt_sha prompt start end wall_seconds exit_code timed_out workdir
  prompt_sha="$(sha256sum "$prompt_file" | cut -d' ' -f1)"
  prompt="$(cat "$prompt_file")"

  attempt() {  # $1=model-with-optional-variant $2=logfile ; sets globals
    local wd
    wd="$(mktemp -d /tmp/svgbench.XXXXXX)"
    start="$(date +%s)"
    ( cd "$wd" && timeout --signal=TERM "$TIMEOUT" opencode run --format json --auto \
        -m "$1" "$prompt" >"$ROOT/$2" 2>&1 )
    exit_code=$?
    end="$(date +%s)"
    wall_seconds=$(( end - start ))
    timed_out=0; [ "$exit_code" -eq 124 ] && timed_out=1
    # collect artifact
    if [ -s "$wd/output.svg" ]; then
      cp "$wd/output.svg" "$ROOT/$out"; method="output.svg"
    elif python3 "$ROOT/extract_svg.py" "$ROOT/$2" "$ROOT/$out"; then
      method="extracted"
    else
      method="missing"
    fi
    rm -rf "$wd"
  }

  method=""
  attempt "${model}${variant_suffix}" "$log"

  # ── Effort fallback: provider rejected the variant -> one retry with default ──
  local effort_fallback=0
  if [ "$method" = "missing" ] && [ "$eff" != "default" ] && grep -qiE 'variant|effort|reasoning.level|unknown option' "$ROOT/$log"; then
    echo "  .. variant '${eff}' rejected by provider; retrying once with default"
    eff="default"; variant_suffix=""
    effort_fallback=1
    out="results/${task}/${slug}__default__${TODAY}.svg"
    log="logs/${task}__${slug}__default__${TODAY}.ndjson"
    attempt "$model" "$log"
  fi

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
    json.dump({'benchmark': 'svg-bench', 'date_utc': today, 'runs': rows}, fh, indent=2)
print(f"\nsummary written: {os.path.relpath(out, root)}")
ok = sum(1 for r in rows if r['result_status'] == 'ok')
print(f"runs recorded: {len(rows)} | ok: {ok} | other: {len(rows) - ok}")
PY

# ── Optional automated judging (free models only, no paid endpoints) ──
if [ "$DO_JUDGE" -eq 1 ]; then
  echo
  echo "== automated judging =="
  python3 "$ROOT/judge.py" || exit 1
  python3 "$ROOT/leaderboard.py" || exit 1
fi

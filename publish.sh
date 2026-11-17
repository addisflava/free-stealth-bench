#!/usr/bin/env bash
# Publish benchmark results to the live GitHub Pages leaderboard.
#
# Call this after discovery has found new models and the judge has finished:
# it rebuilds the deployable site from judgments.json + results, then commits
# and pushes to the remote so the Pages workflow redeploys the live webpage.
#
# Guards:
#   - no remote configured              -> skip push with a clear message
#   - nothing staged after rebuild      -> skip commit (idempotent)
#   - NO_PUBLISH=1                      -> do nothing (explicit opt-out)
set -euo pipefail
cd "$(dirname "$0")"

if [ "${NO_PUBLISH:-0}" = 1 ]; then
  echo "publish: NO_PUBLISH=1 set, skipping"
  exit 0
fi

# Refresh leaderboard data (site/data.json) and the deployable _site tree from
# the current judgments.json + results. This also gives us a fresh set of
# changes to detect below.
python3 build_site.py --out _site

# Everything the live page and its build depend on is tracked in this repo.
git add -A

if git diff --cached --quiet; then
  echo "publish: no new judged results to publish"
  exit 0
fi

# Summarize which new models this publish adds, using the newest discovery
# snapshot that has a matching judged result.
snapshot="$(ls -1 logs/models-snapshot-*.txt 2>/dev/null | sort | tail -1 || true)"
new_models=""
if [ -n "$snapshot" ]; then
  new_models="$(python3 - "$snapshot" <<'PY'
import json, sys
snapshot = sys.argv[1]
wanted = []
for line in open(snapshot, encoding='utf-8'):
    name = line.strip()
    if not name or name.startswith('#'):
        continue
    bare = name.split('/')[-1]
    wanted.append(bare)
try:
    data = json.load(open('judgments.json', encoding='utf-8'))
except Exception:
    data = {}
judged = set()
for key in (data.get('judgments') or {}):
    for tok in str(key).replace('__', '_').split('_'):
        judged.add(tok)
found = sorted({w for w in wanted if w in judged})
print(', '.join(found))
PY
)"
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "publish: no 'origin' remote configured; changes staged but not pushed"
  exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$new_models" != "" ]; then
  msg="Publish judged results for: $new_models"
else
  msg="Publish updated benchmark results and leaderboard"
fi

git commit -m "$msg"
git push origin "HEAD:refs/heads/${branch}"
echo "publish: committed and pushed '$msg' -> origin/$branch (Pages will redeploy)"

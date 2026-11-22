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

# Summarize which new models this publish adds. "New" = present in the newest
# discovery snapshot but absent from the previous one, and actually ranked by
# the judge (names come from the judge report, not the hash-keyed judgments).
snapshots=( $(ls -1 logs/models-snapshot-*.txt 2>/dev/null | sort) )
new_models=""
if [ "${#snapshots[@]}" -ge 1 ]; then
  new_models="$(python3 - "${snapshots[@]}" <<'PY'
import re, sys
snaps = sys.argv[1:]
def models(path):
    out = set()
    for line in open(path, encoding='utf-8'):
        name = line.strip()
        if name and not name.startswith('#'):
            out.add(name.split('/')[-1])
    return out
current = models(snaps[-1])
previous = models(snaps[-2]) if len(snaps) >= 2 else set()
new = current - previous
try:
    report = open('results/judge-report.md', encoding='utf-8').read()
except FileNotFoundError:
    report = ''
ranked = set(re.findall(r'\b[\w.\-]+-free\b', report))
found = sorted(m for m in new if m in ranked)
print(', '.join(found))
PY
)"
fi

# Announce newly-ranked models on the OpenCode community. Always writes a
# ready-to-paste draft into posts/; it only submits to Reddit when OAuth
# credentials are configured. Best-effort: never block publishing.
# Set NO_REDDIT=1 to skip, REDDIT_DRY_RUN=1 for drafts without posting.
if [ "$new_models" != "" ]; then
  python3 "$ROOT/reddit_post.py" --models "$new_models" \
    || echo "  .. reddit announcement failed (re-run: python3 reddit_post.py --models '$new_models')"
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

#!/usr/bin/env bash
# Push whatever this worker has scraped so far.
#
# Carried over from the sibling cgtrader-scraper, where this is what made 8+
# parallel workers on one slice safe. The key property: when two workers touch
# the same shard file, the conflict is resolved by UNION-merging both sides,
# never by picking one. Picking a side silently drops rows.
set -uo pipefail

BRANCH="${BRANCH:-main}"
MSG="${1:-progress}"

resolve_conflicts() {
  local conflicted
  conflicted=$(git diff --name-only --diff-filter=U)
  [ -z "$conflicted" ] && return 0
  echo "resolving $(echo "$conflicted" | wc -l) conflicted file(s) by union merge"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      *.jsonl)
        # Both sides are append-only JSONL: keep every line from both, then
        # drop exact duplicates. Order does not matter, readers dedupe by id.
        git show :2:"$f" > /tmp/ours.$$  2>/dev/null || : > /tmp/ours.$$
        git show :3:"$f" > /tmp/theirs.$$ 2>/dev/null || : > /tmp/theirs.$$
        cat /tmp/ours.$$ /tmp/theirs.$$ | awk '!seen[$0]++' > "$f"
        rm -f /tmp/ours.$$ /tmp/theirs.$$
        git add "$f"
        ;;
      *)
        git checkout --theirs -- "$f" 2>/dev/null || git checkout --ours -- "$f"
        git add "$f"
        ;;
    esac
  done <<< "$conflicted"
}

git config user.name  "cults3d-scraper"
git config user.email "scraper@users.noreply.github.com"

git add -A details url_index 2>/dev/null || true
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -q -m "$MSG" || true

for attempt in 1 2 3 4 5 6 7 8; do
  if git push -q origin "HEAD:$BRANCH" 2>/dev/null; then
    echo "pushed on attempt $attempt"
    exit 0
  fi
  echo "push rejected, rebasing (attempt $attempt)"
  if ! git pull --rebase -q origin "$BRANCH"; then
    resolve_conflicts
    GIT_EDITOR=true git rebase --continue || git rebase --skip || true
  fi
  sleep $(( RANDOM % 10 + attempt * 3 ))
done

echo "could not push after 8 attempts" >&2
exit 1

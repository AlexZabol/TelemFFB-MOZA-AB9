#!/usr/bin/env bash
# Merge an upstream ref into the current branch. A README.md conflict is
# resolved automatically (fork section from our side + upstream's README);
# any other conflict fails the run as before.
# usage: moza/merge_upstream.sh <ref-or-sha>
set -euo pipefail

REF="${1:?usage: $0 <ref-or-sha>}"
START='<!-- moza-fork:start -->'
END='<!-- moza-fork:end -->'

if git merge --no-edit "$REF"; then
  exit 0
fi

# only README.md may be conflicted for the auto-resolution to apply
CONFLICTS=$(git diff --name-only --diff-filter=U)
if [ "$CONFLICTS" != "README.md" ]; then
  echo "::error::merge of $REF failed, conflicting files:" >&2
  echo "$CONFLICTS" >&2
  exit 1
fi

# fork section (markers included) from our side
BLOCK=$(git show HEAD:README.md | awk -v s="$START" -v e="$END" \
  '$0==s{p=1} p{print} $0==e{p=0}')
if ! grep -qxF "$START" <<<"$BLOCK" || ! grep -qxF "$END" <<<"$BLOCK"; then
  echo "::error::README.md conflict and no moza-fork marker block on our side" >&2
  exit 1
fi

# upstream README, minus any marker block it might contain
UPSTREAM=$(git show "$REF":README.md | awk -v s="$START" -v e="$END" \
  '$0==s{skip=1} !skip{print} $0==e{skip=0}')

printf '%s\n\n%s\n' "$BLOCK" "$UPSTREAM" > README.md
git add README.md
git commit --no-edit
echo "::notice::README.md conflict auto-resolved (fork section + upstream README)"

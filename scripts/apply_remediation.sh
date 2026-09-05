#!/usr/bin/env bash
# Commit the staged changes as the application of a PRISM remediation.
# usage: scripts/apply_remediation.sh <remediation-id> <timestamp>
# The message is "apply PRISM remediation <id> (<time>): <first line of recordings/explain-v1-01/prism/remediation.md>";
# GET /explain/prove finds it again by that prefix. PRISM recommends. I apply.
set -euo pipefail
id="${1:?usage: apply_remediation.sh <remediation-id> <timestamp>}"
ts="${2:?usage: apply_remediation.sh <remediation-id> <timestamp>}"
root="$(git rev-parse --show-toplevel)"
file="$root/recordings/explain-v1-01/prism/remediation.md"
[ -f "$file" ] || { echo "missing $file" >&2; exit 1; }
first="$(grep -m1 -v '^[[:space:]]*$' "$file" | sed -E 's/^#+[[:space:]]*//; s/^-[[:space:]]*//')"
[ -n "$first" ] || { echo "$file has no text" >&2; exit 1; }
git -C "$root" diff --cached --quiet && { echo "nothing staged; git add the change first" >&2; exit 1; }
git -C "$root" commit -m "apply PRISM remediation $id ($ts): $first"

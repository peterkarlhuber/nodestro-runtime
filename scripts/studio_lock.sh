#!/usr/bin/env bash
# Writes the studio lock (nodestro-studio platform/internal-server.txt, the exact commits of the internal packages)
# to the file given as the only argument. CI calls it before every install with the lock.
#
# A pull request reads the lock of the studio branch with its own head branch's name when there is one (a change that
# needs newer components and the studio bump that moves them land as a pair), else the studio master's. A push of a
# merge commit to master ("Merge pull request #N from owner/<branch>") reads the lock of the studio branch <branch>
# while that branch is still ahead of the studio master, that is, while its paired bump is not merged yet (lock lag);
# once the bump is merged (or the branch is gone) it reads the studio master's lock as before. The compare request is
# authenticated with GH_TOKEN when the step sets it (the workflow token; read access to public repositories is enough),
# because unauthenticated requests share a small rate limit per runner address. When GitHub cannot be asked (an error
# status, a rate limit, no network) the run reads the studio master's lock and says so in a warning with the cause, so a
# failure from an outdated lock is never mistaken for a regression.
# The nightly and dispatched runs read the studio master's lock only. A paired lock is made visible: a warning names the
# studio branch, and the job summary shows its diff against the studio master's lock.
set -euo pipefail

# Windows runners have "python" but not always "python3".
python=$(command -v python3 || command -v python)

out=${1:?usage: studio_lock.sh <output file>}
base=https://raw.githubusercontent.com/peterkarlhuber/nodestro-studio
path=platform/internal-server.txt
branch=${GITHUB_HEAD_REF:-}

if [ -z "$branch" ] && [ "${GITHUB_EVENT_NAME:-}" = push ] && [ "${GITHUB_REF:-}" = refs/heads/master ] \
  && [ -f "${GITHUB_EVENT_PATH:-}" ]; then
  merged=$("$python" -I -c 'import json, os, re, sys
message = (json.load(open(os.environ["GITHUB_EVENT_PATH"])).get("head_commit") or {}).get("message") or ""
match = re.match(r"Merge pull request #[0-9]+ from [^/\s]+/(\S+)", message)
print(match[1] if match else "")')
  if [ -n "$merged" ]; then
    # Ahead of the studio master: the paired bump is still open. 404: the branch is gone. Anything else is an API error.
    auth=()  # bash 3.2 (macOS) treats an empty array as unset under set -u, hence the ${auth[@]+...} form below
    [ -z "${GH_TOKEN:-}" ] || auth=(-H "Authorization: Bearer $GH_TOKEN")
    status=$(curl -sS -o "$out.compare" -w '%{http_code}' ${auth[@]+"${auth[@]}"} -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/peterkarlhuber/nodestro-studio/compare/master...$merged" 2>"$out.curl") || status=000
    ahead=0
    case "$status" in
      200) ahead=$("$python" -I -c 'import json, sys; print(int(json.load(open(sys.argv[1])).get("ahead_by", 0)))' \
             "$out.compare" 2>/dev/null) \
           || { ahead=0; echo "::warning title=Lock lag unknown::The compare answer for '$merged' could not be read; using the studio master's lock."; } ;;
      404) echo "The studio branch '$merged' is gone; using the studio master's lock." ;;
      *) echo "::warning title=Lock lag unknown::GitHub answered HTTP $status to the compare request for '$merged'$([ -n "${GH_TOKEN:-}" ] || echo ' (no GH_TOKEN)'): $(cat "$out.curl" "$out.compare" 2>/dev/null | head -c 300 | tr '\n' ' '). This run uses the studio master's lock; a failure from an outdated lock is lock lag, not a regression." ;;
    esac
    rm -f "$out.compare" "$out.curl"
    if [ "${ahead:-0}" -gt 0 ]; then
      echo "Lock lag: the studio bump paired with '$merged' is not merged yet ($ahead commit(s) ahead of master)."
      branch=$merged
    fi
  fi
fi

if [ -n "$branch" ] && curl -fsSL "$base/$branch/$path" -o "$out.paired"; then
  curl -fsSL "$base/master/$path" -o "$out.master"
  echo "::warning title=Paired studio lock::This run installs with the lock of the studio branch '$branch', not the studio master's; see the job summary for the diff."
  {
    echo "### Paired studio lock"
    echo
    echo "This run installs with the lock of the studio branch \`$branch\`, not the studio master's."
    echo
    if diff -u --label "master/$path" --label "$branch/$path" "$out.master" "$out.paired" > "$out.diff"; then
      echo "The branch lock is identical to the studio master's lock."
    else
      echo "Diff of the branch lock against the studio master's lock (\`$path\`):"
      echo
      echo '```diff'
      cat "$out.diff"
      echo '```'
    fi
  } >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
  mv "$out.paired" "$out"
  rm -f "$out.master" "$out.diff"
else
  [ -z "$branch" ] || echo "No studio branch '$branch' with a lock; using the studio master's lock."
  curl -fsSL "$base/master/$path" -o "$out"
fi

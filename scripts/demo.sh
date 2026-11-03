#!/usr/bin/env bash
# Repeatable demo with scripted model replies: no model or API key needed.
# Shows the edit-test-fix loop, cancellation and a paused and resumed session.
set -uo pipefail
cd "$(dirname "$0")/.."
H="python3 -m harness --sessions-dir sessions-demo"
rm -rf sessions-demo 2>/dev/null || { chmod -R u+w sessions-demo; rm -rf sessions-demo; }

echo; echo "=== 1. Allowed projects"; $H projects
echo; echo "=== 2. Edit-test-fix loop: the first attempt fails a check; the failure guides the fix"
$H start --script scripts/feature-script.json --label demo-loop
echo; echo "=== 3. Saved session, reopened by a new process"; $H status demo-loop | tail -25

echo; echo "=== 4. A stuck run is cancelled from another terminal"
$H start --script scripts/feature-script.json --label demo-resume --no-run >/dev/null
python3 scripts/stuck_run.py sessions-demo demo-resume &
RUN=$!
sleep 4; $H cancel demo-resume; wait $RUN
echo "--- leftover containers: [$(docker ps -q --filter label=harness.run=demo-resume)]"

echo; echo "=== 5. Someone changes the working copy: resume pauses"
echo "# edited by hand" >> sessions-demo/demo-resume/workspace/src/allocation/domain/model.py
$H resume demo-resume
echo; echo "=== 6. The user decides: restore the last checkpoint and continue"
$H resume demo-resume --restore

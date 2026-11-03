# Demo video script (at most 4 minutes)

Timings add up to about 3:50. Everything shown is real. The API run takes about a
minute, so it fits live, though you can shorten the wait when editing.

## Before recording (not on camera)

```bash
cd ~/Desktop/facultad/UASTW/ai-dev/"stage 2"
python3 -m harness prepare          # target and sandbox image ready
python3 -m harness ping             # VPN on, key in .env, model answers
chmod -R u+w sessions-demo 2>/dev/null; rm -rf sessions-demo
python3 -m harness delete video 2>/dev/null
clear
```

Use a maximized terminal with a large font. Open `README.md` in preview
(`Ctrl+Shift+V` in VS Code) for the diagrams. Do not show `.env` on screen.

## 1. 0:00–0:25 What it is

**Show:** README, the architecture diagram.

> "This is Stage 2 of my coding harness, in Python with no agent framework. It extends
> Stage 1 with repository navigation, a bounded context, an edit-test-fix loop and
> saved sessions. The interface is separate from the core: this service layer is used
> by the command line, the shell and the tests."

## 2. 0:25–0:50 Project, feature and failing start

```bash
python3 -m harness
```
Then in the shell: `/projects`. Show `task/feature-expiry/request.md` in the editor.

> "I can only select projects on this allow-list. The feature is an optional expiry
> date for stock batches. It crosses the command, the domain model, the handler and the
> database mapping, keeps existing callers working, and rejects invalid values. The
> request names no file. At the starting commit, the protected feature check fails and
> the 26 regression tests pass. The checks are outside the agent's writable area."

## 3. 0:50–2:00 Real run

In the shell, type `/new`. Let it run; cut the wait if needed.

**Point at:** the selected files, the spinner's action and context counters, a
`Context compacted` line, an edit preview, and a failed `Check(feature)` followed by a
fix.

> "The harness picked these files itself, and builds a map from the Python syntax tree.
> Each prompt stays under 36,000 characters. When it compacts, the task contract stays
> word for word, and repository text stays marked as untrusted. Here a check failed,
> and the model used that failure to fix its code. This model runs through a course
> API, because my local model was too slow on CPU."

## 4. 2:00–2:25 Review

**Show:** the result panel and the diff.

> "The model's claim is labelled unverified. The verdict comes only from checks the
> harness runs in a Docker sandbox with no network, including held-out tests the agent
> never saw. I gave no manual help in this run."

## 5. 2:25–3:30 Cancel and safe resume

Use a second terminal. In the first:
```bash
python3 -m harness start --label video
```
After the first `Update` appears, in the second:
```bash
python3 -m harness cancel video
docker ps --filter label=harness.run=video
echo "# edited by hand" >> sessions/video/workspace/README.md
python3 -m harness resume video
```
**Point at:** the paused panel, then run:
```bash
python3 -m harness resume video --restore
```

> "Cancelling closes the model connection or kills the command, and no containers stay
> behind. The session is saved. Now I change a file by hand. Resume compares the working
> copy with the checkpoint, including the harness's own edits, and pauses without running
> anything. I decide: restore. It continues with the limits it had already consumed. An
> action interrupted by a crash is marked uncertain and is never replayed."

## 6. 3:30–3:50 Tests and limits

```bash
python3 -m unittest discover -s tests -t . 2>&1 | tail -3
```

> "All Stage 1 and Stage 2 tests pass without a model or API key. Known limits are in the
> README: navigation is Python only, the local model never finished this feature, and
> a harness-driven fix round has been verified with scripted replies, since the real
> model ran the checks itself."

## Manual help to state

None during the verified feature run. The hand edit in segment 5 is a deliberate test.

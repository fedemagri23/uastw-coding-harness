# Demo video script (about 2 minutes)

The video has six segments. Segment 3 (the real model run) is recorded live
but takes about 3–4 minutes on CPU, so you cut the waiting in editing. Every
other segment is real time.

## Before recording (not on camera)

Run these once, in a terminal:

```bash
cd ~/Desktop/facultad/UASTW/ai-dev/stage1

# 1. Ollama must be running and the model present
ollama list                       # must show qwen2.5-coder:7b
# if Ollama is not running:  ollama serve   (in another terminal)

# 2. Target cloned + sandbox image built (no downloads on camera)
python3 -m harness prepare

# 3. Warm up the model so the first call is not extra slow
ollama run qwen2.5-coder:7b "hi" > /dev/null

# 4. Clean terminal for recording
clear
```

Recording setup:
- One terminal with a large font (Ctrl + to zoom), maximized.
- VS Code (or a browser on GitHub) open on `README.md` in preview mode, to
  show the diagrams: in VS Code, open `README.md` and press `Ctrl+Shift+V`.
- Screen recorder: OBS, or GNOME's built-in recorder (`Ctrl+Shift+Alt+R`).

---

## Segment 1 — 0:00–0:20 — What it is

**Show:** `README.md` preview, scrolled to the **Architecture** diagram.

**Say:**
> "This is my Stage 1 coding harness. A local model in Ollama proposes
> actions. The harness checks every request in code, edits a disposable copy
> of the repository, runs tests in a Docker sandbox, and verifies the result
> itself."

## Segment 2 — 0:20–0:40 — Target and failing baseline

**Do (terminal):**
```bash
cat task/request.md | head -12
python3 -m harness baseline
```
After `baseline` finishes (about 10 s), scroll up a little so the
**Checks** table is visible (regression PASSED, acceptance FAILED).

**Say:**
> "The target is Cosmic Python at a pinned commit. The bug: allocating the
> same order line twice reserves stock again. On the starting code, the
> hidden acceptance check fails, 4 of 6, and the 26 existing tests pass."

## Segment 3 — 0:40–1:15 — Real model run (cut the waiting)

**Do (terminal):**
```bash
clear
python3 -m harness run --task-file task/request-guided.md --label video
```
Leave it running. It prints `[action N] asking the model...` and waits
20–150 s per call. **In editing, cut every wait** and keep only:
1. The first lines (target, model, sandbox, the task).
2. Each `request:` line and its `OK` result.
3. The final report: **Diff**, the **Checks** table and **Verdict: PASSED**.

If the run does not end with PASSED (the model is not fully deterministic),
run it again with another label (`--label video2`), or use the saved run
instead: `cat docs/evidence/run3-guided.log`.

**Say** (over the report):
> "A real run with qwen2.5-coder 7B. The model edits `model.py` and runs the
> regression check in the sandbox. Then the harness ignores the model's claim
> and re-runs scope, regression and the hidden acceptance check itself. Here
> is the diff, and all checks pass. Manual help: the 7B model needed a hint
> in the request about where the check goes."

## Segment 4 — 1:15–1:35 — The model's claim is not evidence

**Do (terminal):**
```bash
clear
sed -n '/Model claim/,/--- output of/p' docs/evidence/run2-unguided.log
grep "^Verdict" docs/evidence/run2-unguided.log
```

**Say:**
> "Without the hint, the model made an edit that does nothing (both
> branches return the same value) and said the change was complete. The
> regression tests pass, but the harness reports the acceptance check as
> failed: NOT VERIFIED."

## Segment 5 — 1:35–1:55 — Tests and safety

**Do (terminal):**
```bash
clear
python3 -m unittest discover -s tests -t .
```
It takes about 25 s and ends with `Ran 59 tests ... OK`. Cut the middle if
you need to. Then switch to the `README.md` preview, **Safety controls**
table.

**Say:**
> "59 automated tests with scripted model replies, no API key needed. Safety
> lives in code: paths are confined to the copy, and containers have no
> network, no host environment and a read-only workspace. Stuck commands are
> killed, and actions and denials are limited."

## Segment 6 — 1:55–2:00 — Close

**Say:**
> "Thanks."

---

## After recording (optional)

Remove the extra run directories created on camera:
```bash
chmod -R u+w runs/video* && rm -rf runs/video*
```

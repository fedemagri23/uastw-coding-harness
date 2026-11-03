# Demo results with a real model

Model: `qwen2.5-coder:7b` in local Ollama, CPU only (8 cores, no GPU),
temperature 0. Target: `cosmicpython/code` @ `14c84797ffa7`. Full console
logs and each run's `report.json`, `diff.patch` and `trace.jsonl` are in
[`evidence/`](evidence/).

## Starting code

`python -m harness baseline` → [`evidence/baseline.log`](evidence/baseline.log)

| Check | Result |
|---|---|
| regression | passed — 26 passed, 2 deselected |
| acceptance | **failed** — 4 failed, 2 passed (exit 1) |
| compile | passed |

## Run 1 — unguided request, first harness version (cancelled)

[`evidence/run1-unguided.log`](evidence/run1-unguided.log). In this version
the first message held only the file list. The model never read a file. It
kept searching longer and longer strings that do not exist
(`Product.allocate`, `order_line.allocate`, …,
`order_line.allocate_batch_if_not_allocated_once`). I stopped it with Ctrl+C
after 7 actions.

- Termination `cancelled`. The harness still ran the full verification and
  reported acceptance **failed**, no changed files, verdict **NOT VERIFIED**,
  and no leftover containers.
- What changed in the harness afterwards: the initial context now includes
  the content of files named in the request (`harness/context.py`), and an
  empty search returns a hint not to search longer variants. These are
  general harness improvements, not task-specific code.

## Run 2 — unguided request (wrong fix, caught by verification)

[`evidence/run2-unguided.log`](evidence/run2-unguided.log)

1. `edit_file`: changed `return batch.reference` (after allocating) into
   `return batch.reference if line not in batch._allocations else None`.
2. `run_check regression` → **exit 1** (1 failed, 25 passed). The failure was
   shown to the model.
3. `edit_file`: changed `else None` into `else batch.reference`. The
   expression is now `x if cond else x`, which has no effect.
4. `run_check regression` → exit 0.
5. Final: *"Changed the Product.allocate method to ensure that an order line
   is allocated at most once … The change is complete."*

The harness's own verification: scope passed, regression passed,
**acceptance failed (4 failed)**, verdict **NOT VERIFIED**. This is the
required "failed check stays visible even if the model says it is done"
behaviour, on a real model. The regression suite alone would have accepted
this no-op change. The hidden acceptance check did not.

## Run 3 — guided request (passed) — manual help used

[`evidence/run3-guided.log`](evidence/run3-guided.log),
task file [`../task/request-guided.md`](../task/request-guided.md).

**Manual help:** the request is the same as in run 2 plus a reviewer hint
saying where to add the check (at the start of `Product.allocate`, before
`try:`), what to check (`line in batch._allocations` → return
`batch.reference`), and which two lines to use as `old` in `edit_file`. The
model wrote the edit itself, and no file was edited by hand. This is the same
supervised approach the 7B model needed in the Week 1 lab.

1. `edit_file` on `src/allocation/domain/model.py` → ok (it compiles).
2. `run_check regression` → exit 0 (26 passed).
3. Final.

```diff
--- a/src/allocation/domain/model.py
+++ b/src/allocation/domain/model.py
@@ -13,6 +13,9 @@
         self.events = []  # type: List[events.Event]
 
     def allocate(self, line: OrderLine) -> str:
+        for batch in self.batches:
+            if line in batch._allocations:
+                return batch.reference
         try:
             batch = next(b for b in sorted(self.batches) if b.can_allocate(line))
             batch.allocate(line)
```

| Check (run by the harness) | Result |
|---|---|
| scope | passed — 1 changed file, inside `src/allocation/` |
| regression | passed — 26 passed (pristine `tests/`) |
| acceptance | **passed — 6 passed** |
| compile | passed |

Verdict **PASSED**. Counters: 2 actions, 0 denied, 0 errors, 0 invalid
replies, 3 model calls, 0 retries. Model calls took 20–150 s each on CPU (the first, with the full context, is the slowest).

## Summary

| Run | Request | Model's claim | Harness verdict |
|---|---|---|---|
| 1 | unguided (old context) | none (looping searches, cancelled) | NOT VERIFIED |
| 2 | unguided | "The change is complete" | NOT VERIFIED (acceptance 4 failed) |
| 3 | guided (manual hint) | "regression check passed" | PASSED |

Without help, the 7B model could not find the right fix. With a one-paragraph
hint it made a correct minimal edit. The harness's verdict matched reality in
all three runs, whatever the model claimed.

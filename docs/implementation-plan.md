# Stage 2 implementation plan

The assignment asks to extend the Stage 1 harness so it handles a feature across
modules, keeps context bounded and saves work safely. This plan maps each
requirement to the work done. Stage 1 stays untouched in `../stage1`; this
folder continues its Git history on the `stage-2` branch.

## Requirements and where they are met

| Part | Required | Implementation | Tests |
|---|---|---|---|
| Navigation | Modules, symbols, dependencies, Git state | `navigation.py` (`ast` index), `gitstate.py` (private repository), tools `repo_map`, `find_symbol`, `find_references`, `module_dependencies`, `read_symbol`, `git_status`, `git_diff`, `git_log` | `test_navigation.py` |
| Context | Map, file selection, shortened history under a documented limit; trusted text preserved; untrusted text separate | `context.py` (`ContextManager`, `task_contract`, `untrusted`) | `test_context.py` |
| Task execution | Save request, commit, allowed changes, checks, steps; bounded edit-test-fix loop | `session.py`, `controller.py` (`_run_checks`, stop reasons) | `test_sessions.py::LoopTest`, `test_feature.py` |
| Sessions | Save, reopen, progress, cancel, safe resume, uncertain actions | `session.py`, `resume.py`, `cancel.py`, `model.py`, `sandbox.py` | `test_sessions.py` |
| Verification | Changed files, diff, tests, lint, unfinished work; failed or unavailable stay visible | `verification.py`, `service.py::review` | `test_sessions.py`, `test_ui.py::RenderingTest` |
| CLI or UI | Select project, start, inspect, cancel, resume, review; interface separate from core | `service.py` (core), `cli.py`, `ui/` | `test_ui.py` |
| Earlier behaviour | Stage 1 tests still pass | Stage 1 test files unchanged | `test_tools`, `test_controller`, `test_limits`, `test_failed_command`, `test_bugfix`, `test_sandbox_docker` |
| Feature | Two or more interacting modules, existing callers, error case | `task/feature-expiry/` (optional batch expiry) | `test_feature.py`, real-model runs |

## Order of work

1. Copy Stage 1 and confirm its 59 tests pass as the safety net.
2. Add cancellation to the sandbox and the model client (streaming, closable socket).
3. Add navigation and private Git state, with tools.
4. Add the bounded context manager, then rewrite the loop around it.
5. Add sessions: write-ahead journal, atomic checkpoints, run lock.
6. Add safe resume: re-validate everything, pause on mismatch, never replay.
7. Choose the feature, write acceptance checks, confirm they fail on the starting code for the expected reason.
8. Add the application core and the interfaces (CLI, interactive shell).
9. Write the Stage 2 tests; run the full suite.
10. Run the feature with a real model; record the evidence and any manual help.
11. Write the README, diagrams and video script; package the delivery.

## Extras beyond the assignment

- Terminal interface in the style of current coding agents: spinner with live
  limits, tool-call bullets, line-numbered coloured diff, result panel, an
  interactive shell with slash commands and completion, Esc to cancel.
- Terminal escape sequences from model or repository text are removed before display.
- `replace_symbol`, an edit tool that needs no exact text match, for small models.
- An OpenAI-compatible provider next to Ollama, with `.env` support and a `ping` command.
- Held-out acceptance tests that the agent never sees, next to the visible feature check.
- A watchdog inside the container, so a command ends even if the harness is killed.

## Delivery checklist

- [x] Runnable source, dependencies, launch commands, example settings without secrets
- [x] Automated tests for Stage 2 and Stage 1
- [x] README: setup, model settings, design decisions, known limits, credits, diagrams
- [x] Real-model run with recorded evidence and manual help identified
- [ ] Video of at most 4 minutes (script in `docs/video-script.md`; to be recorded by the author)
- [ ] Submit to Moodle: repository link and exact commit, or the versioned ZIP

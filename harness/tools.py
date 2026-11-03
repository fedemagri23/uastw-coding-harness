"""Repository tools. Each function takes the runtime plus string arguments.

Boundary refusals raise ToolDenied (status "denied"); other failures raise
ordinary exceptions, which the runtime turns into status "error". File tools
and the sandbox share one scope: the run's workspace directory.
"""

import difflib
import os
import tempfile
import textwrap
from pathlib import Path, PurePosixPath

from .navigation import RepoIndex

MAX_CHARS = 12_000
MAX_FILE_BYTES = 64_000
MAX_LISTED = 300
MAX_MATCHES = 50
EXCERPT_CHARS = 160
MAX_REFERENCES = 60
MAX_SYMBOLS = 40
MAX_DIFF_CHARS = 8000
MAP_CHARS = 4000
SKIPPED_DIRS = {"__pycache__"}


class ToolDenied(Exception):
    """A boundary or permission check refused the request."""


# --- file boundary -------------------------------------------------------


def resolve_path(runtime, path, write=False):
    """Map a model-supplied relative path to a real path inside the workspace."""
    if not path.strip():
        raise ValueError("path must not be empty")
    if "\0" in path:
        raise ToolDenied("path contains a NUL byte")
    requested = PurePosixPath(path.replace("\\", "/"))
    if requested.is_absolute() or Path(path).is_absolute():
        raise ToolDenied("absolute paths are not allowed; use a path relative to the repository root")
    if ".." in requested.parts:
        raise ToolDenied("'..' is not allowed in paths")
    if any(part.startswith(".") for part in requested.parts):
        raise ToolDenied("hidden paths are not allowed")

    # resolve() follows symbolic links, so a link pointing outside is caught here.
    resolved = (runtime.root / requested).resolve()
    try:
        relative = resolved.relative_to(runtime.root)
    except ValueError:
        raise ToolDenied("path resolves outside the repository copy") from None
    if any(part.startswith(".") for part in relative.parts):
        raise ToolDenied("path resolves to a hidden location")
    relative = relative.as_posix()

    if write:
        for candidate in {requested.as_posix(), relative}:
            reason = runtime.policy.write_denial(candidate)
            if reason:
                raise ToolDenied(reason)
    return resolved, relative


def walk_files(root):
    """Yield visible regular files, sorted, without following symbolic links."""
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d not in SKIPPED_DIRS and not (base / d).is_symlink()
        )
        for name in sorted(files):
            full = base / name
            if not name.startswith(".") and not full.is_symlink() and full.is_file():
                yield full


# --- read-only tools -----------------------------------------------------


def list_files(runtime):
    paths = []
    for full in walk_files(runtime.root):
        if len(paths) == MAX_LISTED:
            return {"files": paths, "truncated": True, "note": f"SHORTENED: only the first {MAX_LISTED} files"}
        paths.append(full.relative_to(runtime.root).as_posix())
    return {"files": paths, "truncated": False}


def read_file(runtime, path):
    full, relative = resolve_path(runtime, path)
    if not full.is_file():
        raise FileNotFoundError(f"{relative} is not a file")
    try:
        with full.open(encoding="utf-8") as handle:
            content = handle.read(MAX_CHARS + 1)
    except UnicodeDecodeError:
        raise ValueError(f"{relative} is not UTF-8 text") from None
    result = {"path": relative, "content": content[:MAX_CHARS], "truncated": len(content) > MAX_CHARS}
    if result["truncated"]:
        result["note"] = f"SHORTENED: only the first {MAX_CHARS} characters are shown"
    return result


def search_files(runtime, query):
    if not query:
        raise ValueError("query must not be empty")
    matches, searched = [], 0
    for full in walk_files(runtime.root):
        searched += 1
        data = full.read_bytes()[:1_000_000]
        if b"\0" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if query in line:
                if len(matches) == MAX_MATCHES:
                    return {"matches": matches, "truncated": True, "files_searched": searched,
                            "note": f"SHORTENED: only the first {MAX_MATCHES} matches"}
                matches.append({
                    "path": full.relative_to(runtime.root).as_posix(),
                    "line": number,
                    "excerpt": line.strip()[:EXCERPT_CHARS],
                })
    result = {"matches": matches, "truncated": False, "files_searched": searched}
    if not matches:
        result["note"] = ("No line contains this exact text. Do not search longer variants of it: "
                          "search one short identifier (e.g. 'def allocate') or read a file instead.")
    return result


# --- navigation tools ----------------------------------------------------


def _index(runtime):
    return RepoIndex(runtime.root, list(walk_files(runtime.root)))


def repo_map(runtime):
    return {"map": _index(runtime).repo_map(max_chars=MAP_CHARS)}


def find_symbol(runtime, name):
    if not name.strip():
        raise ValueError("name must not be empty")
    symbols = _index(runtime).find_symbol(name.strip())
    result = {
        "symbols": [{"qualname": s.qualname, "kind": s.kind, "path": s.path, "line": s.line, "end_line": s.end_line,
                     "signature": s.signature} for s in symbols[:MAX_SYMBOLS]],
        "truncated": len(symbols) > MAX_SYMBOLS,
    }
    if not symbols:
        result["note"] = "No Python symbol has this name. Try find_references or search_files."
    return result


def find_references(runtime, name):
    if not name.strip():
        raise ValueError("name must not be empty")
    references = _index(runtime).find_references(name.strip())
    return {"references": references[:MAX_REFERENCES], "total": len(references),
            "truncated": len(references) > MAX_REFERENCES}


def module_dependencies(runtime, path):
    _, relative = resolve_path(runtime, path)
    index = _index(runtime)
    if relative not in index.modules:
        raise ValueError(f"{relative} is not an indexed Python module")
    return index.dependencies(relative)


def read_symbol(runtime, path, symbol):
    _, relative = resolve_path(runtime, path)
    index = _index(runtime)
    found = index.symbol_at(relative, symbol)
    if found is None:
        raise ValueError(_symbol_help(index, relative, symbol))
    lines = index.modules[relative].text.splitlines()
    return {"path": relative, "symbol": found.qualname, "kind": found.kind, "line": found.line,
            "end_line": found.end_line, "source": "\n".join(lines[found.line - 1:found.end_line])[:MAX_CHARS]}


def _symbol_help(index, relative, symbol):
    module = index.modules.get(relative)
    if module is None:
        return f"{relative} is not an indexed Python module"
    if module.error:
        return f"{relative}: {module.error}"
    names = ", ".join(s.qualname for s in module.symbols)[:800]
    return f"{symbol!r} is not one unique symbol in {relative}. Symbols there: {names or 'none'}"


# --- Git state (read-only; the private repository has no remotes) ---------


def _repo(runtime):
    if runtime.repo is None:
        raise ValueError("Git state is not available in this run")
    return runtime.repo


def git_status(runtime):
    return _repo(runtime).status()


def git_diff(runtime, path=None):
    relative = resolve_path(runtime, path)[1] if path else None
    text = _repo(runtime).diff(relative)
    result = {"diff": text[:MAX_DIFF_CHARS], "truncated": len(text) > MAX_DIFF_CHARS}
    if result["truncated"]:
        result["note"] = f"SHORTENED: only the first {MAX_DIFF_CHARS} characters are shown"
    return result


def git_log(runtime):
    return {"commits": _repo(runtime).log()}


# --- edit tools ----------------------------------------------------------


def atomic_write(full, data, exclusive=False):
    """Write through a temporary file, so a stop leaves the old or the new content, never a mix."""
    descriptor, temporary = tempfile.mkstemp(prefix=".harness-tmp-", dir=full.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.chmod(temporary, 0o666 & ~_umask())
            os.link(temporary, full)  # fails if the file appeared in the meantime
        else:
            os.chmod(temporary, full.stat().st_mode & 0o777)
            os.replace(temporary, full)
            temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _umask():
    current = os.umask(0)
    os.umask(current)
    return current


def replace_symbol(runtime, path, symbol, source):
    """Replace one whole function, method, class or module variable; indentation is adjusted."""
    full, relative = resolve_path(runtime, path, write=True)
    if not full.is_file():
        raise FileNotFoundError(f"{relative} is not a file")
    if len(source.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"source exceeds {MAX_FILE_BYTES} bytes")
    index = _index(runtime)
    found = index.symbol_at(relative, symbol)
    if found is None:
        raise ValueError(_symbol_help(index, relative, symbol))
    original = index.modules[relative].text
    lines = original.splitlines(keepends=True)
    block = textwrap.indent(textwrap.dedent(source.strip("\n")), " " * found.col).rstrip() + "\n"
    updated = "".join(lines[: found.line - 1]) + block + "".join(lines[found.end_line:])
    if updated == original:
        raise ValueError("source is identical to the current symbol; the edit would change nothing")
    try:
        compile(updated, relative, "exec")
    except SyntaxError as exc:
        raise ValueError(f"edit rejected, the result would not be valid Python: {exc.msg} at line {exc.lineno}. "
                         "Give the complete definition of the symbol.") from None
    data = updated.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"edited file would exceed {MAX_FILE_BYTES} bytes")
    atomic_write(full, data)
    return {"path": relative, "symbol": found.qualname, "replaced_lines": [found.line, found.end_line],
            "new_lines": block.count("\n")}


def write_file(runtime, path, content):
    full, relative = resolve_path(runtime, path, write=True)
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"content exceeds {MAX_FILE_BYTES} bytes")
    if full.exists():
        raise FileExistsError(f"{relative} already exists; use edit_file")
    full.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(full, data, exclusive=True)
    except FileExistsError:
        raise FileExistsError(f"{relative} already exists; use edit_file") from None
    return {"path": relative, "bytes": len(data), "created": True}


def closest_lines(text, old):
    """Show the real lines most similar to the first line of `old`."""
    lines = text.splitlines()
    target = old.strip().splitlines()[0] if old.strip() else old
    stripped = [line.strip() for line in lines]
    close = difflib.get_close_matches(target.strip(), stripped, n=3, cutoff=0.4)
    if not close:
        return "No similar line found; read the file again."
    shown = [f"line {stripped.index(c) + 1}: {lines[stripped.index(c)]!r}" for c in close]
    return "Closest existing lines: " + "; ".join(shown)


def edit_file(runtime, path, old, new):
    full, relative = resolve_path(runtime, path, write=True)
    if not full.is_file():
        raise FileNotFoundError(f"{relative} is not a file")
    if full.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{relative} is larger than {MAX_FILE_BYTES} bytes")
    if not old:
        raise ValueError("old must not be empty")
    if old == new:
        raise ValueError("new is identical to old; the edit would change nothing")
    text = full.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ValueError(f"old occurs 0 times in {relative}; copy it exactly from read_file output. "
                         + closest_lines(text, old))
    if count != 1:
        raise ValueError(f"old occurs {count} times in {relative}; include more lines so it occurs exactly once")
    updated = text.replace(old, new, 1)
    if relative.endswith(".py"):
        try:
            compile(updated, relative, "exec")
        except SyntaxError as exc:
            raise ValueError(f"edit rejected, the result would not be valid Python: {exc.msg} "
                             f"at line {exc.lineno}. Check the indentation of every line in new.") from None
    data = updated.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"edited file would exceed {MAX_FILE_BYTES} bytes")
    atomic_write(full, data)
    start = updated.count("\n", 0, updated.index(new)) + 1 if new else None
    return {"path": relative, "replacements": 1, "bytes": len(data), "changed_from_line": start}


# --- contained execution -------------------------------------------------


def _command_result(result):
    """What the model sees of a command: exit code and output stay visible."""
    shown = {
        "command": result["command"],
        "exit_code": result["exit_code"],
        "passed": result["exit_code"] == 0,
        "timed_out": result["timed_out"],
        "truncated": result["truncated"],
        "output": result["output"],
    }
    if result.get("note"):
        shown["note"] = result["note"]
    return shown


def run_tests(runtime, path):
    """Run pytest on one test file or directory of the workspace, in the sandbox."""
    _, relative = resolve_path(runtime, path)
    if not (runtime.root / relative).exists():
        raise FileNotFoundError(f"{relative} does not exist")
    if not relative.startswith("tests"):
        raise ToolDenied("run_tests only runs paths under tests/")
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", relative]
    return _command_result(runtime.run_command(argv))


def run_check(runtime, name):
    """Run one configured check (see task/target.toml) in the sandbox."""
    if name not in runtime.agent_checks:
        raise ValueError(f"unknown check {name!r}; available: {', '.join(sorted(runtime.agent_checks))}")
    check = runtime.agent_checks[name]
    mounts = [(runtime.acceptance_dir, "/acceptance")] if check.needs_acceptance else []
    return _command_result(runtime.run_command(list(check.argv), mounts=mounts))

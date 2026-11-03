"""Tool registry: one data entry per tool instead of a chain of conditions.

Each entry holds what the model needs (description, argument names) and what
the runtime needs (whether the tool changes files, and which function runs it).
"""

from dataclasses import dataclass
from typing import Callable

from . import tools


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: tuple
    func: Callable
    mutates: bool = False
    executes: bool = False
    optional: tuple = ()   # argument names that may be left out
    needs_git: bool = False  # only offered when the run has private Git state


REGISTRY = {
    tool.name: tool
    for tool in [
        Tool("list_files", "List up to 300 file paths of the repository.", (), tools.list_files),
        Tool(
            "read_file",
            "Read a UTF-8 text file (bounded; shortened output is labelled).",
            ("path",),
            tools.read_file,
        ),
        Tool(
            "search_files",
            "Find an exact literal string (not a question) in repository files; returns "
            "path, line number and excerpt. Use one short word or code fragment.",
            ("query",),
            tools.search_files,
        ),
        Tool(
            "repo_map",
            "Compact map of Python modules: imports, classes, methods and functions.",
            (),
            tools.repo_map,
        ),
        Tool(
            "find_symbol",
            "Find where a class, function, method or module variable is defined (Python, by "
            "name or Class.method). Returns path, kind, line range and signature.",
            ("name",),
            tools.find_symbol,
        ),
        Tool(
            "find_references",
            "Find code lines that use a Python name (variable, attribute, import, keyword "
            "argument). Use it to see which callers a change affects.",
            ("name",),
            tools.find_references,
        ),
        Tool(
            "module_dependencies",
            "Show which repository modules a Python file imports and which modules import it.",
            ("path",),
            tools.module_dependencies,
        ),
        Tool(
            "read_symbol",
            "Read the source of one symbol in a file, e.g. symbol 'Product.allocate'.",
            ("path", "symbol"),
            tools.read_symbol,
        ),
        Tool(
            "git_status",
            "Show the starting commit and which files differ from it.",
            (),
            tools.git_status,
            needs_git=True,
        ),
        Tool(
            "git_diff",
            "Show your changes since the starting commit, optionally for one path.",
            (),
            tools.git_diff,
            optional=("path",),
            needs_git=True,
        ),
        Tool(
            "git_log",
            "Show the latest commits of the target repository.",
            (),
            tools.git_log,
            needs_git=True,
        ),
        Tool(
            "write_file",
            "Create a new file inside the writable scope. Fails if the file already exists.",
            ("path", "content"),
            tools.write_file,
            mutates=True,
        ),
        Tool(
            "edit_file",
            "Replace `old` with `new` in a file inside the writable scope, only when `old` occurs "
            "exactly once. Copy `old` exactly from read_file output, including leading spaces; "
            "write every line of `new` with its full indentation. Python edits that do not "
            "compile are rejected.",
            ("path", "old", "new"),
            tools.edit_file,
            mutates=True,
        ),
        Tool(
            "replace_symbol",
            "Replace one whole function, method, class or module variable with `source`, the "
            "complete new definition; indentation is adjusted for you. Preferred over edit_file "
            "for Python. Edits that do not compile are rejected.",
            ("path", "symbol", "source"),
            tools.replace_symbol,
            mutates=True,
        ),
        Tool(
            "run_tests",
            "Run pytest on one test file or directory under tests/ inside the sandbox; returns "
            "exit code and output.",
            ("path",),
            tools.run_tests,
            executes=True,
        ),
        Tool(
            "run_check",
            "Run a configured check by name inside the sandbox; returns exit code and output.",
            ("name",),
            tools.run_check,
            executes=True,
        ),
    ]
}

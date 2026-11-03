"""Language-aware repository navigation for Python, based on the `ast` module.

RepoIndex parses the visible .py files of a workspace and answers: which
modules exist, where a symbol is defined, where a name is used, and which
modules depend on each other. Files that do not parse stay listed and are
reported instead of breaking the index. Other languages appear in the map by
path only; search_files covers their text.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

MAX_SOURCE_BYTES = 512_000
SOURCE_ROOTS = ("src/", "lib/")
STOP_WORDS = frozenset("""a about after all also an and any are as at be been before but by can each else even for
from has have if in into is it its keep may must new no not of on one only or other our should so such that the
their them then there these they this those to under use used using was we when where which while will with
without would you your add make change code existing file files function module work""".split())

# Plain words in a request that usually point to code with other names.
RELATED_WORDS = {
    "database": {"orm", "sql", "table", "column", "repository", "migration", "model"},
    "persist": {"orm", "sql", "table", "column", "repository"},
    "saved": {"orm", "sql", "table", "column", "repository"},
    "stored": {"orm", "sql", "table", "column", "repository"},
    "endpoint": {"flask", "route", "api", "view"},
    "http": {"flask", "route", "api", "view"},
    "command": {"handler", "bus"},
}


@dataclass(frozen=True)
class Symbol:
    name: str
    qualname: str
    kind: str  # class, function, method or variable
    path: str
    line: int
    end_line: int
    signature: str
    col: int = 0


def tokens(text):
    """Lower-case words from prose or identifiers, with simple plural/verb folding."""
    found = set()
    for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.replace("_", " "))):
        word = word.lower()
        if len(word) < 3 or word in STOP_WORDS:
            continue
        for suffix in ("ations", "ation", "ing", "ies", "es", "ed", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        found.add(word)
    return found


def module_name(relative):
    """Dotted module for a repository path: src/pkg/mod.py -> pkg.mod."""
    path = relative[:-3] if relative.endswith(".py") else relative
    for root in SOURCE_ROOTS:
        if path.startswith(root):
            path = path[len(root):]
            break
    parts = path.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _signature(node):
    params = [arg.arg for arg in node.args.posonlyargs + node.args.args]
    if node.args.vararg:
        params.append("*" + node.args.vararg.arg)
    params += [arg.arg for arg in node.args.kwonlyargs]
    if node.args.kwarg:
        params.append("**" + node.args.kwarg.arg)
    return "(" + ", ".join(p for p in params if p not in ("self", "cls")) + ")"


def _start_line(node):
    decorators = getattr(node, "decorator_list", [])
    return min([d.lineno for d in decorators] + [node.lineno])


class _Module:
    __slots__ = ("path", "name", "text", "symbols", "imports", "external", "error", "words")

    def __init__(self, path, name, text):
        self.path, self.name, self.text = path, name, text
        self.symbols, self.imports, self.external = [], set(), set()
        self.error = None
        self.words = set()


class RepoIndex:
    """A parsed snapshot of the workspace. Build a new one after files change."""

    def __init__(self, root, files):
        self.root = Path(root)
        self.paths = [Path(f).relative_to(self.root).as_posix() for f in files]
        self.modules = {}
        for full, relative in zip(files, self.paths):
            if relative.endswith(".py"):
                self.modules[relative] = self._parse(Path(full), relative)
        self._by_name = {m.name: path for path, m in self.modules.items()}
        for module in self.modules.values():
            self._resolve_imports(module)

    # --- parsing ---------------------------------------------------------

    def _parse(self, full, relative):
        module = _Module(relative, module_name(relative), "")
        if full.stat().st_size > MAX_SOURCE_BYTES:
            module.error = "file too large to index"
            return module
        try:
            module.text = full.read_text(encoding="utf-8")
            tree = ast.parse(module.text)
        except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
            module.error = f"cannot parse: {exc}"
            return module
        self._collect(tree.body, module, prefix="")
        module.words = tokens(relative) | tokens(" ".join(s.name for s in module.symbols))
        doc = ast.get_docstring(tree)
        if doc:
            module.words |= tokens(doc)
        module.imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        return module

    def _collect(self, body, module, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualname = prefix + node.name
                module.symbols.append(Symbol(node.name, qualname, "class", module.path, _start_line(node),
                                             node.end_lineno, "", node.col_offset))
                self._collect(node.body, module, qualname + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if prefix else "function"
                module.symbols.append(Symbol(node.name, prefix + node.name, kind, module.path, _start_line(node),
                                             node.end_lineno, _signature(node), node.col_offset))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        module.symbols.append(Symbol(target.id, prefix + target.id, "variable", module.path,
                                                     node.lineno, node.end_lineno, "", node.col_offset))

    def _internal(self, name, current):
        """Longest existing module that `name` or one of its parents refers to."""
        while name:
            path = self._by_name.get(name)
            if path:
                return None if path == current else path
            name = name.rpartition(".")[0]
        return None

    def _resolve_imports(self, module):
        nodes = module.imports if isinstance(module.imports, list) else []
        internal, external = set(), set()
        package = module.name.split(".")
        if not module.path.endswith("__init__.py"):
            package = package[:-1]
        for node in nodes:
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                base = node.module or ""
                if node.level:
                    keep = package[: len(package) - (node.level - 1)]
                    base = ".".join(keep + ([base] if base else []))
                targets = [f"{base}.{alias.name}" for alias in node.names]
            for target in targets:
                path = self._internal(target, module.path)
                if path:
                    internal.add(path)
                elif target.split(".")[0] not in {m.split(".")[0] for m in self._by_name}:
                    external.add(target.split(".")[0])
        module.imports, module.external = internal, external

    # --- queries ---------------------------------------------------------

    def find_symbol(self, name):
        """Exact name or qualified-name matches, or substring matches if there are none."""
        symbols = [s for m in self.modules.values() for s in m.symbols]
        exact = [s for s in symbols if name in (s.name, s.qualname)]
        return exact or [s for s in symbols if name.lower() in s.qualname.lower()]

    def symbol_at(self, path, qualname):
        module = self.modules.get(path)
        if module is None:
            return None
        matches = [s for s in module.symbols if s.qualname == qualname]
        if not matches:
            matches = [s for s in module.symbols if s.name == qualname]
        return matches[0] if len(matches) == 1 else None

    def find_references(self, name):
        """Lines that use `name` as a variable, attribute, import or keyword argument."""
        found = []
        for path, module in self.modules.items():
            if module.error or name not in module.text:
                continue
            lines = module.text.splitlines()
            definitions = {s.line for s in module.symbols if s.name == name and s.kind != "variable"}
            seen = set()
            for node in ast.walk(ast.parse(module.text)):
                line, kind = None, None
                if isinstance(node, ast.Name) and node.id == name:
                    line, kind = node.lineno, "name"
                elif isinstance(node, ast.Attribute) and node.attr == name:
                    line, kind = node.lineno, "attribute"
                elif isinstance(node, ast.keyword) and node.arg == name:
                    line, kind = node.value.lineno, "keyword"
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    if any(name in (alias.name, alias.asname) or alias.name.endswith("." + name)
                           for alias in node.names):
                        line, kind = node.lineno, "import"
                if line and line not in definitions and line not in seen:
                    seen.add(line)
                    found.append({"path": path, "line": line, "kind": kind, "excerpt": lines[line - 1].strip()[:160]})
        return sorted(found, key=lambda r: (r["path"], r["line"]))

    def dependencies(self, path):
        module = self.modules[path]
        imported_by = sorted(p for p, m in self.modules.items() if path in m.imports)
        return {"path": path, "module": module.name, "imports": sorted(module.imports),
                "imported_by": imported_by, "external": sorted(module.external)}

    # --- selection and the repository map --------------------------------

    def rank_files(self, request):
        """Score files against the request. Returns [(path, score, reasons)], best first."""
        wanted = tokens(request)
        for word in re.findall(r"[a-z]+", request.lower()):
            wanted |= RELATED_WORDS.get(word, set())
        # A word found in most modules (often the package name) says nothing about relevance.
        sources = [m for path, m in self.modules.items() if not self._is_test(path)]
        if len(sources) >= 4:
            wanted = {w for w in wanted if sum(w in m.words for m in sources) <= len(sources) * 0.5}
        scores = {}
        for path, module in self.modules.items():
            if path.endswith("__init__.py") and not module.symbols:
                continue
            path_hits = wanted & tokens(path)
            symbol_hits = wanted & tokens(" ".join(s.name for s in module.symbols))
            word_hits = (wanted & module.words) - path_hits - symbol_hits
            score = 3.0 * len(path_hits) + 2.0 * len(symbol_hits) + 0.5 * len(word_hits)
            if path in request:
                score += 20
            exact = [s.name for s in module.symbols if len(s.name) > 3 and re.search(rf"\b{re.escape(s.name)}\b", request)]
            score += 4.0 * len(set(exact))
            if self._is_test(path):
                score *= 0.25  # tests explain behaviour but are rarely what must change
            reasons = sorted(path_hits | symbol_hits) + sorted(set(exact))
            scores[path] = [score, reasons]
        # Neighbours of strong matches get a boost: a feature usually crosses imports, in both directions.
        base = {path: value[0] for path, value in scores.items()}
        strongest = sorted(base, key=lambda p: -base[p])[:4]
        for path in scores:
            module = self.modules[path]
            linked = [p for p in strongest if p in module.imports or path in self.modules[p].imports]
            if linked and not self._is_test(path):
                scores[path][0] += 0.2 * sum(base[p] for p in linked)
                if not scores[path][1]:
                    scores[path][1] = [f"linked to {linked[0]}"]
        ranked = sorted(scores.items(), key=lambda item: (-item[1][0], item[0]))
        return [(path, round(value[0], 2), value[1]) for path, value in ranked]

    @staticmethod
    def _is_test(path):
        return path.startswith("tests/") or "/tests/" in path or Path(path).name.startswith(("test_", "conftest"))

    def select_files(self, request, limit):
        """Non-test files that match the request, best first."""
        return [path for path, score, _ in self.rank_files(request) if score > 0 and not self._is_test(path)][:limit]

    def _module_entry(self, module):
        lines = [module.path]
        short = lambda path: self.modules[path].name  # noqa: E731
        if module.imports:
            lines[0] += "  imports: " + ", ".join(sorted(short(p) for p in module.imports))
        if module.error:
            lines.append(f"  !! {module.error}")
        methods = {}
        for symbol in module.symbols:
            if symbol.kind == "method":
                owner = symbol.qualname.rsplit(".", 1)[0]
                methods.setdefault(owner, []).append(symbol.name + symbol.signature)
        for symbol in module.symbols:
            if symbol.kind == "class":
                inside = methods.get(symbol.qualname, [])
                lines.append(f"  class {symbol.qualname}" + (": " + ", ".join(inside) if inside else ""))
            elif symbol.kind == "function":
                lines.append(f"  def {symbol.name}{symbol.signature}")
            elif symbol.kind == "variable" and "." not in symbol.qualname and symbol.name[0].islower():
                lines.append(f"  {symbol.name} =")
        return "\n".join(lines)

    def repo_map(self, request="", max_chars=4000):
        """Compact map, most relevant modules first. Stays under max_chars and says what it omits."""
        ordered = [path for path, _, _ in self.rank_files(request)]
        ordered += [path for path in self.modules if path not in ordered]
        tests = [p for p in ordered if self._is_test(p)]
        ordered = [p for p in ordered if not self._is_test(p)]
        parts, used, names_only = [], 0, []
        for path in ordered:
            entry = self._module_entry(self.modules[path])
            if used + len(entry) + 1 > max_chars:
                names_only.append(path)
                continue
            parts.append(entry)
            used += len(entry) + 1
        others = [p for p in self.paths if not p.endswith(".py")]
        for label, paths in (("modules without details", names_only), ("test modules", tests), ("other files", others)):
            if not paths:
                continue
            text = f"{label}: "
            shown = []
            for path in paths:
                if used + len(text) + len(", ".join(shown + [path])) + 40 > max_chars:
                    break
                shown.append(path)
            omitted = len(paths) - len(shown)
            line = text + ", ".join(shown) + (f" (+{omitted} more)" if omitted else "")
            parts.append(line)
            used += len(line) + 1
        return "\n".join(parts)[:max_chars]

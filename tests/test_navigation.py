"""Navigation: modules, symbols, dependencies and Git state, plain and through the tools."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from harness import config
from harness.gitstate import ShadowRepo
from harness.navigation import RepoIndex
from harness.runtime import Runtime
from harness.tools import walk_files
from tests.helpers import make_git_target


class NavigationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nav-test-"))
        self.vendor, self.commit = make_git_target(self.tmp)
        self.workspace = self.tmp / "workspace"
        shutil.copytree(self.vendor, self.workspace, ignore=shutil.ignore_patterns(".git"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.index = RepoIndex(self.workspace, list(walk_files(self.workspace)))


class SymbolTest(NavigationBase):
    def test_finds_class_function_method_and_variable(self):
        by_kind = {s.kind: s for name in ("Reservation", "reserve_stock", "__init__", "TAX_RATE")
                   for s in self.index.find_symbol(name)}
        self.assertEqual(by_kind["class"].path, "src/shop/inventory.py")
        self.assertEqual(by_kind["function"].signature, "(sku, qty)")
        self.assertEqual(by_kind["method"].qualname, "Reservation.__init__")
        self.assertEqual(by_kind["variable"].path, "src/shop/pricing.py")

    def test_finds_references_and_ignores_the_definition(self):
        references = self.index.find_references("reserve_stock")
        paths = {r["path"] for r in references}
        self.assertEqual(paths, {"src/shop/report.py", "tests/test_inventory.py"})
        self.assertNotIn(("src/shop/inventory.py", 11), {(r["path"], r["line"]) for r in references})

    def test_a_file_that_does_not_parse_is_reported_not_fatal(self):
        (self.workspace / "src/shop/broken.py").write_text("def broken(:\n")
        index = RepoIndex(self.workspace, list(walk_files(self.workspace)))
        self.assertIn("cannot parse", index.modules["src/shop/broken.py"].error)
        self.assertTrue(index.find_symbol("reserve_stock"))
        self.assertIn("broken.py", index.repo_map("anything"))


class DependencyTest(NavigationBase):
    def test_absolute_and_relative_imports_resolve_both_ways(self):
        inventory = self.index.dependencies("src/shop/inventory.py")
        self.assertEqual(inventory["imports"], ["src/shop/pricing.py"])
        self.assertIn("src/shop/report.py", inventory["imported_by"])  # relative import
        self.assertIn("tests/test_inventory.py", inventory["imported_by"])


class SelectionTest(NavigationBase):
    def test_selection_ranks_relevant_modules_and_excludes_tests(self):
        selected = self.index.select_files("Add expiry to stock reservation", 2)
        self.assertEqual(selected[0], "src/shop/inventory.py")
        self.assertFalse([p for p in selected if p.startswith("tests/")])

    def test_import_neighbours_are_included(self):
        self.assertIn("src/shop/pricing.py", self.index.select_files("reserve stock", 3))

    def test_repository_map_stays_under_its_limit_and_says_what_is_missing(self):
        for limit in (120, 300, 2000):
            self.assertLessEqual(len(self.index.repo_map("stock", limit)), limit)
        self.assertIn("class Reservation", self.index.repo_map("stock", 2000))

    @unittest.skipUnless((config.VENDOR_DIR / "cosmicpython__code/src").is_dir(), "target not prepared")
    def test_feature_request_selects_the_modules_it_needs_in_the_real_target(self):
        root = config.VENDOR_DIR / "cosmicpython__code"
        index = RepoIndex(root, list(walk_files(root)))
        request = (config.PROJECT_DIR / "task/feature-expiry/request.md").read_text()
        selected = index.select_files(request, config.ContextSettings().max_selected_files)
        for needed in ("domain/model.py", "domain/commands.py", "service_layer/handlers.py", "adapters/orm.py"):
            self.assertIn(f"src/allocation/{needed}", selected)


class GitStateTest(NavigationBase):
    def setUp(self):
        super().setUp()
        self.repo = ShadowRepo.create(self.vendor, self.commit, self.tmp / "git", self.workspace)

    def test_clean_state_shows_the_real_starting_commit(self):
        status = self.repo.status()
        self.assertEqual(status["head"], self.commit)
        self.assertTrue(status["clean"])
        self.assertEqual(self.repo.log(), [self.repo.log()[0]])

    def test_changes_appear_in_status_and_diff_including_new_files(self):
        (self.workspace / "src/shop/pricing.py").write_text("TAX_RATE = 10\n")
        (self.workspace / "src/shop/new.py").write_text("x = 1\n")
        status = self.repo.status()
        self.assertEqual({(c["status"], c["path"]) for c in status["changes"]},
                         {("M", "src/shop/pricing.py"), ("??", "src/shop/new.py")})
        diff = self.repo.diff()
        self.assertIn("+TAX_RATE = 10", diff)
        self.assertIn("src/shop/new.py", diff)

    def test_no_remote_so_nothing_can_be_pushed(self):
        self.assertEqual(self.repo.remotes(), [])
        self.assertFalse((self.workspace / ".git").exists())

    def test_snapshot_tracks_content_and_restore_brings_it_back(self):
        before = self.repo.snapshot()
        (self.workspace / "src/shop/pricing.py").write_text("changed\n")
        (self.workspace / "extra.txt").write_text("extra\n")
        after = self.repo.snapshot()
        self.assertNotEqual(before, after)
        self.assertEqual({c["path"] for c in self.repo.changed_between(before, after)}, {"src/shop/pricing.py", "extra.txt"})
        self.repo.restore(before)
        self.assertEqual(self.repo.snapshot(), before)
        self.assertFalse((self.workspace / "extra.txt").exists())

    def test_relative_paths_work(self):
        previous = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous)
        repo = ShadowRepo("git", "workspace")
        self.assertEqual(repo.snapshot(), self.repo.snapshot())

    def test_git_hooks_in_host_config_do_not_run(self):
        hooks = self.tmp / "git" / "hooks"
        hooks.mkdir(exist_ok=True)
        marker = self.tmp / "ran"
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        self.repo.status()
        self.repo.snapshot()
        self.assertFalse(marker.exists())


class ToolsTest(NavigationBase):
    def setUp(self):
        super().setUp()
        self.repo = ShadowRepo.create(self.vendor, self.commit, self.tmp / "git", self.workspace)
        self.runtime = Runtime(self.workspace, writable=("src/*.py", "src/**/*.py"), protected=("tests/**",), repo=self.repo)

    def run_tool(self, tool, **args):
        return self.runtime.execute({"tool": tool, "args": args})

    def test_tools_return_navigation_results(self):
        self.assertEqual(self.run_tool("find_symbol", name="Reservation")["output"]["symbols"][0]["kind"], "class")
        self.assertEqual(self.run_tool("find_references", name="total")["output"]["total"], 2)
        self.assertEqual(self.run_tool("module_dependencies", path="src/shop/report.py")["output"]["imports"],
                         ["src/shop/inventory.py"])
        self.assertIn("def total", self.run_tool("read_symbol", path="src/shop/pricing.py", symbol="total")["output"]["source"])
        self.assertIn("inventory.py", self.run_tool("repo_map")["output"]["map"])

    def test_git_tools_show_state_and_have_an_optional_path(self):
        self.assertTrue(self.run_tool("git_status")["output"]["clean"])
        self.run_tool("replace_symbol", path="src/shop/pricing.py", symbol="total",
                      source="def total(prices):\n    return sum(prices) + 1\n")
        self.assertFalse(self.run_tool("git_status")["output"]["clean"])
        self.assertIn("+ 1", self.run_tool("git_diff")["output"]["diff"])
        self.assertEqual(self.run_tool("git_diff", path="README.md")["output"]["diff"], "")
        self.assertEqual(self.run_tool("git_diff", other="x")["status"], "error")
        self.assertEqual(self.run_tool("git_diff", path="../outside")["status"], "denied")

    def test_replace_symbol_keeps_indentation_inside_a_class(self):
        result = self.run_tool("replace_symbol", path="src/shop/inventory.py", symbol="Reservation.__init__",
                               source="def __init__(self, sku, qty, expiry=None):\n    self.sku = sku\n    self.qty = qty\n    self.expiry = expiry\n")
        self.assertEqual(result["status"], "ok")
        text = (self.workspace / "src/shop/inventory.py").read_text()
        self.assertIn("    def __init__(self, sku, qty, expiry=None):\n        self.sku = sku", text)
        compile(text, "x", "exec")

    def test_replace_symbol_rejects_bad_code_unknown_symbols_and_protected_files(self):
        before = (self.workspace / "src/shop/pricing.py").read_text()
        bad = self.run_tool("replace_symbol", path="src/shop/pricing.py", symbol="total", source="def total(:\n")
        self.assertIn("not be valid Python", bad["output"])
        unknown = self.run_tool("replace_symbol", path="src/shop/pricing.py", symbol="nope", source="x = 1\n")
        self.assertIn("Symbols there", unknown["output"])
        protected = self.run_tool("replace_symbol", path="tests/test_inventory.py", symbol="test_reserve", source="x = 1\n")
        self.assertEqual(protected["status"], "denied")
        self.assertEqual((self.workspace / "src/shop/pricing.py").read_text(), before)

    def test_edits_leave_no_temporary_files(self):
        self.run_tool("edit_file", path="src/shop/pricing.py", old="TAX_RATE = 21", new="TAX_RATE = 10")
        self.run_tool("write_file", path="src/shop/added.py", content="x = 1\n")
        self.assertEqual([p.name for p in (self.workspace / "src/shop").iterdir() if p.name.startswith(".")], [])

    def test_git_tools_are_not_offered_without_git_state(self):
        runtime = Runtime(self.workspace)
        self.assertEqual(runtime.execute({"tool": "git_status", "args": {}})["status"], "denied")
        self.assertNotIn("git_status", [t["name"] for t in runtime.describe_tools()])

    def test_no_generic_git_or_push_tool_exists(self):
        names = [t["name"] for t in self.runtime.describe_tools()]
        self.assertFalse([n for n in names if "push" in n or "merge" in n or n == "git"])


if __name__ == "__main__":
    unittest.main()

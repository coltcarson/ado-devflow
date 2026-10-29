"""Deterministic scope/report tests; these do not evaluate Copilot's judgment."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "security_scan.py"
SPEC = importlib.util.spec_from_file_location("security_scan", SCRIPT)
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


class SecurityScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "commit.gpgsign", "false")
        self.write(".gitignore", ".ado/\n.env\n")
        self.write("app.py", "value = 1\n")
        self.write("support.py", "control = True\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def init(self, mode="changes", run="case"):
        return scan.initialize(self.root, run, mode, self.base if mode == "changes" else None)

    def prepare(self):
        result = scan.prepare(self.root, "case")
        snapshot = scan.read_json(Path(result["snapshot"]))
        review = scan.read_json(Path(result["review"]))
        return snapshot, review, Path(result["review"])

    def completed_review(self):
        snapshot, review, path = self.prepare()
        review["threat_model"] = "Local fixture; ordinary user input is untrusted."
        review["reviewed"] = [row["path"] for row in snapshot["files"]]
        return snapshot, review, path

    def finding(self, status="open", severity="high", method="source"):
        return {"id": "SEC-001", "title": "Fixture vulnerability", "path": "app.py", "line": 1,
                "severity": severity, "status": status, "attribution": "introduced",
                "evidence": "Current fixture source at app.py:1", "attacker": "Untrusted caller",
                "attack_path": "Input reaches a sensitive operation without validation.",
                "impact": "Unauthorized data access", "prerequisites": "Route enabled",
                "counterevidence": "No effective control in the fixture",
                "validation": {"method": method, "detail": "Fixture source examined; not a real scan."},
                "remediation": "Enforce the authorization boundary", "history": []}

    def render(self, review, path):
        scan.write_json(path, review)
        return scan.report(self.root, "case", path)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.root,
                              capture_output=True, text=True)

    def test_cumulative_staged_unstaged_untracked_deleted_and_renamed(self):
        self.init()
        self.write("app.py", "value = 2\n")
        self.git("add", "app.py")
        self.git("commit", "-qm", "first implementation")
        self.git("mv", "support.py", "renamed.py")
        self.write("new.py", "new = True\n")
        self.git("add", "new.py")
        self.write("app.py", "value = 3\n")
        unusual = "space tab\tline\n--$(echo nope).py" if os.name != "nt" else "space file.py"
        self.write(unusual, "untracked = True\n")
        snapshot, _, _ = self.prepare()
        paths = {row["path"]: row for row in snapshot["files"]}
        self.assertEqual(set(paths), {"app.py", "support.py", "renamed.py", "new.py", unusual})
        self.assertEqual(paths["support.py"]["worktree"]["kind"], "deleted")
        self.assertEqual(snapshot["base"], self.base)

    def test_index_only_change_is_not_lost(self):
        self.init()
        self.write("app.py", "value = 2\n")
        self.git("add", "app.py")
        self.write("app.py", "value = 1\n")
        snapshot, _, _ = self.prepare()
        self.assertIn("app.py", [row["path"] for row in snapshot["files"]])

    def test_preexisting_edits_recorded_without_excluding_them(self):
        self.write("app.py", "user_edit = True\n")
        self.write("user-notes.txt", "user notes\n")
        initial = self.init()
        self.assertEqual(initial["preexisting_edits"], ["app.py", "user-notes.txt"])
        self.write("app.py", "user_edit = True\nagent_edit = True\n")
        snapshot, _, _ = self.prepare()
        self.assertTrue(all(row["preexisting_edit"] for row in snapshot["files"]))

    def test_base_is_pinned_and_initialization_cannot_replace_it(self):
        self.init()
        self.write("app.py", "value = 2\n")
        self.git("add", "app.py")
        self.git("commit", "-qm", "change")
        with self.assertRaises(scan.ScanError):
            self.init()
        snapshot, _, _ = self.prepare()
        self.assertEqual(snapshot["base"], self.base)
        self.assertEqual([row["path"] for row in snapshot["files"]], ["app.py"])

    def test_missing_or_invalid_baseline_has_no_silent_fallback(self):
        for base in (None, "does-not-exist", "--output=malicious"):
            with self.subTest(base=base), self.assertRaises(scan.ScanError):
                scan.initialize(self.root, "case", "changes", base)
        self.assertFalse((self.root / ".ado/security/case").exists())

    def test_repository_inventory_includes_current_files(self):
        self.write("docs/notes.md", "notes\n")
        self.write(".env", "do not read\n")
        self.init("repository")
        snapshot, _, _ = self.prepare()
        self.assertEqual({r["path"] for r in snapshot["files"]},
                         {".gitignore", "app.py", "support.py", "docs/notes.md"})

    def test_report_outputs_excluded_but_other_docs_included(self):
        self.init()
        self.write("app.py", "value = 2\n")
        _, review, path = self.completed_review()
        result = self.render(review, path)
        self.assertTrue(scan.freshness(self.root, "case")["fresh"])
        self.write("docs/unrelated.md", "still review this\n")
        snapshot, _, _ = self.prepare()
        paths = {r["path"] for r in snapshot["files"]}
        self.assertIn("docs/unrelated.md", paths)
        self.assertNotIn(Path(result["report"]).relative_to(self.root).as_posix(), paths)

    def test_source_supporting_code_and_index_changes_invalidate_review(self):
        self.init()
        self.write("app.py", "value = 2\n")
        _, review, path = self.completed_review()
        self.render(review, path)
        self.write("support.py", "control = False\n")
        self.assertFalse(scan.freshness(self.root, "case")["fresh"])
        self.write("support.py", "control = True\n")
        self.assertTrue(scan.freshness(self.root, "case")["fresh"])
        self.git("add", "app.py")
        self.assertFalse(scan.freshness(self.root, "case")["fresh"])
        _, review, path = self.completed_review()
        self.render(review, path)
        self.write("app.py", "value = 3\n")
        self.assertFalse(scan.freshness(self.root, "case")["fresh"])

    def test_reverted_finding_retains_history_after_leaving_inventory(self):
        self.init()
        self.write("app.py", "value = 2\n")
        original, _, _ = self.completed_review()
        self.write("app.py", "value = 1\n")
        snapshot, review, path = self.completed_review()
        self.assertEqual(snapshot["files"], [])
        finding = self.finding("fixed")
        finding["origin_snapshot_id"] = original["id"]
        finding["history"] = ["Reverted the change and revalidated its removal."]
        review["findings"] = [finding]
        result = self.render(review, path)
        self.assertIn("high / fixed: 1", Path(result["report"]).read_text())
        finding["status"] = "open"
        with self.assertRaises(scan.ScanError):
            self.render(review, path)

    def test_repeated_report_does_not_overwrite(self):
        self.init()
        _, review, path = self.completed_review()
        first = self.render(review, path)
        second = self.render(review, path)
        self.assertNotEqual(first["report"], second["report"])
        self.assertTrue(Path(first["report"]).exists())
        self.assertTrue(scan.freshness(self.root, "case")["fresh"])

    def test_commit_of_reviewed_staged_bytes_stays_fresh(self):
        self.init()
        self.write("app.py", "value = 2\n")
        self.git("add", "app.py")
        _, review, path = self.completed_review()
        self.render(review, path)
        self.git("commit", "-qm", "reviewed bytes")
        self.assertTrue(scan.freshness(self.root, "case")["fresh"])

    def test_critical_findings_are_advisory_and_dispositions_visible(self):
        self.init()
        self.write("app.py", "value = 2\n")
        _, review, path = self.completed_review()
        for number, status in enumerate(("open", "fixed", "rejected", "unvalidated")):
            finding = self.finding(status, "critical", "none" if status == "unvalidated" else "source")
            finding["id"] = "SEC-%d" % number
            review["findings"].append(finding)
        scan.write_json(path, review)
        result = self.cli("report", "--run", "case", "--review", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads(result.stdout)
        self.assertEqual(metadata["status"], "incomplete")
        report = Path(metadata["report"]).read_text()
        self.assertIn("critical / open: 1", report)
        self.assertIn("critical / fixed: 1", report)
        self.assertIn("critical / rejected: 1", report)
        self.assertIn("critical / unvalidated: 1", report)
        self.assertIn("do not block", report)

    def test_incomplete_and_stale_reviews_return_success_not_gate(self):
        self.init()
        self.write("app.py", "value = 2\n")
        _, review, path = self.prepare()
        result = self.render(review, path)
        self.assertEqual(result["status"], "incomplete")
        self.write("app.py", "value = 3\n")
        status = self.cli("status", "--run", "case")
        self.assertEqual(status.returncode, 0)
        self.assertEqual(json.loads(status.stdout)["status"], "stale")
        self.assertIn("Unreviewed: app.py", Path(result["report"]).read_text())

    def test_coverage_cannot_include_unlisted_paths_or_silently_skip_files(self):
        self.init()
        self.write("app.py", "value = 2\n")
        snapshot, review, path = self.completed_review()
        review["reviewed"].append("outside.py")
        with self.assertRaises(scan.ScanError):
            scan.validate_review(review, snapshot)
        review["reviewed"] = []
        result = self.render(review, path)
        self.assertEqual(result["status"], "incomplete")

    def test_protected_files_not_opened_including_deleted_baseline(self):
        self.write(".env.production", "never read this\n")
        self.git("add", ".env.production")
        self.git("commit", "-qm", "fixture credential path")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.init("repository")
        original = Path.open

        def guarded(path, *args, **kwargs):
            if path.name == ".env.production":
                self.fail("Credential file opened")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            snapshot, review, path = self.completed_review()
            result = self.render(review, path)
        self.assertEqual(result["status"], "incomplete")
        (self.root / ".env.production").unlink()
        self.assertEqual(scan.file_state(self.root, ".env.production", ["100644 x 0"])["kind"], "protected")

    @unittest.skipIf(os.name == "nt", "Symlink permissions vary on Windows")
    def test_symlink_targets_not_followed_and_output_symlinks_rejected(self):
        os.symlink("/does/not/exist", self.root / "linked.py")
        self.init("repository")
        snapshot, review, path = self.completed_review()
        row = next(row for row in snapshot["files"] if row["path"] == "linked.py")
        self.assertEqual(row["worktree"]["kind"], "symlink")
        self.assertEqual(self.render(review, path)["status"], "incomplete")
        with tempfile.TemporaryDirectory() as other:
            os.symlink(other, self.root / ".ado/security/escape")
            with self.assertRaises(scan.ScanError):
                scan.run_path(self.root, "escape")

    def test_report_local_timestamp_redaction_and_no_staging(self):
        self.init()
        self.write("app.py", "value = 2\n")
        _, review, path = self.completed_review()
        review["limitations"] = ["AZURE_DEVOPS_PAT=fixture-only Bearer fixture-only ghp_fixtureonly"]
        before = self.git("diff", "--cached", "--name-only")
        result = self.render(review, path)
        report = Path(result["report"])
        self.assertRegex(report.name, r"ado-security-review-case-[a-f0-9]{8}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.md$")
        self.assertEqual(report.parent, self.root / "docs")
        self.assertNotIn("fixture-only", report.read_text())
        self.assertNotIn("ghp_fixtureonly", report.read_text())
        self.assertEqual(before, self.git("diff", "--cached", "--name-only"))

    def test_confirmed_findings_require_validation(self):
        self.init()
        self.write("app.py", "value = 2\n")
        snapshot, review, _ = self.completed_review()
        review["findings"] = [self.finding(method="none")]
        with self.assertRaises(scan.ScanError):
            scan.validate_review(review, snapshot)

    def test_report_only_reads_the_returned_run_review_path(self):
        self.init()
        self.write(".env", "credential file must not be opened")
        with self.assertRaises(scan.ScanError):
            scan.report(self.root, "case", self.root / ".env")

    @unittest.skipIf(os.name == "nt", "Symlink permissions vary on Windows")
    def test_artifact_symlinks_cannot_read_or_overwrite_other_files(self):
        self.init()
        self.write("sentinel.json", '{"preserve": true}')
        link = self.root / ".ado/security/outputs.json"
        os.symlink(self.root / "sentinel.json", link)
        with self.assertRaises(scan.ScanError):
            scan.read_json(link)
        with self.assertRaises(scan.ScanError):
            scan.write_json(link, [])
        self.assertEqual((self.root / "sentinel.json").read_text(), '{"preserve": true}')

    def test_error_is_operational_not_security_gate(self):
        result = self.cli("init", "--run", "bad", "--mode", "changes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a push gate", result.stderr)

    def test_binary_and_submodule_are_explicit_gaps(self):
        (self.root / "image.bin").write_bytes(b"abc\0def")
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.base + ",submodule")
        self.init("repository")
        _, review, path = self.completed_review()
        result = self.render(review, path)
        contents = Path(result["report"]).read_text()
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("Binary content", contents)
        self.assertIn("Submodule contents", contents)


if __name__ == "__main__":
    unittest.main()

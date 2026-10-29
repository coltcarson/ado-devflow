"""Check fixture ground truth, not whether an LLM follows the skill."""
import json
from pathlib import Path
import unittest


CASES = json.loads((Path(__file__).with_name("behavior_cases.json")).read_text())["cases"]


class BehaviorFixtureTests(unittest.TestCase):
    def load_case(self, identifier):
        case = next(case for case in CASES if case["id"] == identifier)
        namespace = {}
        # These are the bundled, synthetic in-memory fixtures, not arbitrary source.
        exec(compile(case["files"]["app.py"], "fixture/app.py", "exec"), namespace)
        return namespace

    def test_reachable_bypass_discloses_another_owners_record(self):
        namespace = self.load_case("reachable-authorization-bypass")
        self.assertEqual(namespace["entrypoint"]("alice", 2), "bob-fixture")

    def test_effective_control_rejects_other_owner_and_allows_owner(self):
        namespace = self.load_case("protected-counterexample")
        with self.assertRaises(PermissionError):
            namespace["entrypoint"]("alice", 2)
        self.assertEqual(namespace["entrypoint"]("bob", 2), "bob-fixture")

    def test_uncertain_case_really_has_an_unavailable_control(self):
        case = next(case for case in CASES if case["id"] == "unavailable-external-control")
        self.assertNotIn("deployment_access.py", case["files"])
        # Compile without importing or pretending to validate the absent dependency.
        compile(case["files"]["app.py"], "fixture/app.py", "exec")


if __name__ == "__main__":
    unittest.main()

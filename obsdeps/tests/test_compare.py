"""Compare and explain: source-only changes must be visible."""

from __future__ import annotations

import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, pom


class CompareAndExplain(unittest.TestCase):
    def setUp(self):
        self.api = make_api()
        bundle = Bundle()
        bundle.observation("R1", "obs-parent-a", "2024-01-01T00:00:00Z")
        bundle.observation("R1", "obs-parent-b", "2024-01-05T00:00:00Z")
        bundle.observation("R1", "obs-app", "2024-01-02T00:00:00Z")

        # Child: managed ${release}; explicit 3.0 for a second dependency.
        bundle.add_coordinate_pom(
            "R1", "obs-app", "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0",
                parent=("com.acme", "parent", "1.0"),
                management=[("com.acme", "lib", "${release}")],
                dependencies=[dep("com.acme", "lib", None),
                              dep("com.acme", "direct", "3.0")]),
        )
        # Two parent observations with the SAME release value but different
        # files.  Final version is 1.5 in both cases; the source differs.
        bundle.add_coordinate_pom(
            "R1", "obs-parent-a", "com.acme", "parent", "1.0",
            pom("com.acme", "parent", "1.0", properties={"release": "1.5"}),
        )
        bundle.add_coordinate_pom(
            "R1", "obs-parent-b", "com.acme", "parent", "1.0",
            pom("com.acme", "parent", "1.0",
                properties={"release": "${micro}", "micro": "1.5"}),
        )
        # A dependency POM lives only in obs-app.
        bundle.add_coordinate_pom(
            "R1", "obs-app", "com.acme", "lib", "1.5", pom("com.acme", "lib", "1.5"))
        bundle.add("R1", "obs-app", dep_path("com.acme", "lib", "1.5", "jar"), "lib jar")
        bundle.add("R1", "obs-app", dep_path("com.acme", "lib", "1.5", "jar", "tests"),
                   "lib tests jar")
        self.bundle = bundle
        for key in bundle.observations:
            self.api.register(bundle.manifest(*key))

        self.request_a = {
            "chain": [{"repo_id": "R1", "observation_id": "obs-parent-a"},
                      {"repo_id": "R1", "observation_id": "obs-app"}],
            "coordinate": {"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        }
        self.request_b = {
            "chain": [{"repo_id": "R1", "observation_id": "obs-parent-b"},
                      {"repo_id": "R1", "observation_id": "obs-app"}],
            "coordinate": {"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        }

    def tearDown(self):
        self.api.close()

    def test_compare_reports_source_change_without_version_change(self):
        comparison = self.api.compare(self.request_a, self.request_b)
        self.assertEqual(comparison["status_a"], "resolved")
        self.assertEqual(comparison["status_b"], "resolved")
        self.assertEqual(comparison["summary"]["version_changed"], 0)
        self.assertEqual(comparison["summary"]["source_changed"], 1)
        changed = [row for row in comparison["dependency_changes"]
                   if row["classification"] == "source_changed"]
        self.assertEqual(len(changed), 1)
        row = changed[0]
        self.assertEqual(row["key"], "com.acme:lib:jar:")
        self.assertEqual(row["a"]["version"], "1.5")
        self.assertEqual(row["b"]["version"], "1.5")
        # The ${release} expression is declared in the child in both contexts,
        # but the property definition that gives it its value moved.
        diff = row["source_diff"]
        self.assertIn("properties", diff)
        prop_diff = diff["properties"]
        a_release = next(p for p in prop_diff["a"] if p["name"] == "release")
        b_release = next(p for p in prop_diff["b"] if p["name"] == "release")
        self.assertIn("obs-parent-a", a_release["defined_in"]["locator"])
        self.assertIn("obs-parent-b", b_release["defined_in"]["locator"])
        self.assertEqual(a_release["raw"], "1.5")
        self.assertEqual(b_release["raw"], "${micro}")
        b_micro = next(p for p in prop_diff["b"] if p["name"] == "micro")
        self.assertEqual(b_micro["resolved_through"], ["release", "micro"])
        # Parent chain and context both changed.
        self.assertTrue(comparison["parent_chain_changes"]["changed"])
        self.assertTrue(comparison["context_changes"]["chain_changed"])
        self.assertFalse(comparison["context_changes"]["same_context"])

    def test_explain_narrates_the_selected_sources(self):
        explanation = self.api.explain(
            **self.request_b, dependency={"groupId": "com.acme", "artifactId": "lib"})
        self.assertEqual(explanation["status"], "resolved")
        body = explanation["explanation"]
        self.assertEqual(body["final_version"], "1.5")
        self.assertEqual(body["version_source"], "managed")
        steps = body["steps"]
        facts = [step["fact"] for step in steps]
        self.assertTrue(any("parent edge" in fact for fact in facts))
        managed = next(step for step in steps if "effective dependencyManagement" in step["fact"])
        self.assertIn("obs-app", managed["managed_from"]["locator"])
        self.assertEqual(managed["resolved"], "1.5")
        props = {p["name"]: p for p in managed["properties"]}
        self.assertEqual(props["release"]["raw"], "${micro}")
        self.assertEqual(props["release"]["value"], "1.5")
        self.assertIn("obs-parent-b", props["micro"]["defined_in"]["locator"])
        files = next(step for step in steps if step["fact"].startswith("artifact files"))
        roles = {item["role"]: item for item in files["files"]}
        self.assertEqual(roles["dependency-pom"]["path"], "com/acme/lib/1.5/lib-1.5.pom")
        self.assertEqual(roles["dependency-artifact"]["path"],
                         "com/acme/lib/1.5/lib-1.5.jar")
        self.assertEqual(roles["dependency-artifact"]["sha256"],
                         self.api.store.read_file("R1", "obs-app",
                                                  "com/acme/lib/1.5/lib-1.5.jar").sha256)

    def test_explain_unknown_dependency_is_rejected(self):
        with self.assertRaises(Exception) as raised:
            self.api.explain(**self.request_a,
                             dependency={"groupId": "com.acme", "artifactId": "nope"})
        self.assertIn("dependency-not-in-result", str(raised.exception))

    def test_direct_dependency_and_managed_dependency_are_classified(self):
        comparison = self.api.compare(self.request_a, self.request_b)
        by_key = {row["key"]: row for row in comparison["dependency_changes"]}
        self.assertEqual(by_key["com.acme:direct:jar:"]["classification"], "unchanged")

    def test_comparing_a_context_with_itself_reports_no_changes(self):
        comparison = self.api.compare(self.request_a, self.request_a)
        self.assertTrue(comparison["context_changes"]["same_context"])
        self.assertEqual(comparison["summary"]["unchanged"], 2)

    def test_added_and_removed_dependencies(self):
        result_a = self.api.resolve(**self.request_a)
        result_b = self.api.resolve(**self.request_b)
        # Drop the explicit dependency from B by resolving a different child
        # file is heavy; instead compare against a hand-built empty side.
        empty = dict(result_b)
        empty["dependencies"] = []
        empty["status"] = "resolved"
        comparison = self.api.compare_resolved(result_a, empty)
        self.assertEqual(comparison["summary"]["removed"], 2)
        self.assertTrue(all(row["classification"] == "removed"
                            for row in comparison["dependency_changes"]))


if __name__ == "__main__":
    unittest.main()

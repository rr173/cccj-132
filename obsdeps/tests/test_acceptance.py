"""The acceptance scenario from the task brief, end to end."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, metadata, pom

CHILD = "com.acme:child:1.0"


def build_bundle() -> Bundle:
    """Three observations: child alone, parent in R1, another parent in R0."""
    bundle = Bundle()
    bundle.observation("R1", "obs-child-early", "2024-01-01T00:00:00Z")
    bundle.observation("R1", "obs-parents-later", "2024-01-05T00:00:00Z")
    bundle.observation("R0", "obs-r0", "2024-01-04T00:00:00Z")

    child = pom(
        "com.acme", "child", "1.0",
        parent=("com.acme", "parent", "1.0"),
        properties={"release": "2.0"},
        management=[("com.acme", "lib", "${release}")],
        dependencies=[
            dep("com.acme", "lib", None),
            dep("com.acme", "other", None),
            dep("com.acme", "direct", "3.0"),
        ],
    )
    bundle.add_coordinate_pom("R1", "obs-child-early", "com.acme", "child", "1.0", child)

    parent_r1 = pom(
        "com.acme", "parent", "1.0",
        properties={"release": "1.0", "uncovered": "5.0"},
        management=[("com.acme", "other", "${uncovered}")],
    )
    bundle.add_coordinate_pom("R1", "obs-parents-later", "com.acme", "parent", "1.0", parent_r1)

    parent_r0 = pom(
        "com.acme", "parent", "1.0",
        properties={"release": "9.0", "uncovered": "7.0"},
        management=[("com.acme", "other", "${uncovered}")],
    )
    bundle.add_coordinate_pom("R0", "obs-r0", "com.acme", "parent", "1.0", parent_r0)

    for repo, observation, versions in (
        ("R1", "obs-parents-later", (("lib", "2.0"), ("direct", "3.0"), ("other", "5.0"))),
        ("R0", "obs-r0", (("lib", "9.0"), ("other", "7.0"))),
    ):
        for artifact, version in versions:
            bundle.add_coordinate_pom(
                repo, observation, "com.acme", artifact, version,
                pom("com.acme", artifact, version),
            )
            bundle.add(repo, observation, dep_path("com.acme", artifact, version, "jar"),
                       f"jar bytes for {artifact}-{version}")
    return bundle


def chain(*entries):
    return [{"repo_id": repo, "observation_id": obs} for repo, obs in entries]


class AcceptanceScenario(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = build_bundle()
        self.api = make_api()
        for key in self.bundle.observations:
            self.api.register(self.bundle.manifest(*key))
        self.coord = {"groupId": "com.acme", "artifactId": "child", "version": "1.0"}

    def tearDown(self) -> None:
        self.api.close()

    def _versions(self, result):
        return {
            entry["key"]["key"]: entry["version"]["resolved"]
            for entry in result["dependencies"]
        }

    # -- A ------------------------------------------------------------------
    def test_context_a_child_alone_is_blocked_and_keeps_known_facts(self):
        result = self.api.resolve(chain=chain(("R1", "obs-child-early")), coordinate=self.coord)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["dependencies"], [])
        self.assertTrue(any(m["code"] == "pom-missing" for m in result["missing"]))
        # Known facts are still delivered, without claiming they are effective.
        declared = result["declared_dependencies"]
        self.assertEqual(len(declared), 3)
        self.assertTrue(all(item["status"] == "not-evaluated" for item in declared))
        management = result["declared_management"]
        self.assertEqual(len(management), 1)
        self.assertEqual(management[0]["raw"]["version"], "${release}")
        self.assertTrue(all(item["status"] == "not-evaluated" for item in management))

    # -- B ------------------------------------------------------------------
    def test_context_b_with_parent_from_r1(self):
        result = self.api.resolve(
            chain=chain(("R1", "obs-child-early"), ("R1", "obs-parents-later")),
            coordinate=self.coord,
        )
        self.assertEqual(result["status"], "resolved")
        versions = self._versions(result)
        self.assertEqual(versions["com.acme:lib:jar:"], "2.0")      # child override wins
        self.assertEqual(versions["com.acme:other:jar:"], "5.0")    # parent property
        self.assertEqual(versions["com.acme:direct:jar:"], "3.0")   # explicit, not managed
        lib = next(d for d in result["dependencies"] if d["key"]["key"] == "com.acme:lib:jar:")
        self.assertEqual(lib["version"]["source"], "managed")
        evidence = lib["version"]["evidence"]
        self.assertEqual(evidence["raw_version"], "${release}")
        self.assertEqual(evidence["resolved_version"], "2.0")
        self.assertTrue(evidence["managed_from"]["locator"].startswith("R1/obs-child-early/"))
        # Property definition evidence points at the child's override.
        prop = evidence["properties"][0]
        self.assertEqual(prop["name"], "release")
        self.assertEqual(prop["value"], "2.0")
        self.assertIn("obs-child-early", prop["defined_in"]["locator"])

    # -- C ------------------------------------------------------------------
    def test_context_c_r0_first_changes_only_the_uncovered_property(self):
        result_b = self.api.resolve(
            chain=chain(("R1", "obs-child-early"), ("R1", "obs-parents-later")),
            coordinate=self.coord,
        )
        result_c = self.api.resolve(
            chain=chain(("R0", "obs-r0"), ("R1", "obs-child-early"), ("R1", "obs-parents-later")),
            coordinate=self.coord,
        )
        self.assertEqual(result_c["status"], "resolved")
        versions_c = self._versions(result_c)
        self.assertEqual(versions_c["com.acme:lib:jar:"], "2.0")
        self.assertEqual(versions_c["com.acme:other:jar:"], "7.0")
        self.assertEqual(versions_c["com.acme:direct:jar:"], "3.0")

        # The parent edge must name R0 / obs-r0, not fall through to R1.
        edge = result_c["parent_edges"][0]
        self.assertEqual(edge["selected"]["repo_id"], "R0")
        self.assertEqual(edge["selected"]["observation_id"], "obs-r0")
        self.assertEqual(
            [step["repo_id"] for step in edge["search_trail"]],
            ["R0"],
        )

        comparison = self.api.compare_resolved(result_b, result_c)
        self.assertEqual(comparison["summary"]["version_changed"], 1)
        self.assertEqual(comparison["summary"]["unchanged"], 2)
        changed = [row for row in comparison["dependency_changes"]
                   if row["classification"] == "version_changed"]
        self.assertEqual(changed[0]["key"], "com.acme:other:jar:")
        self.assertTrue(comparison["parent_chain_changes"]["changed"])
        other_change = [p for p in comparison["property_changes"] if p["name"] == "uncovered"]
        self.assertEqual(len(other_change), 1)
        self.assertIn("obs-r0", other_change[0]["b"]["defined_in"]["locator"])

    # -- re-query -----------------------------------------------------------
    def test_old_contexts_do_not_change_after_new_observations(self):
        chain_a = chain(("R1", "obs-child-early"))
        first_a = self.api.resolve(chain=chain_a, coordinate=self.coord)
        chain_b = chain(("R1", "obs-child-early"), ("R1", "obs-parents-later"))
        first_b = self.api.resolve(chain=chain_b, coordinate=self.coord)

        # A later observation with a completely different world view.
        bundle = Bundle()
        bundle.observation("R1", "obs-rebuilt", "2024-02-01T00:00:00Z")
        bundle.add_coordinate_pom("R1", "obs-rebuilt", "com.acme", "child", "1.0",
                                  pom("com.acme", "child", "1.0", dependencies=[
                                      ("com.acme", "lib", "99.0")]))
        self.api.register(bundle.manifest("R1", "obs-rebuilt"))

        second_a = self.api.resolve(chain=chain_a, coordinate=self.coord)
        second_b = self.api.resolve(chain=chain_b, coordinate=self.coord)
        self.assertEqual(first_a["status"], "blocked")
        self.assertEqual(second_a["status"], "blocked")
        self.assertEqual(self._versions(first_b), self._versions(second_b))
        self.assertEqual(first_b["subject"]["file"], second_b["subject"]["file"])
        self.assertEqual(first_a["context"], second_a["context"])


if __name__ == "__main__":
    unittest.main()

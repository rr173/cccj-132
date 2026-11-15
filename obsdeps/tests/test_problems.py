"""Refusal and contradiction cases.

Every case here must produce a locatable input problem, not a plausible guess.
"""

from __future__ import annotations

import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, metadata, pom

COORD = {"groupId": "com.acme", "artifactId": "app", "version": "1.0"}


class Base(unittest.TestCase):
    def setUp(self):
        self.api = make_api()
        self._counter = 0

    def tearDown(self):
        self.api.close()

    def observation(self, repo="R1", observed_at=None):
        self._counter += 1
        observation_id = f"obs-{self._counter}"
        self.api.register({
            "observation": {
                "repo_id": repo,
                "observation_id": observation_id,
                "observed_at": observed_at or f"2024-01-{self._counter:02d}T00:00:00Z",
            },
            "files": [],
        }) if False else None
        bundle = Bundle()
        bundle.observation(repo, observation_id, observed_at or f"2024-01-{self._counter:02d}T00:00:00Z")
        self._pending = bundle
        return bundle, repo, observation_id

    def register(self, bundle, repo, observation_id):
        self.api.register(bundle.manifest(repo, observation_id))
        return {"repo_id": repo, "observation_id": observation_id}

    def resolve(self, *chain, coordinate=COORD):
        return self.api.resolve(chain=list(chain), coordinate=coordinate)

    def codes(self, problems):
        return {p["code"] for p in problems}


class ParentChainCases(Base):
    def test_parent_cycle_reports_coordinates_and_source_chain(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        # a's parent is b, b's parent is a
        a = pom("com.acme", "a", "1.0", parent=("com.acme", "b", "1.0"))
        b = pom("com.acme", "b", "1.0", parent=("com.acme", "a", "1.0"))
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "a", "1.0", a)
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "b", "1.0", b)
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}],
            coordinate={"groupId": "com.acme", "artifactId": "a", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "parent-cycle")
        self.assertEqual(problem["details"]["coordinates"],
                         ["com.acme:a:1.0", "com.acme:b:1.0", "com.acme:a:1.0"])
        self.assertEqual(len(problem["details"]["source_chain"]), 3)
        self.assertEqual(problem["details"]["source_chain"][0],
                         problem["details"]["source_chain"][-1])
        self.assertIn("obs/com/acme/a/1.0/a-1.0.pom", problem["details"]["source_chain"][0])

    def test_self_parent_is_a_cycle(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "a", "1.0",
            pom("com.acme", "a", "1.0", parent=("com.acme", "a", "1.0")),
        )
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}],
            coordinate={"groupId": "com.acme", "artifactId": "a", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("parent-cycle", self.codes(result["problems"]))

    def test_first_repository_damaged_pom_does_not_fall_through(self):
        bundle = Bundle()
        bundle.observation("R0", "obs-r0", "2024-01-01T00:00:00Z")
        bundle.observation("R1", "obs-r1", "2024-01-02T00:00:00Z")
        # R0 has a broken parent POM; R1 has a perfectly good one.
        bundle.add_coordinate_pom("R0", "obs-r0", "com.acme", "parent", "1.0",
                                  "<project><parent><groupId>com.acme</groupId>")
        bundle.add_coordinate_pom("R1", "obs-r1", "com.acme", "parent", "1.0",
                                  pom("com.acme", "parent", "1.0", properties={"release": "1.0"}))
        bundle.add_coordinate_pom(
            "R1", "obs-r1", "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0", parent=("com.acme", "parent", "1.0"),
                dependencies=[dep("com.acme", "downstream", "1.0")]),
        )
        self.api.register(bundle.manifest("R0", "obs-r0"))
        self.api.register(bundle.manifest("R1", "obs-r1"))
        result = self.api.resolve(
            chain=[{"repo_id": "R0", "observation_id": "obs-r0"},
                   {"repo_id": "R1", "observation_id": "obs-r1"}],
            coordinate=COORD,
        )
        self.assertEqual(result["status"], "invalid")
        codes = self.codes(result["problems"])
        self.assertIn("parent-pom-invalid", codes)
        # Evidence must say the search stopped at R0 rather than continuing.
        problem = next(p for p in result["problems"] if p["code"] == "parent-pom-invalid")
        self.assertEqual(problem["details"]["selected"]["repo_id"], "R0")
        self.assertEqual(problem["details"]["search_trail"],
                         [{"repo_id": "R0", "observation_id": "obs-r0", "present": True}])
        # No dependency was invented.
        self.assertEqual(result["dependencies"], [])
        self.assertTrue(result["declared_dependencies"])

    def test_missing_parent_is_blocked_not_valid(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0", parent=("com.acme", "parent", "1.0"),
                dependencies=[dep("com.acme", "x", "1.0")]),
        )
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}], coordinate=COORD)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["dependencies"], [])
        missing = next(m for m in result["missing"] if m["code"] == "pom-missing")
        self.assertEqual(missing["details"]["parent_of_coordinate"], "com.acme:app:1.0")

    def test_parent_coordinate_may_use_own_literal_property_but_not_project(self):
        # Allowed: the child's own property supplies the parent's version.
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        child = pom("com.acme", "app", "1.0",
                    parent=("com.acme", "parent", "${parentVersion}"),
                    properties={"parentVersion": "1.0"})
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "app", "1.0", child)
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "parent", "1.0",
                                  pom("com.acme", "parent", "1.0"))
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}], coordinate=COORD)
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["parent_edges"][0]["parent_coordinate"]["version"], "1.0")

        # Refused: the parent's version cannot use project.*.
        bundle2 = Bundle()
        bundle2.observation("R1", "obs2", "2024-02-01T00:00:00Z")
        bad_child = pom("com.acme", "app", "1.0",
                        parent=("com.acme", "parent", "${project.version}"))
        bundle2.add_coordinate_pom("R1", "obs2", "com.acme", "app", "1.0", bad_child)
        self.api.register(bundle2.manifest("R1", "obs2"))
        bad = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs2"}], coordinate=COORD)
        self.assertEqual(bad["status"], "invalid")
        self.assertIn("parent-coordinate-invalid", self.codes(bad["problems"]))
        cause = next(p for p in bad["problems"]
                     if p["code"] == "parent-coordinate-invalid")["details"]["causes"]
        self.assertEqual(cause[0]["code"], "coordinate-project-reference")

    def test_parent_coordinate_cannot_use_parent_supplied_property(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        child = pom("com.acme", "app", "1.0",
                    parent=("com.acme", "parent", "${baseVersion}"))
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "app", "1.0", child)
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "parent", "1.0",
                                  pom("com.acme", "parent", "1.0",
                                      properties={"baseVersion": "1.0"}))
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}], coordinate=COORD)
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "parent-coordinate-invalid")
        self.assertEqual(problem["details"]["causes"][0]["code"], "unknown-property")

    def test_indirect_project_reference_in_parent_coordinate_property_is_refused(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        child = pom(
            "com.acme", "app", "1.0",
            parent=("com.acme", "parent", "${pv}"),
            properties={"pv": "${project.version}"},
        )
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "app", "1.0", child)
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}], coordinate=COORD)
        self.assertEqual(result["status"], "invalid")
        causes = next(p for p in result["problems"]
                      if p["code"] == "parent-coordinate-invalid")["details"]["causes"]
        self.assertEqual(causes[0]["code"], "coordinate-project-reference")

    def test_artifact_id_cannot_be_inherited(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "parent", "1.0",
            pom("com.acme", "parent", "1.0"))
        broken = pom(None, "app", None, parent=("com.acme", "parent", "1.0"))
        broken = broken.replace("  <artifactId>app</artifactId>\n", "")
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "app", "1.0", broken)
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}], coordinate=COORD)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("pom-missing-artifact-id", self.codes(result["problems"]))


class PropertyCases(Base):
    def _project(self, properties, dependencies=(), management=(), observation_id="obs"):
        bundle = Bundle()
        bundle.observation("R1", observation_id, "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", observation_id, "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0", properties=properties,
                dependencies=dependencies, management=management),
        )
        self.api.register(bundle.manifest("R1", observation_id))
        return [{"repo_id": "R1", "observation_id": observation_id}]

    def test_property_cycle_reports_the_actual_chain(self):
        chain = self._project({"a": "${b}", "b": "${c}", "c": "${a}"},
                              dependencies=[dep("com.acme", "x", "${a}")])
        result = self.resolve(*chain)
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "property-cycle")
        self.assertEqual(problem["details"]["chain"], ["a", "b", "c", "a"])
        self.assertEqual(len(problem["details"]["sources"]), 3)

    def test_self_referential_property_is_a_cycle(self):
        chain = self._project({"v": "${v}"}, dependencies=[dep("com.acme", "x", "${v}")])
        result = self.resolve(*chain)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("property-cycle", self.codes(result["problems"]))

    def test_unknown_property_is_reported_with_the_reference(self):
        chain = self._project({}, dependencies=[dep("com.acme", "x", "${nope}")])
        result = self.resolve(*chain)
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "unknown-property")
        self.assertEqual(problem["details"]["reference"], "nope")
        self.assertIn("dependency", problem["details"])

    def test_unknown_property_inside_a_property_names_both(self):
        chain = self._project({"v": "${missing.deep}"},
                              dependencies=[dep("com.acme", "x", "${v}")])
        result = self.resolve(*chain)
        problem = next(p for p in result["problems"] if p["code"] == "unknown-property")
        self.assertEqual(problem["details"]["reference"], "missing.deep")
        self.assertEqual(problem["details"]["context"], "property v")

    def test_unsupported_project_expression_is_refused(self):
        chain = self._project({}, dependencies=[dep("com.acme", "x", "${project.buildDir}")])
        result = self.resolve(*chain)
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "unsupported-expression")
        self.assertEqual(problem["details"]["reference"], "project.buildDir")

    def test_environment_expression_is_refused(self):
        chain = self._project({"v": "${env.HOME}"},
                              dependencies=[dep("com.acme", "x", "${v}")])
        result = self.resolve(*chain)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("unsupported-expression", self.codes(result["problems"]))

    def test_leftover_expression_is_not_treated_as_a_version(self):
        # A value that resolves to text still containing an expression can only
        # happen through unsupported syntax; it must never become a version.
        chain = self._project({"v": "1.0-${nope"}, observation_id="obs-leftover-a")
        result = self.resolve(*chain)
        # The property is not referenced by a dependency, so the project still
        # resolves; but if referenced it must fail.
        chain2 = self._project({"v": "1.0-${nope"},
                               dependencies=[dep("com.acme", "x", "${v}")],
                               observation_id="obs-leftover-b")
        result2 = self.resolve(*chain2)
        self.assertEqual(result2["status"], "invalid")
        codes = self.codes(result2["problems"])
        self.assertTrue(codes & {"unknown-property", "unresolved-expression"})

    def test_project_group_and_version_are_usable_in_dependency_versions(self):
        chain = self._project({}, dependencies=[
            dep("com.acme", "x", "${project.version}"),
            dep("com.acme", "y", "${pom.groupId}.suffix"),
        ])
        result = self.resolve(*chain)
        self.assertEqual(result["status"], "resolved")
        versions = {d["key"]["key"]: d["version"]["resolved"] for d in result["dependencies"]}
        self.assertEqual(versions["com.acme:x:jar:"], "1.0")
        self.assertEqual(versions["com.acme:y:jar:"], "com.acme.suffix")

    def test_duplicate_property_in_one_pom_is_an_input_problem(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        raw = pom("com.acme", "app", "1.0", properties={"v": "1"},
                  extra="  <properties><v>2</v></properties>")
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "app", "1.0", raw)
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.resolve({"repo_id": "R1", "observation_id": "obs"})
        self.assertEqual(result["status"], "invalid")
        self.assertIn("duplicate-property", self.codes(result["problems"]))


class ManagementCases(Base):
    def _project(self, management, dependencies, properties=None, parents=()):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        for parent in parents:
            bundle.add_coordinate_pom("R1", "obs", "com.acme", parent[0], "1.0", parent[1])
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0", properties=properties,
                management=management, dependencies=dependencies),
        )
        self.api.register(bundle.manifest("R1", "obs"))
        return {"repo_id": "R1", "observation_id": "obs"}

    def test_same_key_in_child_overrides_parent(self):
        parent = pom("com.acme", "parent", "1.0",
                     management=[("com.acme", "x", "1.0")])
        child = pom("com.acme", "app", "1.0",
                    parent=("com.acme", "parent", "1.0"),
                    management=[("com.acme", "x", "2.0")],
                    dependencies=[dep("com.acme", "x", None)])
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "parent", "1.0", parent)
        bundle.add_coordinate_pom("R1", "obs", "com.acme", "app", "1.0", child)
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.resolve({"repo_id": "R1", "observation_id": "obs"})
        entry = next(d for d in result["dependencies"] if d["key"]["key"] == "com.acme:x:jar:")
        self.assertEqual(entry["version"]["resolved"], "2.0")
        overrides = entry["version"]["evidence"]["overrides"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["version"], "1.0")
        self.assertEqual(overrides[0]["declaring_project"], "com.acme:parent:1.0")

    def test_key_differs_by_type_or_classifier(self):
        chain = self._project(
            management=[
                dep("com.acme", "x", "1.0"),
                dep("com.acme", "x", "2.0", type="test-jar"),
                dep("com.acme", "x", "3.0", classifier="sources"),
            ],
            dependencies=[
                dep("com.acme", "x", None),
                dep("com.acme", "x", None, type="test-jar"),
                dep("com.acme", "x", None, classifier="sources"),
                dep("com.acme", "x", None, type="test-jar", classifier="sources"),
            ],
        )
        result = self.resolve(chain)
        versions = {d["key"]["key"]: d["version"]["resolved"] for d in result["dependencies"]}
        self.assertEqual(versions["com.acme:x:jar:"], "1.0")
        self.assertEqual(versions["com.acme:x:test-jar:"], "2.0")
        self.assertEqual(versions["com.acme:x:jar:sources"], "3.0")
        self.assertIsNone(versions["com.acme:x:test-jar:sources"])

    def test_duplicate_management_key_in_one_pom_is_refused(self):
        chain = self._project(
            management=[dep("com.acme", "x", "1.0"), dep("com.acme", "x", "2.0")],
            dependencies=[dep("com.acme", "x", None)],
        )
        result = self.resolve(chain)
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "duplicate-management-entry")
        self.assertEqual(problem["details"]["key"], "com.acme:x:jar:")

    def test_duplicate_direct_dependency_is_refused(self):
        chain = self._project(
            management=[], dependencies=[dep("com.acme", "x", "1.0"), dep("com.acme", "x", "2.0")])
        result = self.resolve(chain)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(len(result["dependencies"]), 1)
        problem = next(p for p in result["problems"] if p["code"] == "duplicate-direct-dependency")
        self.assertEqual(problem["details"]["key"], "com.acme:x:jar:")

    def test_bom_import_is_explicitly_unsupported(self):
        chain = self._project(
            management=[dep("com.acme", "bom", "1.0", scope="import", type="pom")],
            dependencies=[dep("com.acme", "x", None)],
        )
        result = self.resolve(chain)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(u["code"] == "unsupported-bom-import"
                            for u in result["unsupported"]))
        self.assertEqual(len(result["management"]["excluded"]), 1)
        self.assertNotIn("com.acme:bom:pom:",
                         {e["key"]["key"] for e in result["management"]["entries"]})
        dep_entry = result["dependencies"][0]
        self.assertEqual(dep_entry["status"], "unresolved")
        self.assertIsNone(dep_entry["version"]["resolved"])

    def test_unsupported_pom_nodes_are_reported_not_silently_dropped(self):
        chain = self._project(
            management=[], dependencies=[],
            properties={},
        )
        bundle = Bundle()
        bundle.observation("R1", "obs-profiles", "2024-01-02T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs-profiles", "com.acme", "app", "2.0",
            pom("com.acme", "app", "2.0", extra="  <profiles><profile/></profiles>\n"
                                            "  <build><plugins/></build>\n"
                                            "  <relocation><groupId>x</groupId></relocation>"),
        )
        self.api.register(bundle.manifest("R1", "obs-profiles"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs-profiles"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "2.0"},
        )
        nodes = {u["details"].get("node") for u in result["unsupported"]
                 if u["code"] == "unsupported-node"}
        self.assertEqual(nodes, {"profiles", "build", "relocation"})
        self.assertEqual(result["status"], "partial")
        impact = {u["details"]["node"]: u["details"]["affects_resolution"]
                  for u in result["unsupported"] if u["code"] == "unsupported-node"}
        self.assertTrue(impact["profiles"])
        self.assertTrue(impact["relocation"])
        self.assertFalse(impact["build"])


if __name__ == "__main__":
    unittest.main()


class DeepChainCases(Base):
    def test_three_level_chain_inherits_management_with_child_override(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        grand = pom("com.acme", "grand", "1.0",
                    properties={"v": "1"},
                    management=[("com.acme", "lib", "${v}"), ("com.acme", "keep", "8")])
        middle = pom("com.acme", "middle", "1.0", parent=("com.acme", "grand", "1.0"),
                     properties={"v": "2"},
                     management=[("com.acme", "lib", "${v}")])
        child = pom("com.acme", "app", "1.0", parent=("com.acme", "middle", "1.0"),
                    dependencies=[dep("com.acme", "lib", None),
                                  dep("com.acme", "keep", None)])
        for group, artifact, version, content in (
            ("com.acme", "grand", "1.0", grand),
            ("com.acme", "middle", "1.0", middle),
            ("com.acme", "app", "1.0", child),
        ):
            bundle.add_coordinate_pom("R1", "obs", group, artifact, version, content)
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.resolve({"repo_id": "R1", "observation_id": "obs"})
        versions = {d["key"]["key"]: d["version"]["resolved"] for d in result["dependencies"]}
        self.assertEqual(versions["com.acme:lib:jar:"], "2")
        self.assertEqual(versions["com.acme:keep:jar:"], "8")
        self.assertEqual(len(result["parent_chain"]), 3)
        # $ {v} is defined in both grand and middle; the version value comes
        # from the middle POM's override.
        lib = next(d for d in result["dependencies"] if d["key"]["key"] == "com.acme:lib:jar:")
        prop = lib["version"]["evidence"]["properties"][0]
        self.assertIn("obs/com/acme/middle/1.0/middle-1.0.pom",
                      prop["defined_in"]["locator"])
        overrides = lib["version"]["evidence"]["overrides"]
        self.assertTrue(any("grand" in entry["declared_in"]["path"] for entry in overrides))

    def test_parent_cycle_through_three_files_reports_all(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        for name, parent in (("a", "b"), ("b", "c"), ("c", "a")):
            bundle.add_coordinate_pom(
                "R1", "obs", "com.acme", name, "1.0",
                pom("com.acme", name, "1.0", parent=("com.acme", parent, "1.0")))
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"}],
            coordinate={"groupId": "com.acme", "artifactId": "a", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "parent-cycle")
        self.assertEqual(problem["details"]["coordinates"],
                         ["com.acme:a:1.0", "com.acme:b:1.0", "com.acme:c:1.0",
                          "com.acme:a:1.0"])
        self.assertEqual(len(problem["details"]["source_chain"]), 4)
        self.assertEqual(len(problem["details"]["edges"]), 3)

    def test_project_ref_inside_property_used_for_own_version_is_refused(self):
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "app", "1.0",
            pom("com.acme", "app", "${v}", properties={"v": "${project.version}"}))
        self.api.register(bundle.manifest("R1", "obs"))
        result = self.resolve({"repo_id": "R1", "observation_id": "obs"})
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "coordinate-invalid")
        self.assertEqual(problem["details"]["field"], "version")
        self.assertEqual(problem["details"]["causes"][0]["code"],
                         "coordinate-project-reference")

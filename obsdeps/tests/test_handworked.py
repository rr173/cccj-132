"""Hand-worked small example: properties, management keys and source chains.

Everything in this file is written out by hand so the expected values can be
checked on paper.  It is deliberately *not* produced by the implementation.
"""

from __future__ import annotations

import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, pom


class HandWorkedExample(unittest.TestCase):
    """Parent + child POM with three dependencies.

    Parent (com.acme:base:1.0)::

        <properties>
          <release>1.0</release>          <!-- overridden by the child -->
          <managed>4.2</managed>          <!-- used by the parent's management -->
          <dotty>a.b</dotty>              <!-- used inside another property -->
        </properties>
        <dependencyManagement>
          <dependency> com.acme:lib  version ${release} </dependency>
          <dependency> com.acme:lib   type=jar classifier=sources version ${managed} </dependency>
          <dependency> com.acme:tool  version ${dotty} </dependency>
          <dependency> com.acme:explicit version 9.9 </dependency>
        </dependencyManagement>

    Child (com.acme:app:1.0) overrides ``release`` to 2.0 and declares::

        com.acme:lib                     (no version)  -> 2.0   from management
        com.acme:lib type=jar cl=sources (no version)  -> 4.2   from management
        com.acme:explicit                version 1.0   -> 1.0   direct wins
        com.acme:orphan                  (no version)  -> unresolved

    The child does not inherit ``com.acme:tool`` because direct dependencies are
    only the child's own declarations.
    """

    PARENT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>base</artifactId>
  <version>1.0</version>
  <properties>
    <release>1.0</release>
    <managed>4.2</managed>
    <dotty>a.b</dotty>
  </properties>
  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>com.acme</groupId>
        <artifactId>lib</artifactId>
        <version>${release}</version>
      </dependency>
      <dependency>
        <groupId>com.acme</groupId>
        <artifactId>lib</artifactId>
        <type>jar</type>
        <classifier>sources</classifier>
        <version>${managed}</version>
      </dependency>
      <dependency>
        <groupId>com.acme</groupId>
        <artifactId>tool</artifactId>
        <version>${dotty}</version>
      </dependency>
      <dependency>
        <groupId>com.acme</groupId>
        <artifactId>explicit</artifactId>
        <version>9.9</version>
      </dependency>
    </dependencies>
  </dependencyManagement>
</project>
"""

    CHILD_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.acme</groupId>
    <artifactId>base</artifactId>
    <version>1.0</version>
  </parent>
  <artifactId>app</artifactId>
  <properties>
    <release>2.0</release>
  </properties>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>lib</artifactId>
    </dependency>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>lib</artifactId>
      <type>jar</type>
      <classifier>sources</classifier>
    </dependency>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>explicit</artifactId>
      <version>1.0</version>
    </dependency>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>orphan</artifactId>
    </dependency>
  </dependencies>
</project>
"""

    def setUp(self):
        self.api = make_api()
        bundle = Bundle()
        bundle.observation("R1", "obs-base", "2024-03-01T00:00:00Z")
        bundle.observation("R1", "obs-app", "2024-03-02T00:00:00Z")
        bundle.add_coordinate_pom("R1", "obs-base", "com.acme", "base", "1.0", self.PARENT_POM)
        bundle.add_coordinate_pom("R1", "obs-app", "com.acme", "app", "1.0", self.CHILD_POM)
        for group, artifact, version in (
            ("com.acme", "lib", "2.0"), ("com.acme", "lib", "4.2"),
            ("com.acme", "explicit", "1.0"),
        ):
            bundle.add_coordinate_pom("R1", "obs-app", group, artifact, version,
                                      pom(group, artifact, version))
        self.api.register(bundle.manifest("R1", "obs-base"))
        self.api.register(bundle.manifest("R1", "obs-app"))
        self.result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs-base"},
                   {"repo_id": "R1", "observation_id": "obs-app"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )

    def tearDown(self):
        self.api.close()

    def test_effective_versions_are_the_hand_computed_ones(self):
        versions = {
            (d["key"]["groupId"], d["key"]["artifactId"],
             d["key"]["type"], d["key"]["classifier"]): d["version"]["resolved"]
            for d in self.result["dependencies"]
        }
        self.assertEqual(versions[("com.acme", "lib", "jar", "")], "2.0")
        self.assertEqual(versions[("com.acme", "lib", "jar", "sources")], "4.2")
        self.assertEqual(versions[("com.acme", "explicit", "jar", "")], "1.0")
        self.assertIsNone(versions[("com.acme", "orphan", "jar", "")])

    def test_child_inherits_group_and_version_from_parent(self):
        subject = self.result["subject"]
        self.assertEqual(subject["coordinate"],
                         {"groupId": "com.acme", "artifactId": "app", "version": "1.0"})
        self.assertTrue(subject["groupId_from_parent"])
        self.assertTrue(subject["version_from_parent"])

    def test_management_key_includes_type_and_classifier(self):
        entries = {e["key"]["key"]: e for e in self.result["management"]["entries"]}
        self.assertIn("com.acme:lib:jar:", entries)
        self.assertIn("com.acme:lib:jar:sources", entries)
        self.assertEqual(entries["com.acme:lib:jar:"]["version"], "2.0")
        self.assertEqual(entries["com.acme:lib:jar:sources"]["version"], "4.2")
        # The inherited entry for tool exists in the table but is not a direct
        # dependency of the child.
        self.assertIn("com.acme:tool:jar:", entries)
        keys = {d["key"]["key"] for d in self.result["dependencies"]}
        self.assertNotIn("com.acme:tool:jar:", keys)

    def test_managed_version_evidence_names_declaration_and_property(self):
        lib = next(d for d in self.result["dependencies"]
                   if d["key"]["key"] == "com.acme:lib:jar:")
        evidence = lib["version"]["evidence"]
        self.assertEqual(lib["version"]["source"], "managed")
        self.assertEqual(evidence["raw_version"], "${release}")
        # The management declaration itself lives in the parent ...
        self.assertTrue(evidence["managed_from"]["locator"].startswith("R1/obs-base/"))
        # ... while the value came from the child's override.
        prop = evidence["properties"][0]
        self.assertEqual(prop["name"], "release")
        self.assertEqual(prop["value"], "2.0")
        self.assertTrue(prop["defined_in"]["locator"].startswith("R1/obs-app/"))
        self.assertTrue(prop["defined_in"]["locator"].endswith(":11"))

    def test_classifier_variant_uses_its_own_management_entry(self):
        sources = next(d for d in self.result["dependencies"]
                       if d["key"]["key"] == "com.acme:lib:jar:sources")
        self.assertEqual(sources["version"]["source"], "managed")
        evidence = sources["version"]["evidence"]
        self.assertEqual(evidence["raw_version"], "${managed}")
        prop = evidence["properties"][0]
        self.assertEqual(prop["name"], "managed")
        self.assertEqual(prop["value"], "4.2")
        # Defined in the parent because the child did not override it.
        self.assertTrue(prop["defined_in"]["locator"].startswith("R1/obs-base/"))

    def test_direct_version_never_consults_management(self):
        explicit = next(d for d in self.result["dependencies"]
                        if d["key"]["key"] == "com.acme:explicit:jar:")
        self.assertEqual(explicit["version"]["source"], "direct")
        self.assertEqual(explicit["version"]["resolved"], "1.0")
        self.assertNotIn("managed_from", explicit["version"]["evidence"])

    def test_unresolved_dependency_is_not_an_empty_dependency(self):
        orphan = next(d for d in self.result["dependencies"]
                      if d["key"]["key"] == "com.acme:orphan:jar:")
        self.assertEqual(orphan["status"], "unresolved")
        self.assertIsNone(orphan["version"]["resolved"])
        self.assertEqual(orphan["version"]["source"], "unresolved")
        self.assertTrue(any(m["code"] == "dependency-version-unresolved"
                            for m in orphan["missing"]))
        self.assertEqual(self.result["status"], "partial")

    def test_property_lineage_shows_the_override(self):
        lineage = self.result["properties"]["lineage"]["release"]
        statuses = [entry["status"] for entry in lineage]
        # The parent's own definition (effective in the parent), then the
        # point where the child overrode it, then the child's effective value.
        self.assertEqual(statuses, ["effective", "overridden", "effective"])
        self.assertIn("obs-base", lineage[0]["defined_in"]["locator"])
        self.assertEqual(lineage[1]["overridden_by_project"], "com.acme:app:1.0")
        self.assertIn("obs-app", lineage[1]["overridden_by"]["locator"])
        self.assertIn("obs-app", lineage[2]["defined_in"]["locator"])
        # The parent's resolved value is not rewritten by the child override.
        self.assertEqual(lineage[0]["raw"], "1.0")
        # managed and dotty are only defined once (in the parent) so they have
        # no lineage conflict and are absent from the lineage map.
        self.assertNotIn("managed", self.result["properties"]["lineage"])

    def test_parent_edge_records_repository_observation_and_digest(self):
        edge = self.result["parent_edges"][0]
        self.assertEqual(edge["declared_parent"],
                         {"groupId": "com.acme", "artifactId": "base", "version": "1.0"})
        self.assertEqual(edge["selected"]["repo_id"], "R1")
        self.assertEqual(edge["selected"]["observation_id"], "obs-base")
        self.assertEqual(edge["selected"]["path"], "com/acme/base/1.0/base-1.0.pom")
        self.assertRegex(edge["selected"]["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()


class ParentIndependence(unittest.TestCase):
    """A child's override must not rewrite the parent's own resolution."""

    def test_parent_and_child_resolve_differently_in_the_same_context(self):
        from support import make_api
        from fixtures import Bundle, dep

        api = make_api()
        bundle = Bundle()
        bundle.observation("R1", "obs", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "p", "1.0",
            pom("com.acme", "p", "1.0", properties={"v": "1"},
                management=[("com.acme", "lib", "${v}")],
                dependencies=[dep("com.acme", "lib", None)]),
        )
        bundle.add_coordinate_pom(
            "R1", "obs", "com.acme", "app", "1.0",
            pom("com.acme", "app", "1.0", parent=("com.acme", "p", "1.0"),
                properties={"v": "9"},
                dependencies=[dep("com.acme", "lib", None)]),
        )
        api.register(bundle.manifest("R1", "obs"))
        chain = [{"repo_id": "R1", "observation_id": "obs"}]
        parent = api.resolve(chain=chain,
                             coordinate={"groupId": "com.acme", "artifactId": "p",
                                         "version": "1.0"})
        child = api.resolve(chain=chain,
                            coordinate={"groupId": "com.acme", "artifactId": "app",
                                        "version": "1.0"})
        self.assertEqual(parent["dependencies"][0]["version"]["resolved"], "1")
        self.assertEqual(child["dependencies"][0]["version"]["resolved"], "9")
        # Re-resolving the parent after the child context ran is unchanged.
        again = api.resolve(chain=chain,
                            coordinate={"groupId": "com.acme", "artifactId": "p",
                                        "version": "1.0"})
        self.assertEqual(again["dependencies"], parent["dependencies"])
        api.close()


if __name__ == "__main__":
    unittest.main()

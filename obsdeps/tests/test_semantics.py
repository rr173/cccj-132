"""Less common semantics: namespaces, explicit root files, force context."""

from __future__ import annotations

import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, pom


class Namespaces(unittest.TestCase):
    def setUp(self):
        self.api = make_api()

    def tearDown(self):
        self.api.close()

    def test_real_namespace_controls_element_matching(self):
        # A prefixed Maven namespace is understood by URI, regardless of prefix.
        prefixed = """<?xml version="1.0" encoding="UTF-8"?>
<m:project xmlns:m="http://maven.apache.org/POM/4.0.0">
  <m:modelVersion>4.0.0</m:modelVersion>
  <m:groupId>com.acme</m:groupId>
  <m:artifactId>app</m:artifactId>
  <m:version>1.0</m:version>
  <m:dependencies>
    <m:dependency>
      <m:groupId>com.acme</m:groupId>
      <m:artifactId>x</m:artifactId>
      <m:version>2.0</m:version>
    </m:dependency>
  </m:dependencies>
</m:project>
"""
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "o",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "com/acme/app/1.0/app-1.0.pom", "content": prefixed}],
        })
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["dependencies"][0]["key"]["key"], "com.acme:x:jar:")

    def test_foreign_namespace_element_is_unsupported_not_interpreted(self):
        document = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:f="urn:foreign">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>app</artifactId>
  <version>1.0</version>
  <f:dependencies>
    <f:dependency>
      <f:groupId>com.acme</f:groupId>
      <f:artifactId>x</f:artifactId>
      <f:version>1.0</f:version>
    </f:dependency>
  </f:dependencies>
</project>
"""
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "o",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "com/acme/app/1.0/app-1.0.pom", "content": document}],
        })
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        self.assertEqual(result["dependencies"], [])
        self.assertTrue(any(u["code"] == "unsupported-node" for u in result["unsupported"]))

    def test_dtd_is_refused(self):
        document = """<?xml version="1.0"?>
<!DOCTYPE project [ <!ENTITY x "y"> ]>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>app</artifactId>
  <version>1.0</version>
</project>
"""
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "o",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "com/acme/app/1.0/app-1.0.pom", "content": document}],
        })
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("pom-doctype-unsupported", {p["code"] for p in result["problems"]})
        self.assertEqual(result["subject"].get("coordinate_verified"), False)


class ExplicitRoot(unittest.TestCase):
    def setUp(self):
        self.api = make_api()
        bundle = Bundle()
        bundle.observation("R1", "o", "2024-01-01T00:00:00Z")
        bundle.add("R1", "o", "somewhere/odd-name.pom", pom(
            "com.acme", "app", "1.0",
            dependencies=[dep("com.acme", "x", "1.0")],
        ))
        self.api.register(bundle.manifest("R1", "o"))

    def tearDown(self):
        self.api.close()

    def test_explicit_file_root_is_used_verbatim(self):
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            observation={"repo_id": "R1", "observation_id": "o"},
            pom="somewhere/odd-name.pom",
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["subject"]["file"]["path"], "somewhere/odd-name.pom")
        # No coordinate was requested, so the one shown is read from the file.
        self.assertEqual(result["subject"]["coordinate"],
                         {"groupId": "com.acme", "artifactId": "app", "version": "1.0"})
        self.assertEqual(result["dependencies"][0]["key"]["key"], "com.acme:x:jar:")

    def test_root_file_outside_chain_is_refused(self):
        bundle = Bundle()
        bundle.observation("R2", "other", "2024-01-02T00:00:00Z")
        bundle.add("R2", "other", "somewhere/odd-name.pom", pom("com.acme", "app", "1.0"))
        self.api.register(bundle.manifest("R2", "other"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            observation={"repo_id": "R2", "observation_id": "other"},
            pom="somewhere/odd-name.pom",
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("root-file-outside-context", {p["code"] for p in result["problems"]})


class ManagementEdge(unittest.TestCase):
    def setUp(self):
        self.api = make_api()

    def tearDown(self):
        self.api.close()

    def test_management_entry_without_version_cannot_supply_one(self):
        bundle = Bundle()
        bundle.observation("R1", "o", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom("R1", "o", "com.acme", "app", "1.0", pom(
            "com.acme", "app", "1.0",
            management=[("com.acme", "lib", None)],
            dependencies=[dep("com.acme", "lib", None)],
        ))
        self.api.register(bundle.manifest("R1", "o"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("management-version-missing", {p["code"] for p in result["problems"]})
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "unresolved")

    def test_artifact_type_defaults_to_jar_and_is_reported(self):
        bundle = Bundle()
        bundle.observation("R1", "o", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom("R1", "o", "com.acme", "app", "1.0", pom(
            "com.acme", "app", "1.0",
            management=[dep("com.acme", "lib", "2.0")],
            dependencies=[dep("com.acme", "lib", None)],
        ))
        bundle.add("R1", "o", dep_path("com.acme", "lib", "2.0", "jar"), "jar")
        self.api.register(bundle.manifest("R1", "o"))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "o"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        entry = result["dependencies"][0]
        self.assertEqual(entry["key"]["type"], "jar")
        self.assertEqual(entry["key"]["classifier"], "")
        artifact = next(f for f in entry["files"] if f["role"] == "dependency-artifact")
        self.assertEqual(artifact["extension"], "jar")
        self.assertTrue(artifact["observed"])


if __name__ == "__main__":
    unittest.main()

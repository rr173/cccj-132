"""SNAPSHOT selection: metadata is the only source of file names."""

from __future__ import annotations

import json
import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, metadata, pom


def version_path(group, artifact, version):
    return f"{group.replace('.', '/')}/{artifact}/{version}"


class SnapshotSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.api = make_api()

    def tearDown(self) -> None:
        self.api.close()

    def _snapshot_bundle(self, mappings, files=(), *, metadata_group="com.acme"):
        bundle = Bundle()
        bundle.observation("R1", "obs-snap", "2024-01-01T00:00:00Z")
        bundle.add(
            "R1", "obs-snap",
            f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/maven-metadata.xml",
            metadata(metadata_group, "snap", "1.0-SNAPSHOT", mappings,
                     timestamp="20240101.010101", build_number="5"),
        )
        for path, content in files:
            bundle.add("R1", "obs-snap", path, content)
        self.api.register(bundle.manifest("R1", "obs-snap"))
        return bundle

    def _root_bundle(self, dependencies, *, group="com.acme", artifact="app", version="1.0"):
        bundle = Bundle()
        bundle.observation("R2", "obs-root", "2024-01-01T00:00:00Z")
        bundle.add_coordinate_pom("R2", "obs-root", group, artifact, version,
                                  pom(group, artifact, version, dependencies=dependencies))
        self.api.register(bundle.manifest("R2", "obs-root"))
        return {"repo_id": "R2", "observation_id": "obs-root"}

    def _resolve(self, root_observation, subject=("com.acme", "app", "1.0")):
        return self.api.resolve(
            chain=[
                root_observation,
                {"repo_id": "R1", "observation_id": "obs-snap"},
            ],
            coordinate={"groupId": subject[0], "artifactId": subject[1], "version": subject[2]},
        )

    def test_jar_pom_and_tests_jar_come_from_three_different_values(self):
        mappings = [
            ("pom", "", "1.0-20240101.010101-5"),
            ("jar", "", "1.0-20240102.020202-6"),
            ("jar", "tests", "1.0-20240103.030303-7"),
        ]
        pom_name = "snap-1.0-20240101.010101-5.pom"
        jar_name = "snap-1.0-20240102.020202-6.jar"
        tests_name = "snap-1.0-20240103.030303-7-tests.jar"
        self._snapshot_bundle(
            mappings,
            files=[
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/{pom_name}",
                 pom("com.acme", "snap", "1.0-SNAPSHOT")),
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/{jar_name}", "main jar"),
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/{tests_name}", "tests jar"),
            ],
        )
        root = self._root_bundle([
            dep("com.acme", "snap", "1.0-SNAPSHOT"),
            dep("com.acme", "snap", "1.0-SNAPSHOT", classifier="tests", type="jar"),
        ])
        result = self._resolve(root)
        self.assertEqual(result["status"], "resolved")
        dep_entry = next(d for d in result["dependencies"] if d["key"]["classifier"] == "")
        tests_entry = next(d for d in result["dependencies"] if d["key"]["classifier"] == "tests")

        # The POM selection follows its own mapping, never the main jar's.
        pom_file = next(f for f in dep_entry["files"] if f["role"] == "dependency-pom")
        self.assertEqual(pom_file["path"],
                         f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/{pom_name}")
        self.assertEqual(pom_file["snapshot"]["value"], "1.0-20240101.010101-5")
        self.assertEqual(pom_file["snapshot"]["extension"], "pom")
        self.assertEqual(pom_file["snapshot"]["classifier"], "")

        main_file = next(f for f in dep_entry["files"] if f["role"] == "dependency-artifact")
        self.assertEqual(main_file["path"],
                         f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/{jar_name}")
        self.assertEqual(main_file["snapshot"]["value"], "1.0-20240102.020202-6")
        self.assertTrue(main_file["observed"])

        tests_pom = next(f for f in tests_entry["files"] if f["role"] == "dependency-pom")
        tests_artifact = next(f for f in tests_entry["files"] if f["role"] == "dependency-artifact")
        self.assertEqual(tests_artifact["path"],
                         f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/{tests_name}")
        self.assertEqual(tests_artifact["snapshot"]["value"], "1.0-20240103.030303-7")
        self.assertEqual(tests_artifact["snapshot"]["classifier"], "tests")
        # Its POM is chosen by the same metadata entry as the main POM, not by
        # the tests-jar name.
        self.assertEqual(tests_pom["path"], pom_file["path"])
        self.assertNotEqual(tests_artifact["sha256"], main_file["sha256"])
        # Timestamps were present but never used to form a name.
        for item in (pom_file, main_file, tests_artifact):
            self.assertFalse(item["snapshot"]["timestamp_used_for_selection"])

    def test_timestamp_alone_does_not_create_a_mapping(self):
        # Metadata has a timestamp/buildNumber but no snapshotVersions entry for
        # the pom.  Deriving "snap-1.0-20240101.010101-5.pom" is forbidden.
        bundle = Bundle()
        bundle.observation("R1", "obs-snap", "2024-01-01T00:00:00Z")
        base = version_path("com.acme", "snap", "1.0-SNAPSHOT")
        metadata_no_pom = metadata(
            "com.acme", "snap", "1.0-SNAPSHOT", [("jar", "", "1.0-20240102.020202-6")],
            timestamp="20240101.010101", build_number="5")
        bundle.add("R1", "obs-snap", f"{base}/maven-metadata.xml", metadata_no_pom)
        bundle.add("R1", "obs-snap", f"{base}/snap-1.0-20240101.010101-5.pom",
                   pom("com.acme", "snap", "1.0-SNAPSHOT"))
        bundle.add("R1", "obs-snap", f"{base}/snap-1.0-20240102.020202-6.jar", "jar")
        self.api.register(bundle.manifest("R1", "obs-snap"))
        root = self._root_bundle([dep("com.acme", "snap", "1.0-SNAPSHOT")])
        result = self._resolve(root)
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "partial")
        self.assertTrue(any(m["code"] == "snapshot-mapping-missing"
                            for m in entry["missing"]))
        # The renamed file exists but was not used.
        pom_file = next(f for f in entry["files"] if f["role"] == "dependency-pom")
        self.assertFalse(pom_file["observed"])

    def test_missing_mapping_is_reported_not_guessed(self):
        self._snapshot_bundle(
            [("pom", "", "1.0-20240101.010101-5"), ("jar", "", "1.0-20240102.020202-6")],
            files=[
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240101.010101-5.pom",
                 pom("com.acme", "snap", "1.0-SNAPSHOT")),
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240102.020202-6.jar",
                 "jar"),
            ],
        )
        root = self._root_bundle([
            dep("com.acme", "snap", "1.0-SNAPSHOT", classifier="tests", type="jar"),
        ])
        result = self._resolve(root)
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "partial")
        missing = entry["missing"]
        self.assertTrue(any(m["code"] == "snapshot-mapping-missing" for m in missing))
        artifact = next(f for f in entry["files"] if f["role"] == "dependency-artifact")
        self.assertFalse(artifact["observed"])
        self.assertTrue(artifact["expected_path"].endswith(".jar"))
        self.assertEqual(artifact["snapshot"]["available"],
                         [{"extension": "pom", "classifier": "", "value": "1.0-20240101.010101-5"},
                          {"extension": "jar", "classifier": "", "value": "1.0-20240102.020202-6"}])

    def test_duplicate_mapping_conflict_is_an_input_problem(self):
        self._snapshot_bundle(
            [("pom", "", "1.0-20240101.010101-5"),
             ("jar", "", "1.0-20240102.020202-6"),
             ("jar", "", "1.0-20240104.040404-8")],
            files=[
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240101.010101-5.pom",
                 pom("com.acme", "snap", "1.0-SNAPSHOT")),
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240102.020202-6.jar",
                 "jar"),
            ],
        )
        root = self._root_bundle([dep("com.acme", "snap", "1.0-SNAPSHOT")])
        result = self._resolve(root)
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(result["status"], "invalid")
        codes = {p["code"] for p in entry["problems"]}
        self.assertIn("snapshot-mapping-duplicate", codes)
        problem = next(p for p in entry["problems"] if p["code"] == "snapshot-mapping-duplicate")
        self.assertEqual(sorted(problem["details"]["values"]),
                         ["1.0-20240102.020202-6", "1.0-20240104.040404-8"])
        self.assertEqual(len(problem["details"]["lines"]), 2)

    def test_mapping_naming_a_missing_file_is_reported(self):
        self._snapshot_bundle(
            [("pom", "", "1.0-20240101.010101-5"),
             ("jar", "", "1.0-20240102.020202-6")],
            files=[
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240101.010101-5.pom",
                 pom("com.acme", "snap", "1.0-SNAPSHOT")),
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-SNAPSHOT.jar",
                 "timestamp-free jar"),
            ],
        )
        root = self._root_bundle([dep("com.acme", "snap", "1.0-SNAPSHOT")])
        result = self._resolve(root)
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "partial")
        codes = {m["code"] for m in entry["missing"]}
        self.assertIn("snapshot-target-missing", codes)

    def test_metadata_absent_blocks_snapshot_dependency(self):
        bundle = Bundle()
        bundle.observation("R1", "obs-snap", "2024-01-01T00:00:00Z")
        bundle.add("R1", "obs-snap", "placeholder/README.txt", "no metadata here")
        self.api.register(bundle.manifest("R1", "obs-snap"))
        root = self._root_bundle([dep("com.acme", "snap", "1.0-SNAPSHOT")])
        result = self._resolve(root)
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "partial")
        codes = {m["code"] for m in entry["missing"]}
        self.assertIn("snapshot-metadata-missing", codes)

    def test_metadata_coordinate_mismatch_is_a_contradiction(self):
        self._snapshot_bundle(
            [("pom", "", "1.0-20240101.010101-5"), ("jar", "", "1.0-20240102.020202-6")],
            files=[],
            metadata_group="com.other",
        )
        root = self._root_bundle([dep("com.acme", "snap", "1.0-SNAPSHOT")])
        result = self._resolve(root)
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "invalid")
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any(p["code"] == "metadata-coordinate-mismatch"
                            for p in entry["problems"]))

    def test_snapshot_subject_pom_uses_metadata(self):
        self._snapshot_bundle(
            [("pom", "", "1.0-20240101.010101-5"), ("jar", "", "1.0-20240102.020202-6")],
            files=[
                (f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240101.010101-5.pom",
                 pom("com.acme", "snap", "1.0-SNAPSHOT",
                     dependencies=[dep("com.acme", "x", "1.0")])),
            ],
        )
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs-snap"}],
            coordinate={"groupId": "com.acme", "artifactId": "snap", "version": "1.0-SNAPSHOT"},
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["subject"]["file"]["path"],
                         f"{version_path('com.acme', 'snap', '1.0-SNAPSHOT')}/snap-1.0-20240101.010101-5.pom")

    def test_metadata_is_selected_with_the_pom_in_one_observation(self):
        # Metadata in observation X names a value; the file only exists in Y.
        # The mapping's file must come from X, otherwise we would be mixing
        # observations.  It is reported missing rather than taken from Y.
        bundle_x = Bundle()
        bundle_x.observation("R1", "obs-x", "2024-01-01T00:00:00Z")
        base = version_path("com.acme", "snap", "1.0-SNAPSHOT")
        bundle_x.add("R1", "obs-x", f"{base}/maven-metadata.xml",
                     metadata("com.acme", "snap", "1.0-SNAPSHOT",
                              [("pom", "", "1.0-20240101.010101-5"),
                               ("jar", "", "1.0-20240102.020202-6")]))
        self.api.register(bundle_x.manifest("R1", "obs-x"))
        bundle_y = Bundle()
        bundle_y.observation("R1", "obs-y", "2024-01-02T00:00:00Z")
        bundle_y.add("R1", "obs-y", f"{base}/snap-1.0-20240101.010101-5.pom",
                     pom("com.acme", "snap", "1.0-SNAPSHOT"))
        self.api.register(bundle_y.manifest("R1", "obs-y"))
        root = self._root_bundle([dep("com.acme", "snap", "1.0-SNAPSHOT")])
        result = self.api.resolve(
            chain=[root, {"repo_id": "R1", "observation_id": "obs-x"},
                   {"repo_id": "R1", "observation_id": "obs-y"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        entry = result["dependencies"][0]
        self.assertEqual(entry["status"], "partial")
        self.assertIn("snapshot-target-missing", {m["code"] for m in entry["missing"]})
        pom_file = next(f for f in entry["files"] if f["role"] == "dependency-pom")
        self.assertFalse(pom_file["observed"])


if __name__ == "__main__":
    unittest.main()

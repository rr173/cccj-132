"""Observation storage: immutability, ordering independence, digest handling."""

from __future__ import annotations

import copy
import json
import os
import random
import tempfile
import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, metadata, pom


class Registration(unittest.TestCase):
    def setUp(self):
        self.api = make_api()

    def tearDown(self):
        self.api.close()

    def _manifest(self, files, observation_id="obs"):
        return {
            "observation": {"repo_id": "R1", "observation_id": observation_id,
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": files,
        }

    def test_register_reports_digests(self):
        outcome = self.api.register(self._manifest([
            {"path": "com/acme/a/1.0/a-1.0.pom", "content": "<project/>"},
        ]))
        self.assertEqual(outcome["status"], "registered")
        files = self.api.files("R1", "obs")
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(files[0]["kind"], "pom")
        self.assertEqual(files[0]["size"], len("<project/>"))

    def test_same_observation_cannot_be_changed(self):
        self.api.register(self._manifest([{"path": "a.pom", "content": "<project/>"}]))
        with self.assertRaises(Exception) as raised:
            self.api.register(self._manifest([{"path": "a.pom", "content": "<project>2</project>"}]))
        self.assertIn("immutable", str(raised.exception))
        files = self.api.files("R1", "obs")
        self.assertEqual(files[0]["sha256"], self.api.store.read_file(
            "R1", "obs", "a.pom").sha256)
        # The content did not change.
        self.assertEqual(self.api.store.read_file("R1", "obs", "a.pom").content, b"<project/>")

    def test_re_registering_the_same_manifest_is_a_noop(self):
        manifest = self._manifest([
            {"path": "b.pom", "content": "<project/>"},
            {"path": "a.pom", "content": "<project/>"},
        ])
        first = self.api.register(manifest)
        second = self.api.register(manifest)
        self.assertEqual(first["status"], "registered")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])

    def test_registration_order_does_not_change_resolution(self):
        """Import the same observations shuffled, in two databases."""
        bundle = Bundle()
        bundle.observation("R1", "obs-parent", "2024-01-01T00:00:00Z")
        bundle.observation("R1", "obs-child", "2024-01-02T00:00:00Z")
        bundle.add_coordinate_pom("R1", "obs-parent", "com.acme", "parent", "1.0",
                                  pom("com.acme", "parent", "1.0",
                                      properties={"release": "1.0"},
                                      management=[("com.acme", "lib", "${release}")]))
        bundle.add_coordinate_pom("R1", "obs-child", "com.acme", "app", "1.0",
                                  pom("com.acme", "app", "1.0",
                                      parent=("com.acme", "parent", "1.0"),
                                      properties={"release": "2.0"},
                                      dependencies=[dep("com.acme", "lib", None)]))
        bundle.add_coordinate_pom("R1", "obs-child", "com.acme", "lib", "2.0",
                                  pom("com.acme", "lib", "2.0"))
        manifest_keys = list(bundle.observations)

        def resolve_all(api):
            return api.resolve(
                chain=[{"repo_id": "R1", "observation_id": "obs-parent"},
                       {"repo_id": "R1", "observation_id": "obs-child"}],
                coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
            )

        reference = make_api()
        for key in manifest_keys:
            reference.register(bundle.manifest(*key))
        reference_result = resolve_all(reference)
        reference.close()

        for seed in range(5):
            order = copy.copy(manifest_keys)
            random.Random(seed).shuffle(order)
            api = make_api()
            for key in order:
                api.register(bundle.manifest(*key))
            api_result = resolve_all(api)
            api.close()
            self.assertEqual(api_result["context"], reference_result["context"])
            self.assertEqual(api_result["status"], reference_result["status"])
            self.assertEqual(api_result["dependencies"], reference_result["dependencies"])
            self.assertEqual(api_result["management"], reference_result["management"])
            self.assertEqual(api_result["parent_chain"], reference_result["parent_chain"])

    def test_file_order_inside_one_manifest_does_not_matter(self):
        files = [
            {"path": "x/1.0/x-1.0.pom", "content": pom("com.acme", "x", "1.0")},
            {"path": "x/1.0/x-1.0.jar", "content": "jar"},
        ]
        manifest = self._manifest(files)
        shuffled = self._manifest(list(reversed(files)))
        first = self.api.register(manifest)
        second = self.api.register(shuffled)
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(second["status"], "unchanged")

    def test_declared_digest_mismatch_aborts_registration(self):
        with self.assertRaises(Exception) as raised:
            self.api.register(self._manifest([
                {"path": "a.pom", "content": "<project/>", "digest": "sha256:" + "0" * 64},
            ]))
        text = str(raised.exception)
        self.assertIn("digest-mismatch", text)
        self.assertIn("a.pom", text)
        self.assertEqual(self.api.observations(), [])

    def test_declared_digest_match_is_accepted_and_stored(self):
        import hashlib
        content = "<project/>"
        digest = hashlib.sha256(content.encode()).hexdigest()
        outcome = self.api.register(self._manifest([
            {"path": "a.pom", "content": content, "digest": f"sha256:{digest}"},
        ]))
        self.assertEqual(outcome["status"], "registered")

    def test_same_path_different_digest_in_different_observations(self):
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "obs1",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "x/1.0/x-1.0.pom", "content": pom("com.acme", "x", "1.0")}],
        })
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "obs2",
                            "observed_at": "2024-01-02T00:00:00Z"},
            "files": [{"path": "x/1.0/x-1.0.pom", "content": pom("com.acme", "x", "1.0",
                                                                  properties={"a": "b"})}],
        })
        one = self.api.store.read_file("R1", "obs1", "x/1.0/x-1.0.pom")
        two = self.api.store.read_file("R1", "obs2", "x/1.0/x-1.0.pom")
        self.assertNotEqual(one.sha256, two.sha256)
        self.assertNotEqual(one.content, two.content)

    def test_newest_observation_is_never_implicit(self):
        """obs2 is registered later and contains a different child POM."""
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "obs1",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "com/acme/app/1.0/app-1.0.pom", "content": pom(
                "com.acme", "app", "1.0", dependencies=[dep("com.acme", "old", "1.0")])}],
        })
        self.api.register({
            "observation": {"repo_id": "R1", "observation_id": "obs2",
                            "observed_at": "2024-06-01T00:00:00Z"},
            "files": [
                {"path": "com/acme/app/1.0/app-1.0.pom", "content": pom(
                    "com.acme", "app", "1.0", dependencies=[dep("com.acme", "new", "2.0")])},
                {"path": "placeholder.txt", "content": "unrelated observed file"},
            ],
        })
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs1"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        keys = {d["key"]["key"] for d in result["dependencies"]}
        self.assertEqual(keys, {"com.acme:old:jar:"})
        self.assertIn("obs1", result["subject"]["file"]["observation_id"])

    def test_unknown_observation_is_a_located_problem(self):
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "never-registered"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        problem = next(p for p in result["problems"] if p["code"] == "observation-not-registered")
        self.assertEqual(problem["details"]["observation_id"], "never-registered")

    def test_duplicate_chain_entry_is_refused(self):
        self.api.register(self._manifest([{"path": "a.pom", "content": "<project/>"}]))
        result = self.api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs"},
                   {"repo_id": "R1", "observation_id": "obs"}],
            coordinate={"groupId": "com.acme", "artifactId": "app", "version": "1.0"},
        )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("duplicate-chain-entry", {p["code"] for p in result["problems"]})

    def test_manifest_path_traversal_is_refused(self):
        with self.assertRaises(Exception):
            self.api.register(self._manifest([{"path": "../escape.pom", "content": "<project/>"}]))

    def test_content_file_is_read_and_hashed(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a.pom")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(pom("com.acme", "a", "1.0"))
        outcome = self.api.register(self._manifest([{"path": "a.pom", "content_file": path}]))
        self.assertEqual(outcome["status"], "registered")
        fact = self.api.store.read_file("R1", "obs", "a.pom")
        self.assertIn(b"<artifactId>a</artifactId>", fact.content)

    def test_missing_content_file_is_reported(self):
        with self.assertRaises(Exception) as raised:
            self.api.register(self._manifest([
                {"path": "a.pom", "content_file": "/nonexistent/nope.pom"},
            ]))
        self.assertIn("manifest-content-missing", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

"""Scale: many children sharing a multi-level parent chain.

The point is twofold:

* different files that must be parsed are recorded, and repeat reads of the
  *same* file/digest in a shared chain are cache hits;
* two contexts sharing an ancestor still get independent answers, because the
  cache key includes repository, observation, path and digest and the parsed
  document is immutable.
"""

from __future__ import annotations

import json
import time
import unittest

from support import make_api
from fixtures import Bundle, dep, dep_path, pom

N_CHILDREN = 60
CHAIN_DEPTH = 4


def build(database: str):
    api = make_api(database)
    bundle = Bundle()
    bundle.observation("R1", "obs-parents", "2024-01-01T00:00:00Z")
    bundle.observation("R1", "obs-children", "2024-01-02T00:00:00Z")

    # p0 <- p1 <- p2 <- p3, each level adding a property and a management entry.
    previous = None
    for level in range(CHAIN_DEPTH):
        properties = {f"level{level}": str(level), "shared": str(level)}
        management = [("com.acme", f"managed{level}", f"${{level{level}}}")]
        if level == 0:
            management.append(("com.acme", "managedShared", "${shared}"))
        parent = None
        if previous is not None:
            group, artifact, version = previous
            parent = (group, artifact, version)
        content = pom(
            "com.acme", f"p{level}", "1.0",
            parent=parent,
            properties=properties,
            management=management,
        )
        bundle.add_coordinate_pom("R1", "obs-parents", "com.acme", f"p{level}", "1.0", content)
        previous = ("com.acme", f"p{level}", "1.0")

    for index in range(N_CHILDREN):
        child = pom(
            "com.acme", f"child{index}", "1.0",
            parent=("com.acme", f"p{CHAIN_DEPTH - 1}", "1.0"),
            properties={"shared": f"child{index}"},
            dependencies=[
                dep("com.acme", "managed0", None),
                dep("com.acme", "managedShared", None),
                dep("com.acme", f"fixed{index}", "9.0"),
            ],
        )
        bundle.add_coordinate_pom("R1", "obs-children", "com.acme", f"child{index}", "1.0", child)
        bundle.add_coordinate_pom("R1", "obs-children", "com.acme",
                                  f"fixed{index}", "9.0", pom("com.acme", f"fixed{index}", "9.0"))
        bundle.add("R1", "obs-children", dep_path("com.acme", f"fixed{index}", "9.0", "jar"),
                   f"payload {index}")
    api.register(bundle.manifest("R1", "obs-parents"))
    api.register(bundle.manifest("R1", "obs-children"))
    return api


class ScaleAndSharing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = build(":memory:")

    @classmethod
    def tearDownClass(cls):
        cls.api.close()

    def _resolve(self, index, chain=None):
        return self.api.resolve(
            chain=chain or [
                {"repo_id": "R1", "observation_id": "obs-parents"},
                {"repo_id": "R1", "observation_id": "obs-children"},
            ],
            coordinate={"groupId": "com.acme", "artifactId": f"child{index}", "version": "1.0"},
        )

    def test_every_child_resolves_with_its_own_override(self):
        started = time.time()
        results = [self._resolve(i) for i in range(N_CHILDREN)]
        elapsed = time.time() - started
        for index, result in enumerate(results):
            self.assertEqual(result["status"], "resolved")
            entry = next(d for d in result["dependencies"]
                         if d["key"]["key"] == "com.acme:managed0:jar:")
            self.assertEqual(entry["version"]["resolved"], "0")
            shared_entry = next(d for d in result["dependencies"]
                                if d["key"]["key"] == "com.acme:managedShared:jar:")
            self.assertEqual(shared_entry["version"]["resolved"], f"child{index}")
            property_evidence = shared_entry["version"]["evidence"]["properties"]
            shared = next(p for p in property_evidence if p["name"] == "shared")
            self.assertEqual(shared["value"], f"child{index}")
            fixed = next(d for d in result["dependencies"]
                         if d["key"]["key"].startswith("com.acme:fixed"))
            self.assertEqual(fixed["version"]["resolved"], "9.0")
        self.assertLess(elapsed, 30)

    def test_shared_parent_files_are_parsed_once_per_importing_context(self):
        # Fresh API so the cache starts empty; the shared parent chain should be
        # parsed once and then hit for every later child.
        api = build(":memory:")
        first = api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs-parents"},
                   {"repo_id": "R1", "observation_id": "obs-children"}],
            coordinate={"groupId": "com.acme", "artifactId": "child0", "version": "1.0"})
        after_first = dict(first["cache"])
        # 4 shared parents + the child; dependency POMs are absent by design.
        self.assertEqual(after_first["pom_parses"], 4 + 1)
        for index in range(1, 20):
            api.resolve(
                chain=[{"repo_id": "R1", "observation_id": "obs-parents"},
                       {"repo_id": "R1", "observation_id": "obs-children"}],
                coordinate={"groupId": "com.acme", "artifactId": f"child{index}",
                            "version": "1.0"})
        last = api.resolve(
            chain=[{"repo_id": "R1", "observation_id": "obs-parents"},
                   {"repo_id": "R1", "observation_id": "obs-children"}],
            coordinate={"groupId": "com.acme", "artifactId": "child19", "version": "1.0"})["cache"]
        # Parents remain 4 distinct parses; new children/deps add some.
        parent_parses = [p for p in last["parsed_files"] if "/p" in p and "child" not in p]
        self.assertEqual(len(parent_parses), 4)
        self.assertGreater(last["pom_cache_hits"], 4 * 15)
        api.close()

    def test_cache_keys_include_repository_and_observation(self):
        # Same path in two observations, different content -> different keys,
        # and the cache does not serve one for the other.
        api = make_api()
        api.register({
            "observation": {"repo_id": "R1", "observation_id": "obs-a",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "com/acme/shared/1.0/shared-1.0.pom",
                       "content": pom("com.acme", "shared", "1.0",
                                      properties={"tag": "a"},
                                      dependencies=[dep("com.acme", "x", "1.0")])}],
        })
        api.register({
            "observation": {"repo_id": "R1", "observation_id": "obs-b",
                            "observed_at": "2024-01-01T00:00:00Z"},
            "files": [{"path": "com/acme/shared/1.0/shared-1.0.pom",
                       "content": pom("com.acme", "shared", "1.0",
                                      properties={"tag": "b"},
                                      dependencies=[dep("com.acme", "y", "2.0")])}],
        })
        a = api.resolve(chain=[{"repo_id": "R1", "observation_id": "obs-a"}],
                        coordinate={"groupId": "com.acme", "artifactId": "shared",
                                    "version": "1.0"})
        b = api.resolve(chain=[{"repo_id": "R1", "observation_id": "obs-b"}],
                        coordinate={"groupId": "com.acme", "artifactId": "shared",
                                    "version": "1.0"})
        self.assertEqual({d["key"]["key"] for d in a["dependencies"]},
                         {"com.acme:x:jar:"})
        self.assertEqual({d["key"]["key"] for d in b["dependencies"]},
                         {"com.acme:y:jar:"})
        self.assertEqual(a["properties"]["effective"][0]["value"], "a")
        self.assertEqual(b["properties"]["effective"][0]["value"], "b")
        # obs-a was parsed once; obs-b is a different key and is parsed once.
        self.assertEqual(b["cache"]["pom_parses"], 2)
        self.assertEqual(len(b["cache"]["cache_hit_files"]), 0)
        api.close()

    def test_children_with_different_chains_do_not_leak_property_values(self):
        chain_with_only_first_parent = [
            {"repo_id": "R1", "observation_id": "obs-parents"},
            {"repo_id": "R1", "observation_id": "obs-children"},
        ]
        result_a = self._resolve(3, chain=chain_with_only_first_parent)
        # A chain missing the parents entirely must be blocked for that child,
        # while the earlier result stays valid.
        blocked = self._resolve(4, chain=[{"repo_id": "R1", "observation_id": "obs-children"}])
        self.assertEqual(result_a["status"], "resolved")
        self.assertEqual(blocked["status"], "blocked")
        again = self._resolve(3, chain=chain_with_only_first_parent)
        self.assertEqual(again["dependencies"], result_a["dependencies"])
        self.assertEqual(again["properties"], result_a["properties"])


if __name__ == "__main__":
    unittest.main()

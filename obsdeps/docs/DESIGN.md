# obsdeps — dependency resolution inside explicit observation contexts

`obsdeps` answers a question a delivery engineer actually asks of an offline
Maven mirror:

> In **this particular snapshot of material**, what declarations does artifact
> X really inherit, and **why** did dependency Y resolve to this version?

It does that by treating every import as an **immutable observation** and every
query as an **explicit context** (an ordered repository chain of exact
observations plus a subject). Results always carry that context, so a later
observation can never silently rewrite an earlier answer.

This is a data-processing tool over local files. It does **not** download
anything, run Maven or any build, judge vulnerabilities, or attempt full Maven
compatibility.

---

## 1. Concepts

| Term | Meaning |
| --- | --- |
| **Observation** | One bundle: `repo_id`, `observation_id`, `observed_at`, and a complete list of files with their bytes and SHA-256 digests. It is immutable. New material is a **new** observation. |
| **Context** | A request: an ordered chain `[(repo_id, observation_id), ...]` and a subject (a coordinate or an explicit POM path inside one of those observations). |
| **Effective project** | The subject POM plus the parent chain selected *inside that context*, with properties and `dependencyManagement` computed for the subject. |
| **Derivation** | The evidence trail: which file/line declared a version, which property supplied its value, where that property was defined or overridden, and which file was selected for each artifact. |

Two observations of the same coordinate can differ. Nothing global — not the
newest observation, not the highest version, not another repository — is ever
consulted implicitly.

### Result status

| Status | Meaning |
| --- | --- |
| `resolved` | Every direct dependency has a derived version; all needed chain files were read. |
| `partial` | The chain is complete, but something is underdetermined (unresolved version), missing (a SNAPSHOT mapping/target, an artifact POM), or unsupported in a way that affects resolution. |
| `blocked` | Material required to evaluate the subject is absent (e.g. the parent POM is missing). Raw declarations are still reported, explicitly marked `not-evaluated`. |
| `invalid` | Observed facts contradict each other or the input is malformed (parent cycle, duplicate key, digest mismatch, duplicate SNAPSHOT mapping, …). |

Report buckets are always kept apart:

* `problems` — contradictions;
* `missing` — material the selected observations do not contain;
* `unsupported` — constructs this subset refuses to interpret, each with
  `details.affects_resolution`;
* per-dependency `status: unresolved` plus `missing[dependency-version-unresolved]`
  — never an empty or invented dependency.

---

## 2. Install and run

Pure Python 3.9+, standard library only (SQLite via `sqlite3`). No network.

```bash
# from a checkout, no installation needed
python3 tools/obsdeps_cli.py --help

# or install it
python3 -m pip install .
obsdeps --help
```

### CLI

```bash
# 1. import observations (idempotent; a different manifest for the same id is refused)
obsdeps register --db mirror.sqlite3 --manifest bundle/R1__obs-2024-04-08.json

# 2. resolve one explicit context
obsdeps resolve --db mirror.sqlite3 \
  --chain "R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --coordinate com.acme:shop:1.0

# 3. explain one dependency inside that context
obsdeps explain --db mirror.sqlite3 \
  --chain "R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --coordinate com.acme:shop:1.0 --dependency com.acme:telemetry

# 4. compare two explicit contexts
obsdeps compare --db mirror.sqlite3 \
  --a-chain "R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --b-chain "R0=obs-alt,R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --a-coordinate com.acme:shop:1.0 --b-coordinate com.acme:shop:1.0

# helper commands
obsdeps observations --db mirror.sqlite3
obsdeps files --db mirror.sqlite3 --observation R1=obs-2024-04-08
```

* `--chain` accepts `repo=obs,repo2=obs2` or a JSON array.
* `--coordinate` accepts `groupId:artifactId:version` or JSON.
* `--pom PATH --observation repo=obs` selects a root POM file instead of a
  coordinate (useful when the coordinate itself is what you are investigating).
* Output is JSON. Exit codes: `0` resolved/partial, `2` blocked/invalid,
  `1` usage or manifest error (nothing registered).

### Python API

```python
from obsdeps import Api

api = Api("mirror.sqlite3")
api.register_manifest("bundle/R1__obs-2024-04-08.json")

result = api.resolve(
    chain=[{"repo_id": "R1", "observation_id": "obs-2024-04-08"},
           {"repo_id": "R1", "observation_id": "obs-2024-04-01"}],
    coordinate={"groupId": "com.acme", "artifactId": "shop", "version": "1.0"},
)
for dep in result["dependencies"]:
    print(dep["key"]["key"], dep["version"]["resolved"], dep["version"]["source"])

explanation = api.explain(
    chain=[...], coordinate={...},
    dependency={"groupId": "com.acme", "artifactId": "telemetry"},
)
comparison = api.compare(
    {"chain": [...A...], "coordinate": {...}},
    {"chain": [...B...], "coordinate": {...}},
)
```

`Api` is re-entrant across calls; `api.store` and `api.files()` expose what was
imported. Use `with Api(path) as api:` for automatic closing.

### Manifest format

```json
{
  "observation": {
    "repo_id": "R1",
    "observation_id": "obs-2024-04-08",
    "observed_at": "2024-04-08T00:00:00Z"
  },
  "files": [
    {"path": "com/acme/shop/1.0/shop-1.0.pom", "content": "<project>…</project>"},
    {"path": "com/acme/lib/2.0/lib-2.0.jar",
     "content_file": "/local/bundle/lib-2.0.jar",
     "digest": "sha256:9f2c…"}
  ]
}
```

* `path` is repository-relative; `..` and absolute paths are refused.
* `content` is inline UTF-8 text; `content_file` points at local bytes.
* `digest` is optional (`sha256:`/`sha1:`/`md5:` or bare hex). When present it
  is verified; a mismatch aborts the whole registration with a located problem.
* Every stored digest is the one computed from the stored bytes. The same path
  with different bytes is a normal, separate observation fact.

---

## 3. The worked example

`examples/build_bundle.py` writes the input manifests; `examples/expected.json`
holds expectations written **by hand** from those files;
`examples/run_worked_example.py` runs the resolver and compares. Nothing in the
comparison is generated by the resolver.

```bash
python3 examples/build_bundle.py /tmp/obs-bundle    # just look at the inputs
python3 examples/run_worked_example.py              # check against expectations
```

The scenario:

* `com.acme:shop:1.0` has parent `com.acme:shop-parent:1.0`, overrides
  `release=2.0`, manages `pricing` with `${release}`, and declares
  `pricing` (no version), `audit` (explicit `3.0`), `telemetry` (no version).
* **A** (child observation only): parent is absent → `blocked`, no derived
  dependencies, `pom-missing` names the parent edge.
* **B** (R1 parent `obs-later`): pricing `2.0` (child's override),
  telemetry `4.1` (parent property), audit `3.0` (direct wins).
* **C** (R0 parent first, then R1): pricing stays `2.0`, audit stays `3.0`,
  telemetry becomes `5.5` because only that property was not overridden. The
  parent edge records `R0@obs-alt-parent` and the search trail stops there.
* Re-querying A after B/C are present still returns `blocked`; re-querying B
  returns the same subject file and digests.
* A SNAPSHOT library's POM, main jar and `tests` jar come from three different
  `snapshotVersions` values; each is checked against its actual observed file.

---

## 4. What the tool guarantees

### Observations

* Stored bytes are the observed facts; digests are computed at registration.
* Import order never affects resolution: manifest identity is a hash of
  path-sorted file digests, and lookups are by explicit context.
* An observation cannot be updated or deleted (SQLite triggers enforce this
  even outside the API). Re-registering a byte-identical manifest is a no-op;
  different content under an existing id is refused.

### Contexts

* Every result carries `context.chain`, `context.root`, `context_sha256` and
  the engine version.
* The parse cache is keyed by `(repo, observation, path, sha256)` and parsed
  documents are never mutated, so two contexts may share an ancestor's parse
  but can never share its *effective* property values. Each result reports
  `cache.parsed_files` and `cache.cache_hit_files`.
* The database's newest observation is never consulted. `--chain` is the whole
  world.

### Coordinates and parents

* Logical coordinates are `groupId/artifactId/version`; artifacts also have
  `type` and `classifier`.
* A project's own `groupId`/`version` may be inherited from its parent;
  `artifactId` must be on the project itself.
* `project.*` is refused while computing any coordinate (own or parent), and
  also when reached *indirectly* through a property used in a coordinate.
* Parent coordinates may use only that POM's own literal properties. A
  property that the parent would supply is unknown at this point and is
  refused rather than guessed. `<relativePath>` is ignored, with a reported
  reason.
* Parent lookup walks the request's chain in order and takes the first
  observation containing the coordinate. If that POM is damaged, the walk
  stops (`parent-pom-invalid`) instead of falling through to a later
  repository. Each edge records repo, observation, path, digest and the
  per-repository search trail.
* Parent cycles report the actual coordinates and the source-file chain.

### Properties

* Inherited raw definitions, child overrides win, and expansion happens in the
  **child's** environment only; a parent's resolved values are never rewritten
  by a descendant.
* Expansion is a graph walk with cycle detection (the full chain is reported)
  and explicit unknown-reference problems. Every `${` must be gone afterwards;
  a barely-replaced expression is never accepted as a version.
* Supported references: `${project.groupId|artifactId|version}` (and the
  legacy `${pom.…}`), plus `${propertyName}`. `${env.*}`, `${settings.*}` and
  any other `project.*` field are refused with a located problem.
* `properties.lineage` records every definition of an overridden name with
  which project overrode which.

### Dependency management

* Key is the full tuple `(groupId, artifactId, type, classifier)`; `type`
  defaults to `jar`, `classifier` to the empty string.
* Child entries override inherited entries for the same key. Duplicate keys
  within one POM are refused.
* A direct dependency's explicit `version` always wins; otherwise the final
  management table supplies it; otherwise it is `unresolved`. No version is
  ever chosen from the repository's highest version.
* Managed entries keep their raw text and property references, so the evidence
  shows the declaration file/line, the version-value line, the property value
  used and where that property was defined or overridden. Versions are never
  flattened to strings without their sources.
* Managed entries with `scope=import` are excluded and reported as
  `unsupported-bom-import` (BOM import is not supported; the BOM is not read).

### SNAPSHOTs

* A `-SNAPSHOT` version's files are selected **only** from
  `versioning/snapshotVersions` entries in `maven-metadata.xml`, matched by
  `(extension, classifier)`, and the named file must exist in the *same
  observation* that supplied the metadata. The extension matched is the
  dependency's `type` verbatim: `type=test-jar` looks for an `extension=test-jar`
  mapping, exactly as the metadata spells it. This tool does not apply Maven's
  type→extension mapping (`test-jar`→`jar`).
* The POM, the main artifact and each classifier are selected independently.
  It is normal — and tested — for the POM, `jar` and `tests` jar to have three
  different values.
* Timestamps and build numbers are reported but never used to build names.
* Missing mapping, duplicate conflicting mapping, missing metadata, missing
  mapped file and metadata/coordinate mismatch each produce a located problem.

### Read-only input

Registration reads listed files once and stores the bytes. The tool never
writes to the manifest directory or to the repository material.

---

## 5. Deliberate differences from real Maven

This tool is **not** a Maven model builder. It reports what it does not
interpret instead of pretending.

| Area | This tool | Real Maven |
| --- | --- | --- |
| Parent lookup | Only the request's ordered repository chain; `relativePath` ignored | `relativePath`, local repo, remote repos, `-N`/workspace rules |
| Parent coordinates | Own literal properties only; no `project.*`; no parent-supplied properties | Full interpolation, some circularity resolved by effective-model phases |
| Properties | Single-pass graph, no late evaluation; no env/settings/system expressions | Full expression evaluation incl. settings, env, system, profiles |
| Profiles | Ignored and reported if present | Activated by environment/OS/JDK/files/explicit args |
| BOM import | Refused and reported (`scope=import` excluded) | `import` scope pulls and merges the BOM's management |
| Direct dependencies | The POM's own declarations only | Plus inheritance from the parent's `dependencies` |
| `type` → extension | Used verbatim for paths and SNAPSHOT metadata keys | Mapped (`test-jar`→`jar`, `ejb`→`jar`, …) |
| Transitive dependencies | Not solved; absent direct files are simply not fetched | Full graph with mediation, exclusions, scopes, conflicts |
| Scope/optional/exclusions | Recorded but not interpreted (systemPath affects resolution) | Core to the graph |
| Version selection | Never automatic | Ranges, conflicts, `RELEASE`/`LATEST`, metadata-driven |
| SNAPSHOT | Only explicit `snapshotVersions` mapping entries | Also `timestamp`/`buildNumber`-derived names and local-copy rules |
| Repositories | Request chain only | POM/`settings.xml` repositories, mirrors, proxies |
| Build/plugins/modules/relocation | Ignored and reported | Meaningful |
| Model validation | Only the checks listed here | Full POM schema/model validation and warnings |

`unsupported` entries carry `details.affects_resolution`. Nodes like
`profiles`, `relocation`, `systemPath`, or unknown top-level nodes count as
affecting (so the result becomes `partial`); `build`, `modules`,
`repositories`, `pluginRepositories` and descriptive metadata do not, because
the documented policy replaces them and they cannot change the reported
coordinates here.

---

## 6. Error codes

All problems are `{code, message, locator, details?}`. Locators are
`repo/observation/path:line` or `request.chain[i]` / `manifest:path`.

| Code | Bucket | Meaning |
| --- | --- | --- |
| `manifest-invalid-json`, `manifest-missing-field`, `manifest-entry-invalid`, `manifest-path-invalid`, `manifest-duplicate-path`, `manifest-digest-invalid`, `manifest-content-missing`, `manifest-empty` | registration | Malformed manifest; nothing is stored. |
| `digest-mismatch` | registration | Declared digest ≠ stored bytes. |
| `observation-conflict` | registration | Same observation id, different content. |
| `observation-not-registered`, `empty-chain`, `duplicate-chain-entry` | problem | The request is not a valid context. |
| `pom-xml-error`, `pom-doctype-unsupported`, `pom-invalid`, `pom-namespace-unsupported`, `pom-missing-artifact-id`, `pom-duplicate-element`, `duplicate-property` | problem | Unusable POM / namespace. |
| `duplicate-direct-dependency`, `duplicate-management-entry`, `management-version-missing` | problem | Conflicting declarations. |
| `coordinate-invalid`, `coordinate-unresolved`, `coordinate-project-reference`, `coordinate-mismatch` | problem | Project coordinates cannot be determined or disagree with the requested one. |
| `parent-coordinate-missing`, `parent-coordinate-invalid`, `parent-pom-invalid`, `parent-cycle`, `parent-depth-exceeded`, `pom-missing` | problem/missing | Parent chain cannot be trusted. |
| `property-cycle`, `unknown-property`, `unsupported-expression`, `unresolved-expression`, `unknown-project-coordinate`, `expression-depth-exceeded` | problem | Property/expression failure. |
| `snapshot-metadata-missing`, `snapshot-mapping-missing`, `snapshot-target-missing`, `snapshot-mapping-invalid` | missing/problem | SNAPSHOT selection cannot be made from observed facts. |
| `snapshot-mapping-duplicate`, `metadata-coordinate-mismatch`, `metadata-xml-error`, `metadata-invalid` | problem | Contradictory metadata. |
| `artifact-missing` | per-file fact | The bundle has no file at that path (not a failure by itself). |
| `artifact-digest-mismatch` | problem | A `.sha1`/`.sha256` sidecar disagrees with the file. |
| `dependency-version-unresolved` | missing | No explicit version, no usable management entry. |
| `unsupported-node`, `unsupported-bom-import` | unsupported | Construct not interpreted; `affects_resolution` says whether it changes the verdict. |
| `root-file-outside-context`, `root-file-missing` | problem/missing | Explicit root POM not usable in this context. |

---

## 7. Cache semantics (and what it cannot do)

The only cache is the in-memory parsed-document cache. Its key is
`(repo_id, observation_id, path, sha256)`, so:

* **Parent selection** is not cached. Every query re-walks the requested chain,
  so choosing a different observation for a parent produces a different result
  and different evidence. Re-parsing is skipped only for the exact same file.
* **Property overrides** are not cached. Effective properties are computed per
  context from raw definitions, and expansion results are cached only inside
  the per-project interpolator for that one resolution.
* **SNAPSHOT metadata** is cached by file identity. A different observation (or
  a different metadata file) is a different key; changed mappings are
  re-parsed and take effect for contexts that select the new observation.

Nothing resolved is persisted, so there is no resolution cache to invalidate.
Results are reproduced from the stored observation bytes every time. Each
result reports `cache.parsed_files` (distinct documents parsed) and
`cache.cache_hit_files` (documents served from cache), which is the evidence
for shared parents in large runs (see `tests/test_scale.py`).

---

## 8. Reproducing the checks

```bash
# full test suite (plain unittest, no pytest required)
cd tests && PYTHONPATH=../src python3 -m unittest discover -s . -p "test_*.py"

# just the hand-worked example against hand-written expectations
python3 examples/run_worked_example.py

# CLI smoke test
python3 tools/obsdeps_cli.py observations --db /tmp/does-not-exist.sqlite3
```

The suite includes:

* `test_acceptance.py` — the brief's A/B/C scenario, re-query stability.
* `test_handworked.py` — a small example checked against values computed by hand.
* `test_snapshot.py` — independent POM/main/tests-jar selection, all SNAPSHOT
  failure modes.
* `test_problems.py` — parent cycle, first-repo damage, missing parent,
  `project.*`/indirect `project.*` in coordinates, property cycles, unknown
  properties, duplicate keys, BOM import, unsupported nodes.
* `test_store.py` — immutability, digest handling, registration-order
  independence, no implicit newest observation.
* `test_compare.py` — source-only change detection, explain narrative.
* `test_scale.py` — 60 children over a 4-level shared parent chain, cache
  accounting, cross-context isolation.
* `test_cli.py`, `test_examples.py`.

The worked example is verified against `examples/expected.json`, written by
hand from the bundle inputs; `test_examples.py` additionally refuses an
expectations file that looks engine-generated and re-checks reported artifact
digests against the input bytes independently.

---

## 9. Stored data and reproducibility

The SQLite database has two fact tables plus one metadata row per database:

* `observations (repo_id, observation_id, observed_at, manifest_sha256, registered_at)`
* `observation_files (repo_id, observation_id, path, kind, sha256, size,
  declared_digest_algo, declared_digest, content BLOB)`

There are no tables for resolutions or derived values: answers are computed
from the stored bytes on every query. Triggers reject `UPDATE` and `DELETE` on
both tables. `manifest_sha256` is the SHA-256 of the canonical manifest (files
sorted by path, `sha256` of each file), which is why import order and file
order inside a manifest cannot change anything.

To reproduce a run byte for byte: keep the database (or the manifests), keep
the request JSON, and compare `context.context_sha256`, `parent_edges[*].selected.sha256`
and `dependencies[*].files[*].sha256`. Those are all derived from input bytes;
timestamps never enter them.

## 10. Repository layout

```
src/obsdeps/
  manifest.py      manifest parsing, hashing, classification
  store.py         SQLite schema, immutability triggers, exact-observation reads
  xmlutil.py       namespace-aware XML with line numbers, DTD refusal
  pom.py           supported POM subset + unsupported reporting
  metadata.py      maven-metadata.xml snapshotVersions
  interpolate.py   property graph, cycles, unknown refs, provenance
  pomproject.py    parent-chain walk, coordinates, effective properties
  resolution.py    file selection (public/snapshot), request model, parse cache
  engine.py        management table, dependency derivation, artifacts, status
  compare.py       context comparison
  api.py           register/resolve/explain/compare facade
  cli.py           command line
tools/obsdeps_cli.py   source-checkout launcher
examples/             input builder, hand-written expectations, runner
tests/                unittest suite and fixtures
```

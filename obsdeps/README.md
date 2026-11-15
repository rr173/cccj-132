# obsdeps

Explain Maven dependency resolution inside **explicit, immutable observations**
of an offline repository. Given the exact material observed at a point in time,
`obsdeps` reports what a project inherits, why each dependency got its version,
and what changes between two such contexts — with a locatable evidence trail and
without reaching for anything newer.

Pure Python 3.9+, standard library only. No network, no Maven, no builds.

## Quick start

```bash
python3 tools/obsdeps_cli.py register --db mirror.sqlite3 --manifest bundle.json

python3 tools/obsdeps_cli.py resolve --db mirror.sqlite3 \
  --chain "R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --coordinate com.acme:shop:1.0

python3 tools/obsdeps_cli.py explain --db mirror.sqlite3 \
  --chain "R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --coordinate com.acme:shop:1.0 --dependency com.acme:telemetry

python3 tools/obsdeps_cli.py compare --db mirror.sqlite3 \
  --a-chain "R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --b-chain "R0=obs-alt,R1=obs-2024-04-08,R1=obs-2024-04-01" \
  --a-coordinate com.acme:shop:1.0 --b-coordinate com.acme:shop:1.0
```

```python
from obsdeps import Api

with Api("mirror.sqlite3") as api:
    api.register_manifest("bundle.json")
    result = api.resolve(
        chain=[{"repo_id": "R1", "observation_id": "obs-2024-04-08"}],
        coordinate={"groupId": "com.acme", "artifactId": "shop", "version": "1.0"},
    )
    print(result["status"], result["dependencies"])
```

## What it does and does not do

It supports a documented POM subset: coordinates, parent inheritance, properties,
`dependencyManagement`, and the subject's own direct dependencies. It handles
version derivation with full provenance, ordered repository-chain parent lookup,
SNAPSHOT file selection strictly from `maven-metadata.xml` mappings, and
comparison of two explicit contexts.

It does not use profiles, BOM imports, plugins, arbitrary expressions, or solve
transitive dependencies, ranges or conflicts. Every unsupported construct that
is present is reported, with `affects_resolution` saying whether it changes the
verdict. Anything missing, contradictory or unsupported is listed separately
from the dependencies themselves.

* Full design, guarantees, limits, error codes and cache semantics:
  [docs/DESIGN.md](docs/DESIGN.md)
* Runnable hand-checked example: `python3 examples/run_worked_example.py`
* Tests: `cd tests && PYTHONPATH=../src python3 -m unittest discover -s . -p "test_*.py"`

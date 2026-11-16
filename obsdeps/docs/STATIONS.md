# Station directory collection

`obsdeps stations` collects cursor-paged charging-station directories from
several public charging networks into one local SQLite database and keeps a
queryable, evidenced picture of each station's current state. It is pure
standard-library Python and can run fully offline against a local mock
interface.

The feature lives in the `obsdeps.stations` subpackage:

| module | responsibility |
| --- | --- |
| `stations/adapters.py` | the two provider JSON envelopes and their own field mappings; per-record validation |
| `stations/transport.py` | `HttpTransport` (real HTTP) and `MockTransport` (local deterministic interface) |
| `stations/engine.py` | page walking, opaque cursor handling, cycle detection, per-source failure isolation |
| `stations/store.py` | SQLite evidence tables and the derived current-state view |
| `stations/api.py` | the `StationApi` Python facade |
| `stations/cli.py` | the `obsdeps stations ...` command line |

## Source configuration

Each source is one object with a unique `provider_id`, an `endpoint` and an
`adapter_type`. A run takes a list of sources (a bare JSON array, or an object
holding a `sources` array):

```json
{
  "sources": [
    {"provider_id": "ALPHA", "endpoint": "https://alpha.example/directory",
     "adapter_type": "shell_v1"},
    {"provider_id": "BETA", "endpoint": "https://beta.example/directory",
     "adapter_type": "shell_v2"}
  ]
}
```

Unknown adapter types, duplicate `provider_id`s and missing fields are refused
before any request is made, and no database file is created.

### The two supported shells

The adapters never share field rules.

**`shell_v1`** — envelope `{"stations": [...], "next_cursor": "..."}`

| local concept | provider field |
| --- | --- |
| source station id | `id` |
| name / address | `name` / `address` |
| coordinates | `lat`, `lon` |
| observed at | `observed_at` (RFC 3339, explicit offset) |
| operating status | `status` (string, kept verbatim) |
| canonical id | `canonical_id` (optional) |
| connectors | `connectors[]: {type, available, total}` |

**`shell_v2`** — envelope `{"data": {"sites": [...], "cursor": "..."}}`

| local concept | provider field |
| --- | --- |
| source station id | `site_id` |
| name | `site_name` |
| address | `location.address` |
| coordinates | `location.geo.latitude`, `location.geo.longitude` |
| observed at | `last_updated` |
| operating status | `operational` (boolean → `OPEN` / `CLOSED`) |
| canonical id | `canonical` (optional) |
| connectors | `connector_counts[]: {connector_type, free, count}` |

A response whose top-level shape does not match the configured adapter is a
**page structure error** for that source only.

## Pagination and cursors

* The cursor is opaque. The engine stores exactly the text the provider
  returned and sends it back verbatim as the `cursor` query parameter on that
  provider's next request. It is never parsed, normalised or reused.
* Cursors are scoped to their source. Two providers may return identical
  cursor text (the worked example deliberately uses `"cur-a"` in both); each
  provider keeps its own request/next-cursor chain in `station_pages`, keyed by
  `(run_id, provider_id, page_seq)`.
* A page whose `next_cursor` is `null`/absent ends that source normally and
  marks it `complete`.
* If a provider returns a cursor that was **already requested** for that same
  provider, the engine stops following it, records a `page-cursor-cycle`
  problem (with the repeated cursor and page sequence) and marks the source
  `incomplete`. The pages already read — including the page that carried the
  repeated cursor — stay stored and queryable; the source is never reported
  complete. A safety page cap (`--max-pages`) additionally bounds feeds that
  hand out fresh-but-endless cursors.

### Failure scope

Failures are contained at the smallest useful scope:

| failure | scope | effect |
| --- | --- | --- |
| HTTP/network error, undecodable body, wrong envelope | one page of one source | source stops, `incomplete`, `page-transport-error` / `page-structure-error` carrying provider, page sequence and cursor; other sources in the batch still collect and remain queryable |
| repeated cursor | one source | source stops, `incomplete`, `page-cursor-cycle`; other sources unaffected |
| one bad station record (bad counts, coordinates, types ...) | one row | that record is rejected into `station_record_problems` with its original locator, e.g. `ALPHA/page2/stations[3]` (the connector index is in `details.connector_index`); sibling rows and other sources are unaffected |

## Identity rules

1. A provider station id is meaningful **only inside its provider**. The local
   identity is `(provider_id, station_id)` (exposed as `station_key`,
   e.g. `ALPHA:A1`).
2. Two stations from different providers merge into one queryable group **only
   when they carry the same explicit, non-empty canonical id**
   (`stations canonical --canonical-id ...`). Each member keeps its own
   provider-scoped observation evidence; merging never moves evidence between
   providers.
3. Missing canonical ids never merge. Two providers' stations with identical
   names and addresses stay separate rows unless a shared explicit canonical id
   exists. Nothing is inferred from name, address or coordinates.
4. If the same `(provider_id, station_id)` is observed with two different
   explicit canonical ids, the contradiction is preserved in
   `station_canonical_conflicts` (with each variant's locator and timestamp),
   the derived row gets reason `canonical-id-conflict`, and no canonical id is
   chosen — the station is never silently rebound.

## Current-state derivation

Page arrival order never decides a value. For each local identity, across all
stored runs:

* only observations carrying the **newest `observed_at`** can define the
  current state — an older record arriving on a later page cannot overwrite a
  newer value, and the superseded record remains in the observation history;
* if two or more observations share the newest timestamp but disagree on
  `status` or on connector counts, the station is marked `CONFLICT`
  (`conflict = true`, reason `same-timestamp-state-conflict`). No record is
  picked as the winner; every tied record is retained as current evidence;
* identical repeated records at the same timestamp are duplicate evidence, not
  a conflict.

Validation rules applied before a record is accepted: connector counts are
non-negative integers, `available <= total`, latitude in `[-90, 90]`,
longitude in `[-180, 180]`, booleans are not accepted as numbers, and
timestamps need an explicit UTC offset and are normalised to UTC.

## Re-runs and determinism

* A run id is a SHA-256 fingerprint of the normalised evidence (all accepted
  observations, page cursors and problems), so collecting the same inputs
  yields the same run id.
* Re-collecting under that run id atomically replaces the run's evidence and
  rebuilds `station_current`; other runs are untouched. Repeated runs therefore
  produce identical rows rather than duplicates — verified in
  `tests/test_stations_cli.py` and the worked example.
* Passing an explicit `--run-id` stores distinct runs side by side; the
  current-state view is then derived from the union of their observations.
* `--started-at` fixes the run timestamp for byte-stable demonstrations.

## SQLite tables

* evidence (append-only per run): `station_runs`, `station_run_sources`,
  `station_pages` (page sequence, request/next cursor),
  `station_observations` (normalised values, `raw_locator`, original payload),
  `station_record_problems`, `station_page_problems`,
  `station_canonical_conflicts`, `station_sources`;
* derived: `station_current` — one row per local identity with the current
  status/connectors, conflict flags, latest timestamp and the ids of the
  observations that back it.

The CLI JSON and direct `sqlite3` queries over these tables are cross-checked
in the test suite.

## Command line

```bash
# Collect (use --mock-fixture for the fully local interface).
obsdeps stations collect --db stations.sqlite3 --sources sources.json \
    [--mock-fixture mock.json] [--run-id ID] [--started-at TS] [--max-pages N]
obsdeps stations list     --db stations.sqlite3 [--provider P] [--status OPEN]
obsdeps stations show     --db stations.sqlite3 --provider ALPHA --station A1
obsdeps stations canonical --db stations.sqlite3 --canonical-id CAN-RIVERSIDE
obsdeps stations runs     --db stations.sqlite3
obsdeps stations run      --db stations.sqlite3 --run-id run-...
obsdeps stations problems --db stations.sqlite3 [--run-id run-...]
obsdeps stations sources  --db stations.sqlite3
```

Exit codes: `0` success with every source complete and no rejected record;
`2` the command ran and data is queryable but at least one source is
incomplete or at least one record was rejected; `1` usage/configuration error
before collection.

## Python API

```python
from obsdeps.stations import StationApi, MockTransport, HttpTransport

transport = MockTransport.from_file("mock.json")   # or HttpTransport()
with StationApi("stations.sqlite3") as api:
    summary = api.collect(sources, transport=transport)
    api.list_stations(status="OPEN")
    api.get_station("ALPHA", "A1")                 # state + evidence + history
    api.get_canonical_group("CAN-RIVERSIDE")
    api.list_problems()
```

## Worked example and tests

```bash
python3 examples/build_stations_fixture.py examples/stations_mock.json
python3 examples/run_stations_example.py
```

The example states every expectation on paper (page cursors, which observation
wins, conflict members, rejected row locators, the cycling source) and asserts
each one. Tests:

```bash
cd tests
PYTHONPATH=../src python3 -m unittest test_stations_adapters \
    test_stations_handworked test_stations_cli
```

## Problem codes

| code | meaning |
| --- | --- |
| `station-record-invalid` | one station row failed validation; locator pins the original row, `details.field` names the offending field |
| `page-transport-error` | HTTP/network/JSON failure fetching a page; details carry endpoint, cursor, page sequence |
| `page-structure-error` | the body does not match the adapter envelope |
| `page-cursor-cycle` | a cursor already requested for this provider was returned again; source stopped, incomplete |
| `page-limit-exceeded` | the safety page cap was reached; source stopped, incomplete |

# Offline proxy-vote reconciliation

`obsdeps proxyvote` turns one shareholder meeting's record-date roster, proxy
authorizations and submitted ballots into, for every proposal, the shares
validly cast for each choice, the shares that abstained, the shares in
dispute, and the rejected ballots — together with a segment-by-segment trail
that recomputes every total. It reads only local JSONL files or an immutable
local SQLite database, never touches the network, and never rewrites its
inputs.

Pure Python 3.9+, standard library only (`sqlite3`, `datetime`).

| module | responsibility |
| --- | --- |
| `proxyvote/inputs.py` | JSONL loading, request-scoped roster version, row validation |
| `proxyvote/engine.py` | grant-graph validation, ownership sweep, ballot adjudication, evidence |
| `proxyvote/store.py` | immutable SQLite rows and deterministic result ids |
| `proxyvote/api.py` | the `ProxyVoteApi` facade |
| `proxyvote/cli.py` | the `obsdeps proxyvote ...` command line |
| `tests/proxyvote_enumerator.py` | independent per-share reference (tests only) |

---

## 1. Inputs

Four **read-only JSONL** documents, one JSON object per line, all belonging to
the same `meeting_id`. Blank lines are ignored but are part of the file
digest. Malformed JSON anywhere makes the *document* unreadable and aborts the
run (`ProxyVoteInputError`); a syntactically valid row that is wrong is a
*row* problem, scoped and reported (see §7).

### Roster — `--roster`

```json
{"meeting_id":"M","position_id":"P","holder_id":"H","security_class":"COMMON","record_date":"2026-03-01","shares":100}
```

* `position_id` is the identity. Two positions of the same `holder_id` are
  **never merged**; each is its own share universe.
* `shares` is a positive integer; the position's unit range is the half-open
  `[0, shares)`.
* `record_date` must equal the record date selected in the request.
* The roster version is chosen **by the request** (`--record-date`). A later
  registration in the database never replaces it.

### Proposals — `--proposals`

```json
{"meeting_id":"M","proposal_id":"Q","title":"Elect director","options":["X","Y","ABSTAIN"]}
```

`options` must be a non-empty list of unique strings. A ballot's `choice` must
be one of its proposal's options.

### Grants — `--grants`

```json
{"meeting_id":"M","grant_id":"G1","position_id":"P","grantor":"H","grantee":"A",
 "unit_start":20,"unit_end":80,"proposal_ids":["Q"],
 "effective_at":"2026-03-02T00:00:00Z","revoked_at":null,"parent_grant_id":null}
```

* All ranges are **half-open** integer intervals inside `[0, shares)`.
* `proposal_ids` is a non-empty set of proposals the proxy may vote.
* `effective_at` / `revoked_at` are RFC 3339 with an explicit offset. See §3
  for the exact cutoff test.
* `parent_grant_id` chains sub-delegation. A root grant (`null`) must be issued
  by the position holder; a child grant must be issued by its parent's
  `grantee`, lie inside the parent's range, and name no proposal the parent
  does not name.

### Ballots — `--ballots`

```json
{"meeting_id":"M","ballot_id":"B1","position_id":"P","unit_start":20,"unit_end":80,
 "voter":"A","proposal_id":"Q","choice":"X","submitted_at":"2026-03-04T00:00:00Z"}
```

A ballot is valid only within the `voter`'s live authority for that proposal
at the cutoff; see §5.

### Request-selected meeting version

`meeting_id`, `record_date` and `cutoff_at` are **request arguments**, not
inferred from any file or from the database. The same four files reconciled at
a different record date produce a different stored reconciliation; neither
overwrites the other, and a query by `--reconciliation-id` always returns the
version it was registered with.

---

## 2. Running it

### Command line

```bash
# Reconcile four JSONL files for the request's meeting version; writes nothing.
python3 tools/obsdeps_cli.py proxyvote reconcile \
    --meeting-id M --record-date 2026-03-01 --cutoff-at 2026-03-05T00:00:00Z \
    --roster roster.jsonl --proposals proposals.jsonl \
    --grants grants.jsonl --ballots ballots.jsonl > result.json

# Store the result immutably and query it later (fresh process, no source files).
python3 tools/obsdeps_cli.py proxyvote store --db proxyvote.sqlite3 \
    --meeting-id M --record-date 2026-03-01 --cutoff-at 2026-03-05T00:00:00Z \
    --roster roster.jsonl --proposals proposals.jsonl \
    --grants grants.jsonl --ballots ballots.jsonl

python3 tools/obsdeps_cli.py proxyvote list     --db proxyvote.sqlite3 --meeting-id M
python3 tools/obsdeps_cli.py proxyvote summary  --db proxyvote.sqlite3 \
    --reconciliation-id pv-... [--meeting-id M]
python3 tools/obsdeps_cli.py proxyvote proposal --db proxyvote.sqlite3 \
    --reconciliation-id pv-... --proposal-id Q
python3 tools/obsdeps_cli.py proxyvote position --db proxyvote.sqlite3 \
    --reconciliation-id pv-... --position-id P
python3 tools/obsdeps_cli.py proxyvote evidence --db proxyvote.sqlite3 \
    --reconciliation-id pv-... [--proposal-id Q] [--position-id P]
```

Exit codes: `0` the reconciliation is complete; `2` it ran and produced
locatable results but the input is knowingly incomplete (at least one position
was quarantined by an input error — good positions are still printed); `1`
usage or unreadable-document error.

### Python API

```python
from obsdeps.proxyvote import ProxyVoteApi

# File mode: nothing is written, inputs are never modified.
with ProxyVoteApi() as api:
    result = api.reconcile_files(
        meeting_id="M", record_date="2026-03-01",
        cutoff_at="2026-03-05T00:00:00Z",
        roster="roster.jsonl", proposals="proposals.jsonl",
        grants="grants.jsonl", ballots="ballots.jsonl")

# Stored, immutable mode; queries re-read the stored document only.
with ProxyVoteApi("proxyvote.sqlite3") as api:
    outcome = api.register(...)
    api.summary(reconciliation_id=outcome["reconciliation_id"])
    api.proposal(proposal_id="Q", reconciliation_id=outcome["reconciliation_id"])
    api.position(position_id="P", reconciliation_id=outcome["reconciliation_id"])
    api.evidence(proposal_id="Q", reconciliation_id=outcome["reconciliation_id"])
```

`reconcile_documents()` accepts Python row lists, JSONL strings or file paths
for the four documents — handy for tests and for the independent enumerator.

### Worked, hand-checked example

```bash
python3 examples/run_proxyvote_example.py
```

The expectations are computed on paper in the script's docstring and asserted
there; they are not generated by the engine.

---

## 3. Half-open ranges and cutoff-time authority

Every share range is `[start, end)`: the share at `start` belongs to it, the
share at `end` does not. The position's universe is `[0, shares)`, so
consecutive segments tile it with no gap and no double count.

A grant is **live at the cutoff** exactly when

```
effective_at <= cutoff_at  and  (revoked_at is null or cutoff_at < revoked_at)
```

* The `effective_at` instant **is part of** the grant: a grant effective
  exactly at the cutoff counts.
* The `revoked_at` instant is **not** part of the grant: a grant revoked
  exactly at the cutoff does not count.
* The record date does **not** gate grants. It fixes which shares exist; a
  proxy signed after the record date but effective before the cutoff is valid.
  Requiring the proxy to pre-date the record date is wrong.
* A grant whose parent is not live is itself not live (`live_basis =
  parent-not-live`), so a mid-way revocation cannot carry authority to the end.

The engine reports each grant's `live_at_cutoff` and `live_basis` (`live`,
`not-yet-effective`, `revoked`, `parent-not-live`) as evidence.

---

## 4. Ownership and why conflicts are local

For each proposal, only grants that list that proposal convey authority.
Starting from the position's holder, a unit is passed to a grantee when a live
grant by the current authority covers it. Because grant ids are unique and
every chain is validated, **each unit is owned at most once** in the normal
case, and the owner plus its grant path become the segment's evidence.

Contested is deliberately local:

* Two live chains covering the same units (for example H→A and a separate
  H→C over `[35,50)`, or the same range delegated twice by H to A through
  two grants) make **only the intersection** contested. `[0,35)` still
  belongs to A, `[50,80)` still to B, and `[80,100)` to H. The segment lists
  every competing `(voter, path)` in `alternatives`, so the overlap is never
  double-counted and never becomes a whole-account rejection.
* A voter's ballots with different choices over the same units make only the
  ballot intersection contested; identical duplicate submissions count once
  and keep every source `ballot_id`.
* An out-of-authority ballot part is `invalid` for exactly those units; the
  valid part still counts where it lies, and nothing widens or moves authority.

A contested or invalid span never blocks the rest of the position, and never
turns into a whole-account rejection.

---

## 5. Ballots

For a proposal, each ballot is clipped to the union of the voter's owned units
for that proposal:

* submitted strictly after the cutoff → void; its units are reported as
  `invalid` and its id is listed with code `submitted-after-cutoff`;
* submitted at or before the cutoff but partly outside authority → the inside
  part counts and the outside part is `invalid` (code `outside-authority`);
* at most one choice per share per proposal. Identical repeats collapse to one
  vote while retaining all ballot ids; differing choices over the same shares
  are contested on their intersection only.

At every share index and proposal exactly one outcome is derived: a choice, an
`abstain` (no ballot covered that unit), or `contested`. Therefore

```
by_choice[X] + by_choice[Y] + ... + abstain + contested == shares
```

for every position/proposal, and `invalid` is reported separately because those
units are not part of the valid tally. `invalid_ballot_count` counts distinct ballots that were wholly or partly
rejected, while `invalid` counts rejected *submitted* units: if the same
overshooting ballot is submitted twice, both rejections are reported (there is
no way to know the two submissions were "the same" for rejection purposes),
even though identical in-authority repeats count as one vote.

---

## 6. Output

```json
{
  "engine_version": "1.0.0",
  "meeting_id": "M",
  "roster_version": {"record_date": "2026-03-01", "records_rejected": 0,
                     "sha256": "..."},
  "record_date": "2026-03-01",
  "cutoff_at": "2026-03-05T00:00:00Z",
  "cutoff_utc": "2026-03-05T00:00:00Z",
  "complete": true,
  "summary": {"positions": 1, "incomplete_positions": 0,
              "proposals": 1, "grants": 2, "ballots": 3},
  "positions": [ ... ],
  "proposals": [ ... ],
  "problems": [ ... ],
  "sources": {
    "roster":    {"path": "...", "sha256": "...", "bytes": 131, "records": 1},
    "proposals": {"path": "...", "sha256": "...", "bytes": 89,  "records": 1},
    "grants":    {"path": "...", "sha256": "...", "bytes": 466, "records": 2},
    "ballots":   {"path": "...", "sha256": "...", "bytes": 549, "records": 3}
  }
}
```

Per position, per proposal:

```json
{
  "proposal_id": "Q",
  "by_choice": {"ABSTAIN": 0, "X": 100, "Y": 0},
  "abstain": 0, "contested": 0, "invalid": 90,
  "invalid_ballot_count": 2,
  "total": 100,
  "segments": [
    {"position_id": "P", "proposal_id": "Q",
     "unit_start": 0, "unit_end": 20, "units": 20,
     "outcome": "X", "voter": "H", "authority_path": [],
     "alternatives": [], "ballot_ids": ["BH"]}
  ],
  "invalid_ballots": [
    {"ballot_id": "BA", "voter": "A", "choice": "X",
     "code": "outside-authority", "range": [20, 80],
     "outside": [[40, 70]], "units": 30, "submitted_at": "..."}
  ]
}
```

Positions also carry every grant with its range, proposal set, times, parent,
`live_at_cutoff`, `live_basis` and structural `path`. Segments are sorted and
non-overlapping; `sum(units)` equals the position's `shares` for each
proposal.

---

## 7. Problems and completeness

Row problems are collected with a stable code and a locator that names the
document line, and (where known) `details.position_id`. A position with any
grant-graph input error is quarantined (`status: "incomplete"`, no totals),
which sets `complete: false` for the run; every other position still
reconciles and stays locatable. A position with only ballot-level rejections
(`outside-authority`, `submitted-after-cutoff`) stays `reconciled` and the run
stays complete — those are adjudication outcomes, not input errors.

| code | meaning |
| --- | --- |
| `source-unreadable` / `jsonl-malformed` | a whole document cannot be read (raises `ProxyVoteInputError`) |
| `row-not-object` / `row-missing-field` / `row-wrong-type` | malformed JSONL row |
| `row-unknown-meeting` | row's `meeting_id` differs from the request |
| `row-duplicate-position` / `row-duplicate-proposal` | repeated id |
| `position-shares-invalid` | `shares` not a positive integer |
| `proposal-options-invalid` / `proposal-options-duplicate` | bad/duplicate options |
| `grant-duplicate-id` | repeated `grant_id` within a position |
| `grant-unit-invalid` | empty/inverted/negative range |
| `grant-time-invalid` | unparseable time or `revoked_at <= effective_at` |
| `grant-empty-proposals` / `grant-duplicate-proposal` | empty or repeated proposal set |
| `grant-unknown-proposal` | grant names a proposal the meeting does not declare |
| `grant-unknown-position` / `ballot-unknown-position` | row names a position absent from the roster |
| `grant-parent-unknown` | `parent_grant_id` not declared in this position |
| `grant-grantor-mismatch` | root grantor ≠ holder, or child grantor ≠ parent grantee |
| `grant-exceeds-parent` / `grant-proposals-exceed-parent` | child outside parent |
| `grant-exceeds-position` | grant outside `[0, shares)` |
| `grant-cycle` | `parent_grant_id` chain revisits a grant |
| `ballot-unit-invalid` / `ballot-time-invalid` | bad ballot range or time |
| `ballot-choice-invalid` | choice not among the proposal's options |
| `ballot-outside-authority` / `ballot-after-cutoff` | ballot part/wholly rejected (not an input error) |
| `authority-overlap` | two live grant chains claim the same units (contested, not an error) |
| `roster-empty` | the requested roster version declares no positions |

---

## 8. Determinism, storage and reruns

* The result document is canonicalized by sorting every dictionary, every
  position, proposal and segment list. Two runs over byte-identical inputs
  produce byte-identical JSON.
* Input files are opened read-only and never modified; the test suite verifies
  this by digest.
* `proxyvote store` writes an append-only `proxyvote_reconciliations` row plus
  `proxyvote_position_results` / `proxyvote_proposal_results`; SQLite triggers
  refuse `UPDATE` and `DELETE`. The `reconciliation_id` is a SHA-256 of the
  request (meeting, record date, cutoff) and the four source digests, so
  re-running identical inputs is a no-op (`already-registered`).
* Queries (`summary`, `proposal`, `position`, `evidence`) read the stored
  document; they never recompute and never consult a source file, so a fresh
  process with only the database returns exactly what was registered.

How to reproduce the sample:

```bash
python3 examples/run_proxyvote_example.py
cd tests && PYTHONPATH=../src python3 -m unittest \
    test_proxyvote_handworked test_proxyvote_property \
    test_proxyvote_validation test_proxyvote_cli test_proxyvote_scale
```

---

## 9. Complexity: intervals, not shares

The engine never iterates over individual shares. Ownership is partitioned at
the distinct grant endpoints; ballots are partitioned at ballot endpoints; the
outcome is swept once over the union of those boundaries. Runtime therefore
grows with the number of input ranges and their endpoints, independent of
`shares`.

The scale test (`tests/test_proxyvote_scale.py`) runs the real CLI over
100,000 disjoint grant intervals with `shares = 10^12` and checks that the
result equals hand-derived totals (`YES = 200`, `abstain = 10^12 - 200`,
`segments = 100,001`). On the reference machine the run takes about 12 s of
wall time and roughly 0.4 GB peak RSS, and a second run with a
million-times-smaller share universe is in the same time class — an
enumerating implementation could not finish either. The boundary is the
interval count: 100,001 segments come from 100,000 grants plus the uncovered
tail, regardless of how many shares those intervals span.

Delegation depth is similarly bounded by the input: a legal 3,000-level
re-delegation chain works without recursion limits, and cycle detection is an
iterative linear pass rather than a per-grant ancestor walk.

The independent per-share enumerator in `tests/proxyvote_enumerator.py`
deliberately *does* iterate every share and every ballot, but only on small
samples, to cross-check the production engine. The production engine never
imports it.

---

## 10. Limits

* Standard-library JSON only; no comments or trailing commas in the JSONL.
* Times are RFC 3339 with an explicit offset; naive timestamps are refused.
* A position with any structural grant error is not tallied at all; there is
  no partial "best effort" tally for a position whose authority graph is
  untrustworthy. Other positions are unaffected.
* The roster, proposals, grants and ballots must all belong to the requested
  `meeting_id`; the local file layout is otherwise irrelevant.

"""Hand-worked tests for the charging-station collector.

Expectations are stated as paper facts derived from the explicit fixtures in
``stations_fixtures`` (timestamps, counts, page order, locator strings), never
copied from engine output. Each scenario maps to one requirement in
docs/STATIONS.md.
"""

from __future__ import annotations

import unittest

from support import make_api  # noqa: F401  (import path bootstrap only)
from obsdeps.stations import StationApi, StationConfigError, CONFLICT_STATUS
from obsdeps.stations.engine import collect_sources
from obsdeps.stations.transport import MockTransport

import stations_fixtures as fx


def collect(transport=None, sources=None):
    api = StationApi(":memory:")
    result = api.collect(
        sources or fx.main_sources(),
        transport=transport or fx.main_transport(),
        started_at="2026-11-16T00:00:00Z")
    return result, api


class PagingAndCursors(unittest.TestCase):
    def test_same_cursor_text_advances_each_provider_independently(self):
        transport = fx.main_transport()
        result = collect_sources(fx.main_sources(), transport)
        # Both providers' second page uses the literal cursor "cur-a"; the
        # request log proves each provider's cursor was sent on its own request.
        log = transport.request_log
        alpha_log = [c for (p, c, _seq) in log if p == "ALPHA"]
        beta_log = [c for (p, c, _seq) in log if p == "BETA"]
        self.assertEqual(alpha_log, [None, "cur-a", "cur-b"])
        self.assertEqual(beta_log, [None, "cur-a"])
        # Page order and cursor provenance are stored per provider.
        alpha_run = next(s for s in result.sources if s.provider_id == "ALPHA")
        beta_run = next(s for s in result.sources if s.provider_id == "BETA")
        self.assertEqual(
            [(p.page_seq, p.request_cursor, p.next_cursor)
             for p in alpha_run.pages],
            [(1, None, "cur-a"), (2, "cur-a", "cur-b"),
             (3, "cur-b", None)])
        self.assertEqual(
            [(p.page_seq, p.request_cursor, p.next_cursor)
             for p in beta_run.pages],
            [(1, None, "cur-a"), (2, "cur-a", None)])
        self.assertTrue(all(s.status == "complete" for s in result.sources))


class CurrentStateSemantics(unittest.TestCase):
    def setUp(self):
        self.summary, self.api = collect()

    def tearDown(self):
        self.api.close()

    def test_newer_observation_wins_when_older_arrives_later(self):
        # ALPHA A1: 10:00 OPEN on page 1, then 06:00 CLOSED on page 2.
        station = self.api.get_station("ALPHA", "A1")
        self.assertEqual(station["status"], "OPEN")
        self.assertEqual(station["latest_observed_at"],
                         "2024-05-01T10:00:00Z")
        self.assertFalse(station["conflict"])
        # The superseded older record is still retained as history.
        history = station["observation_history"]
        self.assertEqual([o["observed_at"] for o in history],
                         ["2024-05-01T06:00:00Z", "2024-05-01T10:00:00Z"])
        self.assertEqual([o["status"] for o in history], ["CLOSED", "OPEN"])
        # And only the newest record backs the derived current value.
        self.assertEqual(len(station["evidence"]), 1)
        self.assertEqual(station["evidence"][0]["observed_at"],
                         "2024-05-01T10:00:00Z")
        self.assertEqual(station["evidence"][0]["raw_locator"],
                         "ALPHA/page1/stations[0]")

    def test_same_timestamp_contradiction_marks_conflict(self):
        station = self.api.get_station("ALPHA", "A6")
        self.assertEqual(station["status"], CONFLICT_STATUS)
        self.assertTrue(station["conflict"])
        self.assertIn("same-timestamp-state-conflict",
                      station["conflict_reasons"])
        self.assertEqual(station["latest_observed_at"],
                         "2024-05-01T12:00:00Z")
        # Both tied records are kept as evidence; no winner was invented.
        evidence = station["evidence"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual({e["status"] for e in evidence}, {"OPEN", "CLOSED"})
        self.assertEqual({e["raw_locator"] for e in evidence},
                         {"ALPHA/page2/stations[1]",
                          "ALPHA/page3/stations[0]"})

    def test_canonical_id_merges_providers_but_keeps_separate_evidence(self):
        group = self.api.get_canonical_group("CAN-RIVERSIDE")
        self.assertEqual(group["member_count"], 2)
        identities = {(m["provider_id"], m["station_id"])
                      for m in group["members"]}
        self.assertEqual(identities, {("ALPHA", "A1"), ("BETA", "B1")})
        for member in group["members"]:
            self.assertTrue(member["evidence"])
            providers = {e["provider_id"] for e in member["evidence"]}
            self.assertEqual(providers, {member["provider_id"]})

    def test_missing_canonical_never_merges_equal_name_and_address(self):
        # ALPHA A2 vs BETA B2 and A4 vs B3 share name+address, no canonical id.
        a2 = self.api.get_station("ALPHA", "A2")
        b2 = self.api.get_station("BETA", "B2")
        a4 = self.api.get_station("ALPHA", "A4")
        b3 = self.api.get_station("BETA", "B3")
        for station in (a2, b2, a4, b3):
            self.assertIsNone(station["canonical_id"])
        self.assertEqual(a2["name"], b2["name"])
        self.assertEqual(a2["address"], b2["address"])
        self.assertNotEqual(a2["station_key"], b2["station_key"])
        self.assertEqual(a4["name"], b3["name"])
        self.assertEqual(a4["address"], b3["address"])
        self.assertNotEqual(a4["station_key"], b3["station_key"])
        # Both remain independently queryable as distinct current rows.
        keys = {(s["provider_id"], s["station_id"])
                for s in self.api.list_stations()}
        self.assertEqual(keys, set(fx.EXPECTED["current_station_keys"]))

    def test_contradictory_canonical_ids_keep_conflict_evidence(self):
        station = self.api.get_station("ALPHA", "A8")
        self.assertTrue(station["conflict"])
        self.assertIn("canonical-id-conflict", station["conflict_reasons"])
        self.assertIsNone(station["canonical_id"])  # cannot choose one
        conflicts = station["canonical_conflicts"]
        self.assertEqual({c["canonical_id"] for c in conflicts},
                         {"CAN-FLIP-1", "CAN-FLIP-2"})
        self.assertTrue(all(c["raw_locator"] for c in conflicts))
        # The contradictory records still exist as observations.
        self.assertEqual(len(station["observation_history"]), 2)

    def test_canonical_conflict_spread_across_runs_still_evidenced(self):
        from obsdeps.stations import MockTransport
        base = dict(name="Cross", address="1 Cross Rd", lat=1.0, lon=1.0,
                    status="OPEN",
                    connectors=[{"type": "CCS", "available": 1, "total": 2}])
        first = {"C": {"": fx.v1_page([
            fx.v1_station("C1", observed_at="2024-05-01T10:00:00Z",
                          canonical_id="CAN-X", **base)], None)}}
        second = {"C": {"": fx.v1_page([
            fx.v1_station("C1", observed_at="2024-05-02T10:00:00Z",
                          canonical_id="CAN-Y", **base)], None)}}
        sources = [{"provider_id": "C", "endpoint": "mock://c",
                    "adapter_type": "shell_v1"}]
        with StationApi(":memory:") as api:
            api.collect(sources, transport=MockTransport(first),
                        run_id="run-1")
            api.collect(sources, transport=MockTransport(second),
                        run_id="run-2")
            station = api.get_station("C", "C1")
            self.assertIn("canonical-id-conflict",
                          station["conflict_reasons"])
            self.assertIsNone(station["canonical_id"])
            variants = {v["canonical_id"]
                        for v in station["canonical_variants"]}
            self.assertEqual(variants, {"CAN-X", "CAN-Y"})
            # Each variant points at the run and row that supplied it.
            self.assertEqual({v["run_id"]
                              for v in station["canonical_variants"]},
                             {"run-1", "run-2"})


class RecordValidation(unittest.TestCase):
    def test_bad_counts_and_coordinates_reject_only_that_record(self):
        result = collect_sources(fx.main_sources(), fx.main_transport())
        problems = [p for run in result.sources
                    for p in run.record_problems]
        locators = sorted(p.locator for p in problems)
        self.assertEqual(locators,
                         sorted(fx.EXPECTED["alpha_rejected_locators"]
                                + fx.EXPECTED["beta_rejected_locators"]))
        # The valid rows on the same pages were still accepted.
        alpha_run = next(s for s in result.sources if s.provider_id == "ALPHA")
        self.assertEqual(
            [s.station_id for s in alpha_run.stations],
            fx.EXPECTED["alpha_accepted_station_ids"])
        beta_run = next(s for s in result.sources if s.provider_id == "BETA")
        self.assertEqual(sorted(s.station_id for s in beta_run.stations),
                         ["B1", "B2", "B3", "B6"])
        # Problem codes and messages carry the offending field and row path.
        by_locator = {p.locator: p for p in problems}
        self.assertEqual(
            by_locator["ALPHA/page1/stations[2]"]
            .details["field"], "connectors.available/total")
        self.assertEqual(
            by_locator["ALPHA/page1/stations[2]"]
            .details["connector_index"], 0)
        self.assertEqual(
            by_locator["BETA/page1/data.sites[3]"]
            .details["field"], "location.geo.latitude/longitude")
        self.assertEqual(
            by_locator["BETA/page1/data.sites[4]"]
            .details["field"], "connector_counts.free/count")

    def test_each_adapter_uses_its_own_field_paths(self):
        result = collect_sources(fx.main_sources(), fx.main_transport())
        alpha = next(s for s in result.sources if s.provider_id == "ALPHA")
        beta = next(s for s in result.sources if s.provider_id == "BETA")
        # v2 boolean operational is translated into the shared status vocab.
        b1 = next(s for s in beta.stations if s.station_id == "B1")
        self.assertEqual(b1.status, "OPEN")
        b2 = next(s for s in beta.stations if s.station_id == "B2")
        self.assertEqual(b2.status, "CLOSED")
        # v2 free/count and v1 available/total both land as (available, total).
        self.assertEqual(b1.connectors, [("CCS", 4, 8)])
        a1 = next(s for s in alpha.stations
                  if s.station_id == "A1"
                  and s.observed_at == "2024-05-01T10:00:00Z")
        self.assertEqual(a1.connectors, [("CCS", 2, 4), ("CHAdeMO", 1, 2)])


class FailureIsolation(unittest.TestCase):
    def test_cursor_cycle_stops_source_and_marks_it_incomplete(self):
        result = collect_sources(fx.cycle_sources(),
                                 MockTransport(fx.gamma_cycle_fixture()))
        run = result.sources[0]
        self.assertEqual(run.provider_id, "GAMMA")
        self.assertEqual(run.status, "incomplete")
        # Both fetched pages were processed; the third request is never made,
        # so the feed cannot loop forever even though page 2 repeated a cursor.
        self.assertEqual(len(run.pages), 2)
        self.assertEqual(run.page_problems[0].code, "page-cursor-cycle")
        self.assertEqual(run.page_problems[0].details["cursor"], "loop")
        # The pages that did arrive remain usable, including the cycling page.
        self.assertEqual([s.station_id for s in run.stations], ["G1", "G2"])

    def test_bad_envelope_is_located_page_structure_error(self):
        result = collect_sources(fx.bad_envelope_sources(),
                                 MockTransport(fx.delta_bad_envelope_fixture()))
        run = result.sources[0]
        self.assertEqual(run.status, "incomplete")
        self.assertEqual(run.page_problems[0].code, "page-structure-error")
        self.assertEqual(run.page_problems[0].locator, "DELTA/page1")
        self.assertEqual(run.page_problems[0].details["page_seq"], 1)

    def test_transport_error_reports_provider_page_and_cursor(self):
        result = collect_sources(fx.transport_error_sources(),
                                 MockTransport(
                                     fx.epsilon_transport_error_fixture()))
        run = result.sources[0]
        self.assertEqual(run.status, "incomplete")
        problem = run.page_problems[0]
        self.assertEqual(problem.code, "page-transport-error")
        self.assertEqual(problem.locator, "EPSILON/page2")
        self.assertEqual(problem.details["cursor"], "vanished")
        self.assertEqual(problem.details["page_seq"], 2)

    def test_failing_source_does_not_block_other_sources_in_batch(self):
        fixture = {**fx.gamma_cycle_fixture(), **fx.main_fixture()}
        sources = fx.cycle_sources() + fx.main_sources()
        with StationApi(":memory:") as api:
            summary = api.collect(sources, transport=MockTransport(fixture),
                                  started_at="2026-11-16T00:00:00Z")
            self.assertEqual(summary["sources"], 3)
            self.assertEqual(summary["complete"], 2)
            self.assertEqual(summary["incomplete"], 1)
            # Healthy providers remain fully queryable.
            self.assertIsNotNone(api.get_station("BETA", "B6"))
            self.assertEqual(
                api.get_station("ALPHA", "A1")["status"], "OPEN")
            # The cycled provider kept the page it managed to read.
            self.assertEqual(
                api.get_station("GAMMA", "G1")["status"], "OPEN")
            run_detail = next(r for r in api.list_runs())
            detail = api.get_run_detail(run_detail["run_id"])
            statuses = {s["provider_id"]: s["status"]
                        for s in detail["sources"]}
            self.assertEqual(statuses["GAMMA"], "incomplete")
            self.assertEqual(statuses["ALPHA"], "complete")
            self.assertEqual(statuses["BETA"], "complete")

    def test_page_cap_stops_an_endless_feed_as_incomplete(self):
        # List-backed mock: each body points at a fresh cursor, so without the
        # cap the feed would run until the fixture ran out. The cap must stop
        # it deterministically and label the source incomplete, not complete.
        pages = [
            fx.v1_page([
                fx.v1_station(f"L{seq}", name="Long", address="1 Long Rd",
                              lat=1.0, lon=1.0,
                              observed_at="2024-05-01T10:00:00Z",
                              status="OPEN")],
                       next_cursor=f"fresh-{seq}")
            for seq in range(5)
        ]
        transport = MockTransport({"LONG": pages})
        result = collect_sources(
            [{"provider_id": "LONG", "endpoint": "mock://long",
              "adapter_type": "shell_v1"}],
            transport, max_pages=3)
        run = result.sources[0]
        self.assertEqual(run.status, "incomplete")
        self.assertEqual(len(run.pages), 3)
        self.assertEqual(run.page_problems[0].code, "page-limit-exceeded")
        # The request log proves the cap, not fixture exhaustion, stopped it.
        self.assertEqual(
            [c for (p, c, _s) in transport.request_log if p == "LONG"],
            [None, "fresh-0", "fresh-1"])


class DeterminismAndStore(unittest.TestCase):
    def test_rerun_same_input_gives_identical_run_and_state(self):
        transport_a = fx.main_transport()
        transport_b = fx.main_transport()
        with StationApi(":memory:") as api:
            first = api.collect(fx.main_sources(), transport=transport_a,
                                started_at="2026-11-16T00:00:00Z")
            second = api.collect(fx.main_sources(), transport=transport_b,
                                 started_at="2026-11-16T00:00:00Z")
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(first["input_fingerprint"],
                             second["input_fingerprint"])
            # One run row, not duplicates; derived state unchanged.
            self.assertEqual(len(api.list_runs()), 1)
            stations_once = api.list_stations()
            third = api.collect(fx.main_sources(),
                                transport=fx.main_transport(),
                                started_at="2026-11-16T00:00:00Z")
            self.assertEqual(third["run_id"], first["run_id"])
            self.assertEqual(api.list_stations(), stations_once)

    def test_history_accumulates_across_distinct_runs(self):
        with StationApi(":memory:") as api:
            api.collect(fx.main_sources(), transport=fx.main_transport(),
                        run_id="run-manual-1",
                        started_at="2026-11-16T00:00:00Z")
            api.collect(fx.main_sources(), transport=fx.main_transport(),
                        run_id="run-manual-2",
                        started_at="2026-11-16T01:00:00Z")
            self.assertEqual({r["run_id"] for r in api.list_runs()},
                             {"run-manual-1", "run-manual-2"})
            station = api.get_station("ALPHA", "A1")
            self.assertEqual({o["run_id"]
                              for o in station["observation_history"]},
                             {"run-manual-1", "run-manual-2"})

    def test_bad_source_configuration_is_refused_before_requests(self):
        transport = fx.main_transport()
        with self.assertRaises(StationConfigError):
            collect_sources([], transport)
        with self.assertRaises(StationConfigError):
            collect_sources([{"provider_id": "X", "endpoint": "u",
                              "adapter_type": "unknown-shell"}], transport)
        with self.assertRaises(StationConfigError):
            collect_sources([{"provider_id": "ALPHA", "endpoint": "u",
                              "adapter_type": "shell_v1"},
                             {"provider_id": "ALPHA", "endpoint": "v",
                              "adapter_type": "shell_v2"}], transport)
        self.assertEqual(transport.request_log, [])


class ExtraDerivationCases(unittest.TestCase):
    def test_identical_same_timestamp_duplicates_are_not_conflicts(self):
        from obsdeps.stations import MockTransport
        row = fx.v1_station(
            "D1", name="Dup", address="1 Dup Rd", lat=1.0, lon=1.0,
            observed_at="2024-05-01T10:00:00Z", status="OPEN",
            connectors=[{"type": "CCS", "available": 1, "total": 2}])
        fixture = {"DUP": {"": fx.v1_page([row, dict(row)], None)}}
        sources = [{"provider_id": "DUP", "endpoint": "mock://dup",
                    "adapter_type": "shell_v1"}]
        with StationApi(":memory:") as api:
            api.collect(sources, transport=MockTransport(fixture),
                        started_at="2026-11-16T00:00:00Z")
            station = api.get_station("DUP", "D1")
            self.assertFalse(station["conflict"])
            self.assertEqual(station["status"], "OPEN")
            # Both identical records back the value; no winner was invented.
            self.assertEqual(station["evidence_count"], 2)

    def test_newer_observation_in_a_later_run_replaces_current_state(self):
        from obsdeps.stations import MockTransport
        old = {"R": {"": fx.v1_page([
            fx.v1_station("R1", name="Run", address="1 Run Rd", lat=1.0,
                          lon=1.0, observed_at="2024-05-01T10:00:00Z",
                          status="CLOSED",
                          connectors=[{"type": "CCS", "available": 0,
                                       "total": 2}])], None)}}
        new = {"R": {"": fx.v1_page([
            fx.v1_station("R1", name="Run", address="1 Run Rd", lat=1.0,
                          lon=1.0, observed_at="2024-05-02T10:00:00Z",
                          status="OPEN",
                          connectors=[{"type": "CCS", "available": 2,
                                       "total": 2}])], None)}}
        sources = [{"provider_id": "R", "endpoint": "mock://r",
                    "adapter_type": "shell_v1"}]
        with StationApi(":memory:") as api:
            api.collect(sources, transport=MockTransport(old),
                        run_id="run-old")
            self.assertEqual(api.get_station("R", "R1")["status"], "CLOSED")
            api.collect(sources, transport=MockTransport(new),
                        run_id="run-new")
            station = api.get_station("R", "R1")
            self.assertEqual(station["status"], "OPEN")
            self.assertEqual(station["latest_observed_at"],
                             "2024-05-02T10:00:00Z")
            # The current evidence points only at the newest run's record.
            self.assertEqual({e["run_id"] for e in station["evidence"]},
                             {"run-new"})
            # The older run's observation remains in the history.
            self.assertEqual({o["run_id"]
                              for o in station["observation_history"]},
                             {"run-old", "run-new"})


if __name__ == "__main__":
    unittest.main()

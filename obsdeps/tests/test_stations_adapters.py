"""Unit tests for adapter-specific field paths and shared value validation."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from support import make_api  # noqa: F401  (import path bootstrap only)
from obsdeps.stations import adapters
from obsdeps.stations.adapters import (
    check_connector, check_coordinates, parse_observed_at,
    parse_page, parse_page_shell_v1, parse_page_shell_v2,
)
from obsdeps.stations.engine import collect_sources
from obsdeps.stations.errors import TransportError
from obsdeps.stations.transport import HttpTransport


class SharedValueValidation(unittest.TestCase):
    def test_timestamp_normalizes_offsets_and_z_suffix(self):
        text, sort_key = parse_observed_at("2024-05-01T10:00:00Z")
        self.assertEqual(text, "2024-05-01T10:00:00Z")
        self.assertEqual(sort_key, "2024-05-01T10:00:00Z")
        # An offset instant converts to the same UTC text.
        text, _ = parse_observed_at("2024-05-01T06:00:00-04:00")
        self.assertEqual(text, "2024-05-01T10:00:00Z")
        self.assertIsNone(parse_observed_at("2024-05-01T10:00:00")[0])
        self.assertIsNone(parse_observed_at("not-a-timestamp")[0])
        self.assertIsNone(parse_observed_at("")[0])
        self.assertIsNone(parse_observed_at(None)[0])

    def test_coordinate_ranges(self):
        self.assertIsNone(check_coordinates(0, 0))
        self.assertIsNone(check_coordinates(-90, 180))
        self.assertIsNone(check_coordinates(90, -180))
        self.assertIn("latitude", check_coordinates(90.01, 0))
        self.assertIn("longitude", check_coordinates(0, -180.01))
        self.assertIn("number", check_coordinates("10", 0))
        # Booleans are not valid coordinates even though bool is an int subclass.
        self.assertIn("number", check_coordinates(True, 0))

    def test_connector_ranges(self):
        self.assertIsNone(check_connector(0, 0))
        self.assertIsNone(check_connector(2, 2))
        self.assertIn("negative", check_connector(0, -1))
        self.assertIn("negative", check_connector(-1, 2))
        self.assertIn("exceed", check_connector(3, 2))
        self.assertIn("integer", check_connector(1.0, 2))
        self.assertIn("integer", check_connector(True, 2))


class ShellV1Adapter(unittest.TestCase):
    def _valid(self, **overrides):
        row = {
            "id": "S1", "name": "N", "address": "A",
            "lat": 1.0, "lon": 2.0,
            "observed_at": "2024-05-01T10:00:00Z", "status": "OPEN",
            "connectors": [{"type": "CCS", "available": 1, "total": 2}],
        }
        row.update(overrides)
        return row

    def test_valid_row_normalized(self):
        page = parse_page_shell_v1(
            {"stations": [self._valid()], "next_cursor": "c"},
            provider_id="P1", page_seq=1)
        self.assertEqual(len(page.stations), 1)
        station = page.stations[0]
        self.assertEqual((station.provider_id, station.station_id,
                          station.status, station.canonical_id),
                         ("P1", "S1", "OPEN", None))
        self.assertEqual(station.connectors, [("CCS", 1, 2)])
        self.assertEqual(station.raw_locator, "P1/page1/stations[0]")
        self.assertEqual(page.next_cursor, "c")

    def test_record_errors_keep_sibling_rows(self):
        page = parse_page_shell_v1(
            {"stations": [
                self._valid(id="good"),
                self._valid(id="bad", lat=200),
                self._valid(id="good2",
                            connectors=[{"type": "T", "available": 5,
                                         "total": 1}]),
            ]}, provider_id="P1", page_seq=2)
        self.assertEqual([s.station_id for s in page.stations],
                         ["good"])
        self.assertEqual(len(page.rejected), 2)
        self.assertEqual(page.rejected[0].locator,
                         "P1/page2/stations[1]")
        self.assertEqual(page.rejected[1].locator,
                         "P1/page2/stations[2]")

    def test_missing_required_fields_and_bad_types(self):
        cases = [
            self._valid(id=" "),
            self._valid(name=123),
            self._valid(status=None),
            self._valid(observed_at="2024-05-01"),
            self._valid(canonical_id=""),
            self._valid(connectors=[]),
            self._valid(connectors=[{"type": "T", "available": 0,
                                     "total": 1},
                                    {"type": "T", "available": 0,
                                     "total": 1}]),
            "not-an-object",
        ]
        page = parse_page_shell_v1(
            {"stations": cases}, provider_id="P1", page_seq=1)
        self.assertEqual(page.stations, [])
        self.assertEqual(len(page.rejected), len(cases))

    def test_envelope_structure_errors_raise(self):
        with self.assertRaises(ValueError):
            parse_page_shell_v1([], provider_id="P1", page_seq=1)
        with self.assertRaises(ValueError):
            parse_page_shell_v1({"wrong": []}, provider_id="P1", page_seq=1)
        with self.assertRaises(ValueError):
            parse_page_shell_v1({"stations": [], "next_cursor": 7},
                                provider_id="P1", page_seq=1)


class ShellV2Adapter(unittest.TestCase):
    def _valid(self, **overrides):
        row = {
            "site_id": "T1", "site_name": "N",
            "location": {"address": "A",
                         "geo": {"latitude": 1.0, "longitude": 2.0}},
            "last_updated": "2024-05-01T10:00:00Z",
            "operational": True,
            "connector_counts": [
                {"connector_type": "CCS", "free": 1, "count": 2}],
        }
        row.update(overrides)
        return row

    def test_boolean_operational_maps_to_status_vocab(self):
        open_page = parse_page_shell_v2(
            {"data": {"sites": [self._valid()], "cursor": None}},
            provider_id="P2", page_seq=1)
        closed_page = parse_page_shell_v2(
            {"data": {"sites": [self._valid(operational=False)],
                      "cursor": None}},
            provider_id="P2", page_seq=1)
        self.assertEqual(open_page.stations[0].status, "OPEN")
        self.assertEqual(closed_page.stations[0].status, "CLOSED")
        self.assertEqual(open_page.stations[0].connectors, [("CCS", 1, 2)])
        self.assertEqual(open_page.stations[0].raw_locator,
                         "P2/page1/data.sites[0]")

    def test_nested_field_paths_are_its_own(self):
        bad = [
            self._valid(location="flat-string"),
            self._valid(location={"address": "A", "geo": "flat"}),
            self._valid(location={"address": "A",
                                  "geo": {"latitude": 1,
                                          "longitude": 999}}),
            self._valid(operational="yes"),
            self._valid(last_updated=None),
            self._valid(canonical=42),
            self._valid(connector_counts=[]),
        ]
        page = parse_page_shell_v2(
            {"data": {"sites": bad}}, provider_id="P2", page_seq=1)
        self.assertEqual(page.stations, [])
        self.assertEqual(len(page.rejected), len(bad))
        # Locators use the v2 path, never the v1 'stations' wording.
        self.assertTrue(all("data.sites" in p.locator
                            for p in page.rejected))

    def test_envelope_structure_errors_raise(self):
        with self.assertRaises(ValueError):
            parse_page_shell_v2({"stations": []},
                                provider_id="P2", page_seq=1)
        with self.assertRaises(ValueError):
            parse_page_shell_v2({"data": []},
                                provider_id="P2", page_seq=1)
        with self.assertRaises(ValueError):
            parse_page_shell_v2({"data": {"sites": {}}},
                                provider_id="P2", page_seq=1)


class Dispatch(unittest.TestCase):
    def test_unknown_adapter_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_page("shell_v3", {}, provider_id="P", page_seq=1)

    def test_adapter_registry_lists_exactly_two(self):
        self.assertEqual(adapters.ADAPTER_TYPES, ("shell_v1", "shell_v2"))


class HttpTransportPaging(unittest.TestCase):
    """A real loopback HTTP server: cursors must be echoed verbatim."""

    def setUp(self):
        pages = {
            "": {"stations": [
                {"id": "S1", "name": "N", "address": "A", "lat": 1.0,
                 "lon": 2.0, "observed_at": "2024-05-01T10:00:00Z",
                 "status": "OPEN",
                 "connectors": [{"type": "CCS", "available": 1,
                                 "total": 2}]}],
                "next_cursor": "opaque/cursor?x=1"},
            "opaque/cursor?x=1": {"stations": [], "next_cursor": None},
        }
        seen_cursors = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/broken-json":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b"{not valid json")
                    return
                query = parse_qs(parsed.query)
                cursor = query["cursor"][0] if "cursor" in query else None
                seen_cursors.append(cursor)
                if cursor == "boom":
                    self.send_response(500)
                    self.end_headers()
                    return
                body = json.dumps(pages[cursor if cursor is not None else ""])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.endpoint = (f"http://127.0.0.1:{self.server.server_port}/feed")
        self.seen_cursors = seen_cursors

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_opaque_cursor_is_sent_back_unchanged_through_real_http(self):
        result = collect_sources(
            [{"provider_id": "WEB", "endpoint": self.endpoint,
              "adapter_type": "shell_v1"}],
            HttpTransport(timeout=5))
        run = result.sources[0]
        self.assertEqual(run.status, "complete")
        self.assertEqual([s.station_id for s in run.stations], ["S1"])
        # The opaque text, including its slash and query-like fragment, was
        # carried back verbatim (and URL-encoded on the wire).
        self.assertEqual(self.seen_cursors,
                         [None, "opaque/cursor?x=1"])

    def test_http_error_is_a_located_transport_problem(self):
        transport = HttpTransport(timeout=5)
        with self.assertRaises(TransportError) as ctx:
            transport.fetch_page(provider_id="WEB", endpoint=self.endpoint,
                                 cursor="boom", page_seq=4)
        self.assertEqual(ctx.exception.page_seq, 4)
        self.assertEqual(ctx.exception.cursor, "boom")
        self.assertIn("500", ctx.exception.message)

    def test_invalid_json_body_is_reported(self):
        transport = HttpTransport(timeout=5)
        with self.assertRaises(TransportError) as ctx:
            transport.fetch_page(
                provider_id="WEB",
                endpoint=f"http://127.0.0.1:"
                         f"{self.server.server_port}/broken-json",
                cursor=None, page_seq=1)
        self.assertIn("not valid JSON", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()

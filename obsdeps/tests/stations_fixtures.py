"""Hand-designed fixtures for the charging-station collector tests.

Everything here is written out explicitly (no engine output is copied back) so
the tests can assert paper expectations. Two providers use *identical cursor
text* ("cur-a") to prove cursors stay scoped to their own provider, and the
same names/addresses recur across providers without canonical ids to prove they
must not merge.
"""

from __future__ import annotations

from obsdeps.stations.transport import MockTransport

# --------------------------------------------------------------------------- #
# shell_v1 builders (provider ALPHA uses the stations/next_cursor envelope)
# --------------------------------------------------------------------------- #


def v1_station(station_id, *, name, address, lat, lon, observed_at, status,
               canonical_id=None, connectors=None):
    row = {
        "id": station_id,
        "name": name,
        "address": address,
        "lat": lat,
        "lon": lon,
        "observed_at": observed_at,
        "status": status,
        "connectors": connectors or [
            {"type": "CCS", "available": 1, "total": 2}],
    }
    if canonical_id is not None:
        row["canonical_id"] = canonical_id
    return row


def v1_page(stations, next_cursor):
    body = {"stations": stations}
    if next_cursor is not None:
        body["next_cursor"] = next_cursor
    return body


# --------------------------------------------------------------------------- #
# shell_v2 builders (provider BETA uses the data.sites/cursor envelope)
# --------------------------------------------------------------------------- #


def v2_site(site_id, *, name, address, latitude, longitude, last_updated,
            operational, canonical=None, connector_counts=None):
    row = {
        "site_id": site_id,
        "site_name": name,
        "location": {
            "address": address,
            "geo": {"latitude": latitude, "longitude": longitude},
        },
        "last_updated": last_updated,
        "operational": operational,
        "connector_counts": connector_counts or [
            {"connector_type": "CCS", "free": 1, "count": 2}],
    }
    if canonical is not None:
        row["canonical"] = canonical
    return row


def v2_page(sites, cursor):
    return {"data": {"sites": sites, "cursor": cursor}}


# --------------------------------------------------------------------------- #
# The main two-provider scenario (both multi-page, both use cursor "cur-a")
# --------------------------------------------------------------------------- #

RIVER_ADDRESS = "1 River St"
MARKET_ADDRESS = "10 Market Sq"
EAST_ADDRESS = "99 River Rd"


def alpha_pages():
    page1 = v1_page([
        v1_station("A1", name="Riverside Hub", address=RIVER_ADDRESS,
                   lat=40.71, lon=-74.00, observed_at="2024-05-01T10:00:00Z",
                   status="OPEN", canonical_id="CAN-RIVERSIDE",
                   connectors=[{"type": "CCS", "available": 2, "total": 4},
                               {"type": "CHAdeMO", "available": 1, "total": 2}]),
        v1_station("A2", name="Market Square", address=MARKET_ADDRESS,
                   lat=51.50, lon=-0.12, observed_at="2024-05-01T08:00:00Z",
                   status="OPEN",
                   connectors=[{"type": "Type2", "available": 3, "total": 6}]),
        # rejected: available exceeds total
        v1_station("A3", name="Bad Counts", address="3 Bad Rd",
                   lat=10.0, lon=10.0, observed_at="2024-05-01T08:00:00Z",
                   status="OPEN",
                   connectors=[{"type": "CCS", "available": 3, "total": 2}]),
        v1_station("A4", name="Riverside Hub East", address=EAST_ADDRESS,
                   lat=40.72, lon=-74.01, observed_at="2024-05-01T07:00:00Z",
                   status="OPEN",
                   connectors=[{"type": "CCS", "available": 1, "total": 1}]),
        # rejected: latitude outside [-90, 90]
        v1_station("A5", name="Bad Lat", address="5 Bad Rd",
                   lat=91.5, lon=10.0, observed_at="2024-05-01T08:00:00Z",
                   status="OPEN"),
    ], next_cursor="cur-a")

    page2 = v1_page([
        # A1 again, OLDER observation arriving later: must not overwrite 10:00
        v1_station("A1", name="Riverside Hub", address=RIVER_ADDRESS,
                   lat=40.71, lon=-74.00, observed_at="2024-05-01T06:00:00Z",
                   status="CLOSED", canonical_id="CAN-RIVERSIDE",
                   connectors=[{"type": "CCS", "available": 0, "total": 4}]),
        v1_station("A6", name="Tied Plaza", address="6 Tie Ave",
                   lat=12.0, lon=12.0, observed_at="2024-05-01T12:00:00Z",
                   status="OPEN",
                   connectors=[{"type": "CCS", "available": 1, "total": 2}]),
        # rejected: negative total
        v1_station("A7", name="Negative", address="7 Bad Rd",
                   lat=10.0, lon=10.0, observed_at="2024-05-01T08:00:00Z",
                   status="OPEN",
                   connectors=[{"type": "CCS", "available": 0, "total": -1}]),
    ], next_cursor="cur-b")

    page3 = v1_page([
        # A6 again, SAME timestamp but contradictory status/counts -> CONFLICT
        v1_station("A6", name="Tied Plaza", address="6 Tie Ave",
                   lat=12.0, lon=12.0, observed_at="2024-05-01T12:00:00Z",
                   status="CLOSED",
                   connectors=[{"type": "CCS", "available": 0, "total": 2}]),
        # A8 with one explicit canonical id ...
        v1_station("A8", name="Flipped", address="8 Flip St",
                   lat=20.0, lon=20.0, observed_at="2024-05-01T11:00:00Z",
                   status="OPEN", canonical_id="CAN-FLIP-1",
                   connectors=[{"type": "Type2", "available": 2, "total": 2}]),
        # ... and again with a contradictory explicit canonical id: keep evidence
        v1_station("A8", name="Flipped", address="8 Flip St",
                   lat=20.0, lon=20.0, observed_at="2024-05-01T11:05:00Z",
                   status="OPEN", canonical_id="CAN-FLIP-2",
                   connectors=[{"type": "Type2", "available": 2, "total": 2}]),
    ], next_cursor=None)

    return {"": page1, "cur-a": page2, "cur-b": page3}


def beta_pages():
    page1 = v2_page([
        v2_site("B1", name="Riverside Hub", address=RIVER_ADDRESS,
                latitude=40.71, longitude=-74.00,
                last_updated="2024-05-01T09:30:00Z", operational=True,
                canonical="CAN-RIVERSIDE",
                connector_counts=[
                    {"connector_type": "CCS", "free": 4, "count": 8}]),
        # same name/address as ALPHA A2 but no canonical: must stay separate
        v2_site("B2", name="Market Square", address=MARKET_ADDRESS,
                latitude=51.50, longitude=-0.12,
                last_updated="2024-05-01T08:30:00Z", operational=False),
        # same name/address as ALPHA A4 but no canonical: must stay separate
        v2_site("B3", name="Riverside Hub East", address=EAST_ADDRESS,
                latitude=40.72, longitude=-74.01,
                last_updated="2024-05-01T07:30:00Z", operational=True),
        # rejected: longitude outside [-180, 180]
        v2_site("B4", name="Bad Lon", address="4 Bad Rd",
                latitude=10.0, longitude=200.0,
                last_updated="2024-05-01T08:00:00Z", operational=True),
        # rejected: free exceeds count
        v2_site("B5", name="Bad Free", address="5 Bad Rd",
                latitude=10.0, longitude=10.0,
                last_updated="2024-05-01T08:00:00Z", operational=True,
                connector_counts=[
                    {"connector_type": "CCS", "free": 9, "count": 1}]),
    ], cursor="cur-a")  # same cursor text as ALPHA, different provider

    page2 = v2_page([
        v2_site("B6", name="Lakeside", address="6 Lake Rd",
                latitude=45.0, longitude=-73.0,
                last_updated="2024-05-01T14:00:00Z", operational=True,
                connector_counts=[
                    {"connector_type": "Type2", "free": 5, "count": 5}]),
    ], cursor=None)

    return {"": page1, "cur-a": page2}


def main_fixture():
    return {"ALPHA": alpha_pages(), "BETA": beta_pages()}


def main_sources():
    return [
        {"provider_id": "ALPHA", "endpoint": "mock://alpha/directory",
         "adapter_type": "shell_v1"},
        {"provider_id": "BETA", "endpoint": "mock://beta/directory",
         "adapter_type": "shell_v2"},
    ]


def main_transport():
    return MockTransport(main_fixture())


# --------------------------------------------------------------------------- #
# Failure-scenario providers
# --------------------------------------------------------------------------- #


def gamma_cycle_fixture():
    """Page 2 hands back the cursor that was already used -> cycle."""
    return {
        "GAMMA": {
            "": v1_page([
                v1_station("G1", name="Cycle Town", address="1 Loop Rd",
                           lat=1.0, lon=1.0,
                           observed_at="2024-05-01T10:00:00Z",
                           status="OPEN"),
            ], next_cursor="loop"),
            "loop": v1_page([
                v1_station("G2", name="Never Reached", address="2 Loop Rd",
                           lat=1.0, lon=1.0,
                           observed_at="2024-05-01T10:00:00Z",
                           status="OPEN"),
            ], next_cursor="loop"),  # repeats an already-seen cursor
        }
    }


def delta_bad_envelope_fixture():
    """A structurally wrong body that matches neither adapter envelope."""
    return {"DELTA": {"": {"unexpected": ["no stations key here"]}}}


def epsilon_transport_error_fixture():
    """Page 1 points at a cursor the mock interface cannot serve."""
    return {
        "EPSILON": {
            "": v1_page([
                v1_station("E1", name="Partial", address="1 Partial Rd",
                           lat=1.0, lon=1.0,
                           observed_at="2024-05-01T10:00:00Z",
                           status="OPEN"),
            ], next_cursor="vanished"),
            # no entry for "vanished": MockTransport raises TransportError
        }
    }


def cycle_sources():
    return [{"provider_id": "GAMMA", "endpoint": "mock://gamma/directory",
             "adapter_type": "shell_v1"}]


def bad_envelope_sources():
    return [{"provider_id": "DELTA", "endpoint": "mock://delta/directory",
             "adapter_type": "shell_v1"}]


def transport_error_sources():
    return [{"provider_id": "EPSILON", "endpoint": "mock://epsilon/directory",
             "adapter_type": "shell_v1"}]


# Hand-computed expectations for the main scenario (see test_stations_handworked).
EXPECTED = {
    "alpha_page_cursors": [None, "cur-a", "cur-b"],
    "beta_page_cursors": [None, "cur-a"],
    "alpha_accepted_station_ids": ["A1", "A2", "A4", "A1", "A6",
                                   "A6", "A8", "A8"],
    "alpha_rejected_locators": [
        "ALPHA/page1/stations[2]",
        "ALPHA/page1/stations[4]",
        "ALPHA/page2/stations[2]",
    ],
    "beta_rejected_locators": [
        "BETA/page1/data.sites[3]",
        "BETA/page1/data.sites[4]",
    ],
    "current_station_keys": [
        ("ALPHA", "A1"), ("ALPHA", "A2"), ("ALPHA", "A4"),
        ("ALPHA", "A6"), ("ALPHA", "A8"),
        ("BETA", "B1"), ("BETA", "B2"), ("BETA", "B3"), ("BETA", "B6"),
    ],
}

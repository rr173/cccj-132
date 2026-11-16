#!/usr/bin/env python3
"""Build the local mock-interface fixture for the station worked example.

The fixture is plain JSON consumed by ``MockTransport``: per provider, each
cursor (the empty string for the first page) maps to one response body. It is
generated here -- rather than hand-edited -- so every number and the expected
outcome are stated next to the data and can be checked on paper.

Run::

    python3 examples/build_stations_fixture.py examples/stations_mock.json
"""

from __future__ import annotations

import json
import os
import sys


def v1(station_id, name, address, lat, lon, observed_at, status, *,
       canonical_id=None, connectors=None):
    row = {
        "id": station_id, "name": name, "address": address,
        "lat": lat, "lon": lon,
        "observed_at": observed_at, "status": status,
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


def v2(site_id, name, address, latitude, longitude, last_updated,
       operational, *, canonical=None, connector_counts=None):
    row = {
        "site_id": site_id, "site_name": name,
        "location": {"address": address,
                     "geo": {"latitude": latitude, "longitude": longitude}},
        "last_updated": last_updated, "operational": operational,
        "connector_counts": connector_counts or [
            {"connector_type": "CCS", "free": 1, "count": 2}],
    }
    if canonical is not None:
        row["canonical"] = canonical
    return row


def v2_page(sites, cursor):
    return {"data": {"sites": sites, "cursor": cursor}}


def build() -> dict:
    # ALPHA: shell_v1, three pages. Note page 2 carries the same cursor text
    # ("cur-a") that BETA uses on its own second page.
    alpha = {
        "": v1_page([
            # A1 newest observation first; page 2 re-sends an older one.
            v1("A1", "Riverside Hub", "1 River St", 40.71, -74.00,
               "2024-05-01T10:00:00Z", "OPEN",
               canonical_id="CAN-RIVERSIDE",
               connectors=[{"type": "CCS", "available": 2, "total": 4},
                           {"type": "CHAdeMO", "available": 1, "total": 2}]),
            # A2 shares name/address with BETA B2 but has no canonical id.
            v1("A2", "Market Square", "10 Market Sq", 51.50, -0.12,
               "2024-05-01T08:00:00Z", "OPEN",
               connectors=[{"type": "Type2", "available": 3, "total": 6}]),
            # Rejected: available (3) > total (2); siblings stay valid.
            v1("A3", "Bad Counts", "3 Bad Rd", 10.0, 10.0,
               "2024-05-01T08:00:00Z", "OPEN",
               connectors=[{"type": "CCS", "available": 3, "total": 2}]),
        ], "cur-a"),
        "cur-a": v1_page([
            # Older A1 arriving later must not overwrite the 10:00 OPEN row.
            v1("A1", "Riverside Hub", "1 River St", 40.71, -74.00,
               "2024-05-01T06:00:00Z", "CLOSED",
               canonical_id="CAN-RIVERSIDE",
               connectors=[{"type": "CCS", "available": 0, "total": 4}]),
            v1("A6", "Tied Plaza", "6 Tie Ave", 12.0, 12.0,
               "2024-05-01T12:00:00Z", "OPEN",
               connectors=[{"type": "CCS", "available": 1, "total": 2}]),
        ], "cur-b"),
        "cur-b": v1_page([
            # Same timestamp as page 2 but contradictory -> CONFLICT.
            v1("A6", "Tied Plaza", "6 Tie Ave", 12.0, 12.0,
               "2024-05-01T12:00:00Z", "CLOSED",
               connectors=[{"type": "CCS", "available": 0, "total": 2}]),
        ], None),
    }

    # BETA: shell_v2, two pages; second page cursor text equals ALPHA's.
    beta = {
        "": v2_page([
            v2("B1", "Riverside Hub", "1 River St", 40.71, -74.00,
               "2024-05-01T09:30:00Z", True,
               canonical="CAN-RIVERSIDE",
               connector_counts=[
                   {"connector_type": "CCS", "free": 4, "count": 8}]),
            v2("B2", "Market Square", "10 Market Sq", 51.50, -0.12,
               "2024-05-01T08:30:00Z", False),
            # Rejected: longitude out of range.
            v2("B4", "Bad Lon", "4 Bad Rd", 10.0, 200.0,
               "2024-05-01T08:00:00Z", True),
        ], "cur-a"),
        "cur-a": v2_page([
            v2("B6", "Lakeside", "6 Lake Rd", 45.0, -73.0,
               "2024-05-01T14:00:00Z", True,
               connector_counts=[
                   {"connector_type": "Type2", "free": 5, "count": 5}]),
        ], None),
    }

    # GAMMA: page 2 repeats the cursor already used -> cycle, incomplete.
    gamma = {
        "": v1_page([
            v1("G1", "Cycle Town", "1 Loop Rd", 1.0, 1.0,
               "2024-05-01T10:00:00Z", "OPEN"),
        ], "loop"),
        "loop": v1_page([
            v1("G2", "Never Followed", "2 Loop Rd", 1.0, 1.0,
               "2024-05-01T10:05:00Z", "OPEN"),
        ], "loop"),
    }
    return {"ALPHA": alpha, "BETA": beta, "GAMMA": gamma}


def main(argv):
    target = argv[1] if len(argv) > 1 else "stations_mock.json"
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(build(), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"wrote {target}")


if __name__ == "__main__":
    main(sys.argv)

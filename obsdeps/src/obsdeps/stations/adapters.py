"""The two provider JSON adapters and the shared record-level validation.

Each adapter owns *its own* envelope and field paths. Nothing here assumes one
provider's field names apply to the other: the mapping tables live inside the
adapter functions and an unknown adapter type is a configuration error.

Adapter ``shell_v1`` -- envelope ``{"stations": [...], "next_cursor"?}``::

    id              string            source station id
    name            string
    address         string
    lat / lon       numbers           decimal degrees
    observed_at     RFC 3339 string
    status          string            e.g. "OPEN" / "CLOSED"
    canonical_id    string, optional  cross-network identity
    connectors      list of {type, available, total}

Adapter ``shell_v2`` -- envelope ``{"data": {"sites": [...], "cursor"?}}``::

    site_id         string
    site_name       string
    location.address string
    location.geo.latitude / location.geo.longitude
    last_updated    RFC 3339 string
    operational     boolean           mapped to OPEN / CLOSED
    canonical       string, optional
    connector_counts  list of {connector_type, free, count}

Every adapter yields a :class:`NormalizedStation`; every record-level problem
is returned as a :class:`~obsdeps.errors.Problem` whose locator pins down the
provider, page and original row index, so the engine can reject just that
record and keep the rest.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..errors import Problem

ADAPTER_TYPES = ("shell_v1", "shell_v2")

# Status vocabulary used inside the local store. Adapters translate their own
# raw status representations onto these strings; the values are never guessed
# from another provider's spelling.
STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"


@dataclass(frozen=True)
class NormalizedStation:
    """A provider record after adapter translation and validation.

    ``connectors`` maps connector type to ``(available, total)``; the mapping
    preserves first-seen order via a list of pairs in :class:`StationRecord`.
    """

    provider_id: str
    station_id: str
    canonical_id: Optional[str]
    name: str
    address: str
    latitude: float
    longitude: float
    observed_at: str            # normalized RFC 3339 UTC text
    observed_sort: str          # same instant, normalized for stable ordering
    status: str
    connectors: List[Tuple[str, int, int]]
    raw_locator: str            # provider/page/row pointer into the feed


@dataclass(frozen=True)
class ParsedPage:
    """The useful content of one fetched page."""

    stations: List[NormalizedStation]
    next_cursor: Optional[str]
    rejected: List[Problem] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Shared low-level field validation (generic type/range checks only; the field
# *paths* are decided by each adapter independently).
# --------------------------------------------------------------------------- #


def parse_observed_at(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Parse an RFC 3339 timestamp into ``(normalized_text, sort_key)``.

    Returns ``(None, problem_message)`` when the value cannot be interpreted.
    """
    if not isinstance(value, str) or not value.strip():
        return None, "observed timestamp must be a non-empty string"
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        instant = _dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None, f"observed timestamp is not valid RFC 3339: {value!r}"
    if instant.tzinfo is None:
        return None, f"observed timestamp needs an explicit UTC offset: {value!r}"
    instant_utc = instant.astimezone(_dt.timezone.utc)
    canonical = instant_utc.strftime("%Y-%m-%dT%H:%M:%S")
    if instant_utc.microsecond:
        canonical += f".{instant_utc.microsecond:06d}"
    canonical += "Z"
    return canonical, canonical


def check_coordinates(lat: Any, lon: Any) -> Optional[str]:
    if not isinstance(lat, (int, float)) or isinstance(lat, bool):
        return "latitude must be a number"
    if not isinstance(lon, (int, float)) or isinstance(lon, bool):
        return "longitude must be a number"
    if not -90.0 <= float(lat) <= 90.0:
        return f"latitude {lat} out of range [-90, 90]"
    if not -180.0 <= float(lon) <= 180.0:
        return f"longitude {lon} out of range [-180, 180]"
    return None


def check_connector(available: Any, total: Any) -> Optional[str]:
    if not isinstance(total, int) or isinstance(total, bool):
        return "connector total must be an integer"
    if not isinstance(available, int) or isinstance(available, bool):
        return "connector available count must be an integer"
    if total < 0:
        return f"connector total cannot be negative (got {total})"
    if available < 0:
        return f"connector available count cannot be negative (got {available})"
    if available > total:
        return f"connector available {available} cannot exceed total {total}"
    return None


def _require_nonempty_string(value: Any, field_name: str) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return f"{field_name} must be a non-empty string"
    return None


# --------------------------------------------------------------------------- #
# Adapter shell_v1
# --------------------------------------------------------------------------- #


def parse_page_shell_v1(payload: Any, *, provider_id: str,
                        page_seq: int) -> ParsedPage:
    """Parse the ``{"stations": [...], "next_cursor"?}`` envelope."""
    rejected: List[Problem] = []
    if not isinstance(payload, dict):
        raise ValueError("response body must be a JSON object")
    rows = payload.get("stations")
    if not isinstance(rows, list):
        raise ValueError("envelope field 'stations' must be an array")
    cursor = payload.get("next_cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("envelope field 'next_cursor' must be a non-empty string or null")

    stations: List[NormalizedStation] = []
    for row_index, row in enumerate(rows):
        locator = f"{provider_id}/page{page_seq}/stations[{row_index}]"
        problem = _parse_row_shell_v1(row, locator)
        if isinstance(problem, Problem):
            rejected.append(problem)
        else:
            stations.append(problem)
    return ParsedPage(stations=stations, next_cursor=cursor, rejected=rejected)


def _parse_row_shell_v1(row: Any, locator: str):
    if not isinstance(row, dict):
        return Problem("station-record-invalid",
                       "station entry must be a JSON object", locator)
    for field_name in ("id", "name", "address", "observed_at", "status"):
        message = _require_nonempty_string(row.get(field_name), field_name)
        if message is not None:
            return Problem("station-record-invalid", message, locator,
                           {"field": field_name})

    observed, sort_key = parse_observed_at(row.get("observed_at"))
    if observed is None:
        return Problem("station-record-invalid", sort_key, locator,
                       {"field": "observed_at"})

    coords_problem = check_coordinates(row.get("lat"), row.get("lon"))
    if coords_problem is not None:
        return Problem("station-record-invalid", coords_problem, locator,
                       {"field": "lat/lon"})

    connectors, connector_problem = _connectors_shell_v1(row.get("connectors"),
                                                         locator)
    if connector_problem is not None:
        return connector_problem

    canonical = row.get("canonical_id")
    if canonical is not None and (not isinstance(canonical, str)
                                  or not canonical.strip()):
        return Problem("station-record-invalid",
                       "canonical_id must be a non-empty string when present",
                       locator, {"field": "canonical_id"})

    return NormalizedStation(
        provider_id=locator.split("/", 1)[0],
        station_id=row["id"].strip(),
        canonical_id=canonical.strip() if canonical else None,
        name=row["name"].strip(),
        address=row["address"].strip(),
        latitude=float(row["lat"]),
        longitude=float(row["lon"]),
        observed_at=observed,
        observed_sort=sort_key,
        status=row["status"].strip(),
        connectors=connectors,
        raw_locator=locator,
    )


def _connectors_shell_v1(value: Any, locator: str):
    if not isinstance(value, list) or not value:
        return None, Problem("station-record-invalid",
                             "connectors must be a non-empty array",
                             locator, {"field": "connectors"})
    result: List[Tuple[str, int, int]] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return None, Problem("station-record-invalid",
                                 "connector entry must be a JSON object",
                                 locator, {"field": "connectors",
                                           "connector_index": index})
        type_problem = _require_nonempty_string(item.get("type"), "type")
        if type_problem is not None:
            return None, Problem("station-record-invalid", type_problem,
                                 locator, {"field": "connectors.type",
                                           "connector_index": index})
        connector_type = item["type"].strip()
        message = check_connector(item.get("available"), item.get("total"))
        if message is not None:
            return None, Problem("station-record-invalid", message,
                                 locator,
                                 {"field": "connectors.available/total",
                                  "connector_index": index})
        if connector_type in seen:
            return None, Problem(
                "station-record-invalid",
                f"connector type {connector_type!r} appears more than once",
                locator, {"field": "connectors.type",
                          "connector_index": index})
        seen[connector_type] = index
        result.append((connector_type, int(item["available"]),
                       int(item["total"])))
    return result, None


# --------------------------------------------------------------------------- #
# Adapter shell_v2
# --------------------------------------------------------------------------- #


def parse_page_shell_v2(payload: Any, *, provider_id: str,
                        page_seq: int) -> ParsedPage:
    """Parse the ``{"data": {"sites": [...], "cursor"?}}`` envelope."""
    rejected: List[Problem] = []
    if not isinstance(payload, dict):
        raise ValueError("response body must be a JSON object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("envelope field 'data' must be a JSON object")
    rows = data.get("sites")
    if not isinstance(rows, list):
        raise ValueError("envelope field 'data.sites' must be an array")
    cursor = data.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("envelope field 'data.cursor' must be a non-empty string or null")

    stations: List[NormalizedStation] = []
    for row_index, row in enumerate(rows):
        locator = f"{provider_id}/page{page_seq}/data.sites[{row_index}]"
        problem = _parse_row_shell_v2(row, locator, provider_id)
        if isinstance(problem, Problem):
            rejected.append(problem)
        else:
            stations.append(problem)
    return ParsedPage(stations=stations, next_cursor=cursor, rejected=rejected)


def _parse_row_shell_v2(row: Any, locator: str, provider_id: str):
    if not isinstance(row, dict):
        return Problem("station-record-invalid",
                       "site entry must be a JSON object", locator)
    for field_name in ("site_id", "site_name", "last_updated"):
        message = _require_nonempty_string(row.get(field_name), field_name)
        if message is not None:
            return Problem("station-record-invalid", message, locator,
                           {"field": field_name})
    if not isinstance(row.get("operational"), bool):
        return Problem("station-record-invalid",
                       "operational must be a boolean", locator,
                       {"field": "operational"})

    location = row.get("location")
    if not isinstance(location, dict):
        return Problem("station-record-invalid",
                       "location must be a JSON object", locator,
                       {"field": "location"})
    address_problem = _require_nonempty_string(location.get("address"),
                                               "location.address")
    if address_problem is not None:
        return Problem("station-record-invalid", address_problem, locator,
                       {"field": "location.address"})
    geo = location.get("geo")
    if not isinstance(geo, dict):
        return Problem("station-record-invalid",
                       "location.geo must be a JSON object", locator,
                       {"field": "location.geo"})
    coords_problem = check_coordinates(geo.get("latitude"),
                                       geo.get("longitude"))
    if coords_problem is not None:
        return Problem("station-record-invalid", coords_problem, locator,
                       {"field": "location.geo.latitude/longitude"})

    observed, sort_key = parse_observed_at(row.get("last_updated"))
    if observed is None:
        return Problem("station-record-invalid", sort_key, locator,
                       {"field": "last_updated"})

    connectors, connector_problem = _connectors_shell_v2(
        row.get("connector_counts"), locator)
    if connector_problem is not None:
        return connector_problem

    canonical = row.get("canonical")
    if canonical is not None and (not isinstance(canonical, str)
                                  or not canonical.strip()):
        return Problem("station-record-invalid",
                       "canonical must be a non-empty string when present",
                       locator, {"field": "canonical"})

    return NormalizedStation(
        provider_id=provider_id,
        station_id=row["site_id"].strip(),
        canonical_id=canonical.strip() if canonical else None,
        name=row["site_name"].strip(),
        address=location["address"].strip(),
        latitude=float(geo["latitude"]),
        longitude=float(geo["longitude"]),
        observed_at=observed,
        observed_sort=sort_key,
        status=STATUS_OPEN if row["operational"] else STATUS_CLOSED,
        connectors=connectors,
        raw_locator=locator,
    )


def _connectors_shell_v2(value: Any, locator: str):
    if not isinstance(value, list) or not value:
        return None, Problem("station-record-invalid",
                             "connector_counts must be a non-empty array",
                             locator, {"field": "connector_counts"})
    result: List[Tuple[str, int, int]] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return None, Problem("station-record-invalid",
                                 "connector count entry must be a JSON object",
                                 locator, {"field": "connector_counts",
                                           "connector_index": index})
        type_problem = _require_nonempty_string(item.get("connector_type"),
                                                "connector_type")
        if type_problem is not None:
            return None, Problem(
                "station-record-invalid", type_problem,
                locator, {"field": "connector_counts.connector_type",
                          "connector_index": index})
        connector_type = item["connector_type"].strip()
        message = check_connector(item.get("free"), item.get("count"))
        if message is not None:
            return None, Problem("station-record-invalid", message,
                                 locator, {"field": "connector_counts.free/count",
                                           "connector_index": index})
        if connector_type in seen:
            return None, Problem(
                "station-record-invalid",
                f"connector type {connector_type!r} appears more than once",
                locator, {"field": "connector_counts.connector_type",
                          "connector_index": index})
        seen[connector_type] = index
        result.append((connector_type, int(item["free"]),
                       int(item["count"])))
    return result, None


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def parse_page(adapter_type: str, payload: Any, *, provider_id: str,
               page_seq: int) -> ParsedPage:
    """Dispatch to the adapter named in the source configuration.

    A structural envelope problem (wrong top-level shape) raises
    :class:`ValueError`; the engine records it as a page-level failure for that
    single source. Record-level problems never raise -- they ride back inside
    :class:`ParsedPage.rejected`.
    """
    if adapter_type == "shell_v1":
        return parse_page_shell_v1(payload, provider_id=provider_id,
                                   page_seq=page_seq)
    if adapter_type == "shell_v2":
        return parse_page_shell_v2(payload, provider_id=provider_id,
                                   page_seq=page_seq)
    raise ValueError(f"unknown adapter type: {adapter_type!r}")

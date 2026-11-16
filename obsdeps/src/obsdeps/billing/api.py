"""Public API for offline time-of-use settlement.

Two ways of working, both offline and deterministic:

* **File mode** -- :meth:`settle_files` (or the CLI ``billing settle``) reads
  the four JSON input files directly. Nothing is written, the inputs are never
  modified, and no database is needed.
* **Registered mode** -- :meth:`register_inputs` stores the canonical input
  rows in an immutable SQLite database, and :meth:`settle_registered` reruns
  the settlement from the stored bytes. Re-registering identical content is a
  no-op; different content under an existing identity is refused.

Data contradictions never raise: they land in the result's ``rejected``
bucket with a stable problem code. Only an unusable *input file*
(:class:`BillingInputError`) or a store conflict
(:class:`~obsdeps.errors.Problematic`) raises.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from ..errors import Problematic
from .engine import BILLING_ENGINE_VERSION, settle_inputs
from .errors import BillingInputError
from .inputs import load_inputs
from .store import BillingStore

__all__ = ["BillingApi", "BillingInputError", "BILLING_ENGINE_VERSION"]


class BillingApi:
    """Facade over the parser, the immutable store and the settlement engine."""

    def __init__(self, database: Optional[str] = None):
        self.database = database
        self._store: Optional[BillingStore] = None
        if database is not None:
            self._store = BillingStore(database)

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> "BillingApi":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- file mode
    def settle_files(
        self,
        *,
        meters: str,
        readings: str,
        tariffs: str,
        periods: str,
    ) -> Dict[str, Any]:
        """Settle directly from four local JSON file paths."""
        for path in (meters, readings, tariffs, periods):
            if not isinstance(path, str) or not os.path.isfile(path):
                raise BillingInputError(f"input file does not exist: {path}",
                                        source=path)
        sources = {"meters": meters, "readings": readings,
                   "tariffs": tariffs, "periods": periods}
        parsed = load_inputs(
            meters=meters, readings=readings, tariffs=tariffs, periods=periods,
            sources=sources,
        )
        return settle_inputs(parsed)

    def settle_documents(
        self,
        *,
        meters: Sequence[Dict[str, Any]],
        readings: Sequence[Dict[str, Any]],
        tariffs: Sequence[Dict[str, Any]],
        periods: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Settle from already-parsed JSON structures (also used by the store)."""
        parsed = load_inputs(
            meters=list(meters), readings=list(readings),
            tariffs=list(tariffs), periods=list(periods),
            sources={"meters": "<documents>", "readings": "<documents>",
                     "tariffs": "<documents>", "periods": "<documents>"},
        )
        return settle_inputs(parsed)

    # --------------------------------------------------------- registered mode
    def register_inputs(
        self,
        *,
        meters: str,
        readings: str,
        tariffs: str,
        periods: str,
        batch_id: Optional[str] = None,
        note: Optional[str] = None,
        registered_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate and store four input files in the immutable database."""
        if self._store is None:
            raise BillingInputError(
                "register_inputs() requires BillingApi(database=...)")
        documents = self._read_four(meters, readings, tariffs, periods)
        # Parse first: structural validation errors must reject the whole batch
        # before anything is written.
        load_inputs(meters=documents["meters"], readings=documents["readings"],
                    tariffs=documents["tariffs"], periods=documents["periods"],
                    sources={"meters": meters, "readings": readings,
                             "tariffs": tariffs, "periods": periods})
        return self._store.register_batch(
            meters=documents["meters"], readings=documents["readings"],
            tariffs=documents["tariffs"], periods=documents["periods"],
            batch_id=batch_id, note=note, registered_at=registered_at,
        )

    def settle_registered(self) -> Dict[str, Any]:
        """Settle every period currently held in the database."""
        if self._store is None:
            raise BillingInputError(
                "settle_registered() requires BillingApi(database=...)")
        documents = self._store.load_documents()
        parsed = load_inputs(
            meters=documents["meters"], readings=documents["readings"],
            tariffs=documents["tariffs"], periods=documents["periods"],
            sources={"meters": f"{self.database}:billing_meters",
                     "readings": f"{self.database}:billing_readings",
                     "tariffs": f"{self.database}:billing_tariffs",
                     "periods": f"{self.database}:billing_periods"},
        )
        return settle_inputs(parsed)

    def store_counts(self) -> Dict[str, int]:
        if self._store is None:
            raise BillingInputError(
                "store_counts() requires BillingApi(database=...)")
        return self._store.counts()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _read_four(meters: str, readings: str, tariffs: str,
                   periods: str) -> Dict[str, List[Dict[str, Any]]]:
        from .inputs import load_json_file
        names = {"meters": meters, "readings": readings,
                 "tariffs": tariffs, "periods": periods}
        documents: Dict[str, List[Dict[str, Any]]] = {}
        for name, path in names.items():
            data = load_json_file(path)
            if not isinstance(data, list):
                raise BillingInputError(
                    f"{name} document must be a JSON array", source=path)
            documents[name] = data
        return documents

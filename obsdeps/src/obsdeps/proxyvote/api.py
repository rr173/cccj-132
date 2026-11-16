"""Public Python API for offline proxy-vote reconciliation.

Three ways of working, all offline and deterministic:

* **File mode** -- :meth:`reconcile_files` reads the four JSONL files for one
  explicitly requested meeting version and returns the result.  Nothing is
  written and the inputs are never modified.
* **Stored mode** -- :meth:`register` parses, reconciles and stores the result
  immutably under a deterministic id, then :meth:`summary`, :meth:`position`,
  :meth:`proposal` and :meth:`evidence` answer from the database alone.
* **Embedded documents** -- :meth:`reconcile_documents` takes already-parsed
  rows (also used by the tests and the independent enumerator).

Unreadable documents raise :class:`ProxyVoteInputError`.  Data problems never
raise: they are attached to the position they touch and set ``complete`` to
``false`` while the other positions keep their results.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, List, Optional

from .engine import PROXYVOTE_ENGINE_VERSION, reconcile_meeting
from .errors import ProxyVoteInputError, ReconciliationError
from .inputs import load_documents
from .store import ProxyVoteStore

__all__ = ["ProxyVoteApi", "ProxyVoteInputError", "ReconciliationError",
           "PROXYVOTE_ENGINE_VERSION"]


class ProxyVoteApi:
    """Facade over the four-document parser, the engine and the store."""

    def __init__(self, database: Optional[str] = None):
        self.database = database
        self._store: Optional[ProxyVoteStore] = None
        if database is not None:
            self._store = ProxyVoteStore(database)

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> "ProxyVoteApi":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- file mode
    def reconcile_files(self, *, meeting_id: str, record_date: str,
                        cutoff_at: str, roster: str, proposals: str,
                        grants: str, ballots: str) -> Dict[str, Any]:
        """Reconcile directly from four local JSONL paths."""
        for path in (roster, proposals, grants, ballots):
            if not isinstance(path, str) or not os.path.isfile(path):
                raise ProxyVoteInputError(
                    f"input file does not exist: {path}", source=path)
        parsed = load_documents(
            meeting_id=meeting_id, record_date=record_date,
            cutoff_at=cutoff_at, roster=roster, proposals=proposals,
            grants=grants, ballots=ballots)
        return reconcile_meeting(parsed)

    def reconcile_documents(self, *, meeting_id: str, record_date: str,
                            cutoff_at: str, roster: Any, proposals: Any,
                            grants: Any, ballots: Any) -> Dict[str, Any]:
        """Reconcile from Python row lists, JSON strings or file paths."""
        parsed = load_documents(
            meeting_id=meeting_id, record_date=record_date,
            cutoff_at=cutoff_at, roster=roster, proposals=proposals,
            grants=grants, ballots=ballots)
        return reconcile_meeting(parsed)

    # ----------------------------------------------------------- stored mode
    def register(self, *, meeting_id: str, record_date: str, cutoff_at: str,
                 roster: str, proposals: str, grants: str, ballots: str,
                 created_at: Optional[str] = None) -> Dict[str, Any]:
        """Reconcile and store the result immutably; re-running is a no-op."""
        if self._store is None:
            raise ProxyVoteInputError(
                "register() requires ProxyVoteApi(database=...)")
        parsed = load_documents(
            meeting_id=meeting_id, record_date=record_date,
            cutoff_at=cutoff_at, roster=roster, proposals=proposals,
            grants=grants, ballots=ballots)
        result = reconcile_meeting(parsed)
        stamp = created_at or _stamp()
        return self._store.save(result, sources=parsed.sources,
                                created_at=stamp)

    def list_reconciliations(self,
                             meeting_id: Optional[str] = None
                             ) -> List[Dict[str, Any]]:
        return self._require_store().list_reconciliations(meeting_id)

    def summary(self, *, reconciliation_id: Optional[str] = None,
                meeting_id: Optional[str] = None,
                record_date: Optional[str] = None,
                cutoff_at: Optional[str] = None) -> Dict[str, Any]:
        """Totals per proposal from the stored document (read-only)."""
        document = self._document(reconciliation_id, meeting_id,
                                  record_date, cutoff_at)
        return {
            "reconciliation_id": document["_reconciliation_id"],
            "engine_version": document["engine_version"],
            "meeting_id": document["meeting_id"],
            "roster_version": document["roster_version"],
            "record_date": document["record_date"],
            "cutoff_at": document["cutoff_at"],
            "cutoff_utc": document["cutoff_utc"],
            "complete": document["complete"],
            "summary": document["summary"],
            "proposals": [
                {key: value for key, value in proposal.items()
                 if key != "segments"}
                for proposal in document["proposals"]
            ],
            "problems": document["problems"],
            "sources": document["sources"],
        }

    def proposal(self, *, proposal_id: str,
                 reconciliation_id: Optional[str] = None,
                 meeting_id: Optional[str] = None,
                 record_date: Optional[str] = None,
                 cutoff_at: Optional[str] = None) -> Dict[str, Any]:
        document = self._document(reconciliation_id, meeting_id,
                                  record_date, cutoff_at)
        for proposal in document["proposals"]:
            if proposal["proposal_id"] == proposal_id:
                return proposal
        raise ReconciliationError(
            f"proposal {proposal_id!r} is not part of reconciliation "
            f"{document['_reconciliation_id']}", code="proposal-unknown",
            locator=f"proposal:{proposal_id}")

    def position(self, *, position_id: str,
                 reconciliation_id: Optional[str] = None,
                 meeting_id: Optional[str] = None,
                 record_date: Optional[str] = None,
                 cutoff_at: Optional[str] = None) -> Dict[str, Any]:
        document = self._document(reconciliation_id, meeting_id,
                                  record_date, cutoff_at)
        for position in document["positions"]:
            if position["position_id"] == position_id:
                return position
        raise ReconciliationError(
            f"position {position_id!r} is not part of reconciliation "
            f"{document['_reconciliation_id']}", code="position-unknown",
            locator=f"position:{position_id}")

    def evidence(self, *, reconciliation_id: Optional[str] = None,
                 meeting_id: Optional[str] = None,
                 record_date: Optional[str] = None,
                 cutoff_at: Optional[str] = None,
                 proposal_id: Optional[str] = None,
                 position_id: Optional[str] = None) -> Dict[str, Any]:
        """Per-segment evidence, optionally narrowed to one proposal/position."""
        document = self._document(reconciliation_id, meeting_id,
                                  record_date, cutoff_at)
        if proposal_id is not None:
            proposal = self.proposal(
                proposal_id=proposal_id,
                reconciliation_id=document["_reconciliation_id"])
            return {
                "reconciliation_id": document["_reconciliation_id"],
                "meeting_id": document["meeting_id"],
                "proposal_id": proposal_id,
                "by_choice": proposal["by_choice"],
                "abstain": proposal["abstain"],
                "contested": proposal["contested"],
                "invalid": proposal["invalid"],
                "segments": [s for s in proposal["segments"]
                             if position_id is None
                             or s["position_id"] == position_id],
            }
        positions = document["positions"]
        if position_id is not None:
            positions = [self.position(
                position_id=position_id,
                reconciliation_id=document["_reconciliation_id"])]
        return {
            "reconciliation_id": document["_reconciliation_id"],
            "meeting_id": document["meeting_id"],
            "complete": document["complete"],
            "positions": positions,
            "sources": document["sources"],
        }

    def counts(self) -> Dict[str, int]:
        return self._require_store().counts()

    # --------------------------------------------------------------- helpers
    def _require_store(self) -> ProxyVoteStore:
        if self._store is None:
            raise ProxyVoteInputError(
                "this operation requires ProxyVoteApi(database=...)")
        return self._store

    def _document(self, reconciliation_id: Optional[str],
                  meeting_id: Optional[str], record_date: Optional[str],
                  cutoff_at: Optional[str]) -> Dict[str, Any]:
        store = self._require_store()
        if reconciliation_id is None:
            if meeting_id is None:
                raise ReconciliationError(
                    "either reconciliation_id or meeting_id is required",
                    code="request-incomplete")
            found = store.latest(meeting_id, record_date=record_date,
                                 cutoff_at=cutoff_at)
            if found is None:
                raise ReconciliationError(
                    f"no stored reconciliation for meeting {meeting_id!r}",
                    code="reconciliation-unknown",
                    locator=f"meeting:{meeting_id}")
            reconciliation_id, document = found
        else:
            document = store.document(reconciliation_id)
            if document is None:
                raise ReconciliationError(
                    f"unknown reconciliation {reconciliation_id!r}",
                    code="reconciliation-unknown",
                    locator=f"reconciliation:{reconciliation_id}")
        document = dict(document)
        document["_reconciliation_id"] = reconciliation_id
        return document


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

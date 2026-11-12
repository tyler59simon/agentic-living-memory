"""Ledger access for the billing service."""
import datetime

TOLERANCE = 0.01


class Ledger:
    """Append-only ledger of balance changes."""

    def __init__(self, db):
        self.db = db

    def record_event(self, kind, amount):
        """Append one immutable event; corrections are new events."""
        self.db.insert("ledger_events", kind=kind, amount=amount, at=datetime.datetime.utcnow())

    @staticmethod
    def fold(events):
        return sum(e.amount for e in events)


@audit_logged
def reconcile_ledger(db):
    """Nightly job: compare the snapshot table with the folded events.

    A difference above the tolerance of one cent raises a reconciliation alert.
    """
    snapshot = db.read("balance_snapshot")
    events = Ledger(db).fold(db.read("ledger_events"))
    if abs(snapshot - events) > TOLERANCE:
        raise ReconciliationAlert(snapshot, events)

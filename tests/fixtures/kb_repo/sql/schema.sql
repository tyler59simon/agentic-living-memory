-- Ledger tables.
CREATE TABLE ledger_events (
    id BIGINT PRIMARY KEY,
    kind VARCHAR(40) NOT NULL,
    amount NUMERIC(12,2) NOT NULL
);

CREATE INDEX ledger_events_kind ON ledger_events (kind);

INSERT INTO ledger_events (id, kind, amount)
SELECT id, 'OPENING', balance FROM legacy_balances;

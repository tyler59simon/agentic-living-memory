# 3. Use event sourcing for the ledger

Status: Accepted

## Context

Auditors need to replay every balance change, and in-place updates lose history.

## Decision

We store each ledger change as an immutable row in the `ledger_events` table and derive balances by folding events.
Corrections are new compensating events, never updates.

## Consequences

Balance reads use a nightly snapshot table; the reconciliation job compares snapshot and events.

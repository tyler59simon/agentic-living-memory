# Billing Service

The billing service issues invoices and collects payments for the Acme platform.

## Retry policy

Failed payment captures are retried with exponential backoff. The first retry waits 30 seconds,
each later retry doubles the wait, and after five attempts the invoice moves to the dunning queue.

## Deployment

Releases ship through the blue-green pipeline. A rollback restores the previous container image tag.

```bash
# this comment is not a heading
./deploy.sh --env prod
```

### Smoke tests

After every release the pipeline calls the `/health/billing` endpoint and checks the ledger balance.

## Glossary

Dunning
: the process of chasing overdue invoices.

# Direct USDT TRC-20 deposits

## Architecture and security boundary

Veltrix uses **one shared, public receiving wallet** for direct USDT TRC-20
deposits. Every client request displays the same address and a unique deposit
reference; the exact payable amount is the primary matching aid. This design
does not depend on a memo field, a per-client address, or a custody allocator.

The application never generates, receives, derives, logs, or stores a private
key, seed phrase, signing credential, or wallet password. It cannot sign or
move funds. The configured value is a public address only.

The authoritative mainnet contract is the official Tether TRC-20 contract:

`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`

The implementation pins this address for mainnet and rejects a different
mainnet contract. Tether documents the same TRC-20 contract at
<https://tether.to/en/supported-protocols/>; TRONSCAN lists it as the official
USDT token with 6 decimals. Testnet requires an operator-verified test token
contract.

## Components

- `Deposit`, `CryptoDeposit`: client request, exact amount, address, txid,
  confirmations, submitted claim, and lifecycle state.
- `ChainScanCursor`: durable timestamp/fingerprint/window cursor per network.
- `ChainTransfer`: independently observed contract event, keyed by
  `(network, txid, event_index)`, persisted before confirmation or credit.
- `services/tron.py`: configuration validation, scanning, attribution,
  canonical verification, and crediting.
- `services/ledger.py`: idempotent double-entry credit to customer and
  treasury/fee accounts.
- `services/reconciliation.py`: read-only deposit reconciliation.
- `scan_tron_deposits`, `tron_monitor_status`, `reconcile_deposits` management
  commands.
- Django admin exposes scan cursors, transfers, deposits, and ledger records as
  read-only inspection data where applicable.

## Fail-closed controls

`FUNDING_DEPOSITS_ENABLED` is the global real-funding switch. In a production
environment, settings additionally require an explicit `DEBUG=0` and
`FUNDING_LIVE_VERIFIED=1`; otherwise both funding and NOWPayments are forced
off. This is a deployment safety gate, not a substitute for provider
verification.

The direct rail requires all of the following:

- `CRYPTO_DEPOSIT_MODE=direct_tron`
- `FUNDING_DEPOSITS_ENABLED=1`
- `TRON_DEPOSITS_ENABLED=1`
- `TRON_NETWORK=mainnet|testnet`
- an HTTPS `TRON_API_BASE_URL` for the same network. TronGrid hosts are
  network-specific: `https://api.trongrid.io` is mainnet only, while
  `https://nile.trongrid.io` and `https://api.shasta.trongrid.io` are testnets.
  The application rejects cross-network TronGrid combinations.
- `VELTRIX_USDT_TRC20_RECEIVING_ADDRESS` set to a valid public TRON address
- `TRON_REQUIRED_CONFIRMATIONS>=1`
- the matching `Asset`, `Network`, and `DepositProvider` rows

For mainnet only, crediting additionally requires `TRON_LIVE_VERIFIED=1`.
Detection and canonical checks are read-only and use a separate gate, so an
operator can inspect chain state without granting permission to credit. The
monitor skips the credit step on mainnet until `TRON_LIVE_VERIFIED=1`; it still
advances the scan cursor and records confirmed transfers.

Testnet must keep `TRON_LIVE_VERIFIED=0`. Testnet can complete the full
detection, approval, and credit workflow using test funds. Mainnet uses the
live-credit attestation. A deployment classified as production also refuses to
expose or use a testnet receiving address, even if testnet is selected by
mistake; testnet rehearsals belong in the isolated `DEBUG=1` environment below.

Local testnet example (no production database):

```dotenv
DEBUG=1
SECRET_KEY=local-test-only
NOWPAYMENTS_SANDBOX=1
CRYPTO_DEPOSIT_MODE=direct_tron
FUNDING_DEPOSITS_ENABLED=1
TRON_DEPOSITS_ENABLED=1
TRON_NETWORK=testnet
TRON_API_BASE_URL=https://nile.trongrid.io
VELTRIX_USDT_TRC20_RECEIVING_ADDRESS=<operator-controlled test wallet>
TRON_USDT_CONTRACT=<verified test USDT contract>
TRON_REQUIRED_CONFIRMATIONS=2
TRON_LIVE_VERIFIED=0
TRON_SCAN_START_TIMESTAMP=<milliseconds after deployment>
```

Keep `TRON_LIVE_VERIFIED=0` everywhere testnet is selected. `NOWPAYMENTS_SANDBOX=1`
is needed by the load-time funding gate for this isolated `DEBUG=1`
configuration; it closes the legacy live-funding request and does not authorize
production funding.

Do not add `DATABASE_URL` to this rehearsal. Any configured database URL marks
the deployment as production for the funding gate, which then requires
`FUNDING_LIVE_VERIFIED=1` before `FUNDING_DEPOSITS_ENABLED` can be on.
`FUNDING_LIVE_VERIFIED=1` is a production funding attestation and must not be
enabled merely to rehearse testnet. Run testnet rehearsals only in the isolated
`DEBUG=1` environment above.

## Setup

1. Create the configured rail rows only after the environment is valid:

   ```bash
   python manage.py seed_funding_rails --activate-direct-tron
   ```

   Omit `--activate-direct-tron` first to create inactive rows for inspection.
   The command creates the USDT asset, the configured TRON network, and the
   `direct_tron` provider. It does not invent an address or contract.

2. Confirm the rows match the environment:

   - `Asset.code=USDT`, `decimals=6`, active.
   - `Network.chain=TRON`, `token_standard=TRC20`, decimals 6,
     `contract_address` equal to the configured contract, `is_testnet`
     matching `TRON_NETWORK`, active, confirmation threshold at least 1.
   - `DepositProvider.code=direct_tron`, rail crypto, active.
   - Exactly one matching network row; zero or multiple rows fail closed.

3. Start the monitor outside the web process. The container command runs
   Gunicorn only; it does not start a worker.

## Scanning and cursor recovery

TronGrid contract events are queried by millisecond timestamp window:

- `TRON_SCAN_START_TIMESTAMP` is the initial lower bound and must be chosen
  conservatively.
- `TRON_SCAN_WINDOW_SECONDS` bounds each invocation.
- `TRON_SCAN_OVERLAP_SECONDS` re-reads a small tail; duplicates are ignored by
  the unique event constraint.
- `TRON_SCAN_LAG_SECONDS` keeps the scan behind the node head.
- `TRON_UNCONFIRMED_REVIEW_MINUTES` moves a transfer that never reaches the
  solidity confirmation threshold into manual review. It is never credited
  automatically after that timeout.
- `TRON_SCAN_WINDOW_SECONDS`, `TRON_SCAN_OVERLAP_SECONDS`, and the lag are
  deployment settings; keep overlap smaller than the window.

The cursor stores the next timestamp, the in-flight page fingerprint, and the
exact window bounds that must be replayed with that fingerprint. A page with a
fingerprint does not advance the timestamp. The final page advances to
`window_end + 1`. A non-advancing fingerprint, malformed event, provider error,
or database failure leaves the cursor unchanged. Duplicate events are safe.

After a restart, the monitor resumes from the persisted cursor. After database
restore or a prolonged outage:

1. Inspect `python manage.py tron_monitor_status` and the read-only
   `ChainScanCursor` admin page.
2. Let the monitor catch up from the stored timestamp. Do not move the cursor
   backwards without an incident record; backwards movement increases provider
   load but is safe from duplicate credits because event identity is unique and
   ledger credits are idempotent.
3. If a cursor must be repaired, record the old/new values, the reason, and the
   operator in the incident log. Never delete `ChainTransfer` or ledger rows to
   force a reprocess.
4. Run reconciliation before and after recovery.

`TRON_HTTP_RETRIES` and `TRON_HTTP_BACKOFF_SECONDS` control bounded retries for
timeouts, connection failures, HTTP 429, and 5xx responses. `Retry-After` is
honored up to 30 seconds. Authentication and other non-retryable HTTP errors
stop the run without advancing the cursor.

## Confirmation, finality, and attribution

A detected event is persisted before any credit. Before confirmation and again
immediately before credit, the service checks:

- the transaction exists on the Solidity node and the transaction ID matches;
- the receipt result is `SUCCESS`;
- the transaction remains in the detected block;
- the Solidity block hash matches the stored hash when one exists;
- the canonical event at the same event index has the same contract, sender,
  recipient, and exact base-unit amount.

A missing transaction, changed block/hash, failed receipt, changed event, or
amount mismatch is flagged for manual review. A temporarily unindexed event is
a retryable provider error, not a reason to guess. A post-credit reorg detection
flags the transfer and writes an audit event; it does **not** silently edit or
reverse the ledger. Reconciliation and an explicit compensating adjustment are
required.

Attribution is conservative:

- A client-submitted transaction hash is only a claim.
- Unique payable amounts are optional. Even when enabled, a matching amount is
  not proof of ownership.
- Automatic linking requires `TRON_UNIQUE_AMOUNT_ENABLED=1` and
  `TRON_AUTO_MATCH_UNIQUE_AMOUNT=1`, exactly one eligible unexpired request
  with that amount, no competing claims, and no competing amount candidate.
- Automatic crediting requires the additional
  `TRON_AUTO_CREDIT_UNIQUE_AMOUNT=1`; it is independent of automatic matching.
- Otherwise the transfer remains unallocated and enters manual review.
- An administrator may explicitly link a transfer to one deposit after review.
- Exact amount and sufficient solidity confirmations are mandatory for normal
  crediting.
- An administrator may accept a verified on-chain amount that differs from the
  request only through the explicit mismatch override, with a reason. The
  ledger records the actual verified amount. This is audited and only applies
  to a confirmed, canonical direct-TRON transfer in manual review.

## Credit and ledger semantics

The shared `credit_deposit` service is the only path that creates customer
funding balances. It re-reads the deposit under a row lock, checks eligibility
and amount, posts to the immutable double-entry ledger, updates the cached
balance, and is idempotent by deposit reference. A database failure rolls back
the entire credit. The treasury account represents assets expected to be held;
customer balances are liabilities owed to clients. The application does not
prove on-chain treasury holdings and does not sweep funds.

## Reconciliation

Run periodically:

```bash
python manage.py reconcile_deposits
```

It records runs and findings for:

- unallocated or ambiguous chain transfers;
- credited transfers without a credited deposit;
- direct deposits lacking a verified transfer;
- chain/deposit amount mismatches;
- duplicate or inconsistent ledger credits;
- bank requests missing independent verification evidence;
- provider totals versus ledger totals.

Findings never move money. An administrator can mark a finding resolved or
ignored with a reason. Resolve real discrepancies through a separate reviewed
compensating entry or correction, not by editing ledger rows.

## Hosting and scheduling

The repository does not provision a background worker. On Render, create a
separate scheduled job (or an external cron/scheduler) that runs in the same
image and environment as the web service:

```bash
python manage.py scan_tron_deposits
```

Run it every few minutes. Run `python manage.py reconcile_deposits` daily and
`python manage.py tron_monitor_status` for inspection. Do not add monitor work
to every Gunicorn worker or the web startup command.

Database row locks, unique event/ledger references, and the lease-backed
`ScheduledJob` lock prevent overlapping runs from crediting twice. A single
scheduled runner is still preferred to avoid redundant provider load. The
repository does not supply alerting, paging, or an external scheduler;
operations must provide those.

## Testnet exercise

1. Use an isolated database and testnet-only configuration.
2. Keep `TRON_LIVE_VERIFIED=0`.
3. Run `seed_funding_rails --activate-direct-tron` and verify the row values.
4. Create a deposit through the authenticated client API.
5. Send a small test-USDT transfer on the configured testnet.
6. Run the monitor until the event is observed and reaches the confirmation
   threshold.
7. Verify the transfer is attributed or flagged conservatively, then rehearse
   approval and the mismatch path.
8. Test duplicate rescans, provider outage recovery, cursor restart, a moved or
   missing transaction, failed receipts, and reconciliation.

Testnet success is not evidence of mainnet readiness.

## Controlled mainnet activation

Do not enable all switches at once.

1. Back up the database and apply migrations through the reviewed deployment
   process. Migrations `0005`–`0009` are additive.
2. Configure the public address, pinned contract, HTTPS provider, and network
   rows. Leave deposits and monitoring disabled.
3. Enable detection only (`TRON_DEPOSITS_ENABLED=1`) and run the monitor in a
   controlled window. Confirm events are observed without credits.
4. Keep `TRON_LIVE_VERIFIED=0` until an operator has independently verified the
   address, contract, provider response shape, confirmations, restart behavior,
   and reconciliation.
5. Set `TRON_LIVE_VERIFIED=1` and `FUNDING_LIVE_VERIFIED=1` for a small,
   monitored live test after the switch-off and rollback procedures are agreed.
6. Start with automatic matching/crediting **off**. Use manual review and a
   small test amount.
7. Run reconciliation, review audit logs, and only then consider enabling
   automatic matching and crediting separately.

## Known limitations

- No live mainnet TronGrid interoperability has been executed in this
  repository; validate the provider response shape against the operator's
  actual provider.
- The web process does not run the monitor; scheduler and alerting are
  deployment responsibilities.
- Post-credit reorg handling flags and audits but does not automatically reverse
  customer money.
- Suspicious or ambiguous transfers remain uncredited pending support and
  operations review; there is no automatic correction.
- The shared-address model depends on outside-the-app accounting controls and
  operational discipline for unusual payments.
- Tests use Django's test database. Production Postgres row-lock behavior should
  be verified during staging.

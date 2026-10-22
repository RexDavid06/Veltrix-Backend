# VELTRIX — Multi-Cryptocurrency Deposits & Secure Custody: Technical Design

| Field | Value |
|---|---|
| Status | **Draft for human approval** (design only) |
| Scope | Multi-asset crypto deposit ingestion, double-entry ledger, custody/treasury, application integration, failure recovery, rollout |
| Repositories | `~/Veltrix-Backend` (Django 5.2.18 + DRF 3.18.3), `~/Veltrix-Frontend` (React + Vite) |
| Constraint | No application code, migrations, packages, settings, or financial transactions were changed to produce this document. The only new file is this document. |
| Related audit | Phase 1 frontend audit + Phase 2 backend audit (results cited throughout) |

**Legend used everywhere in this document**

- **[V]** — Verified from repository source code at the time of writing.
- **[P]** — Proposed design (not implemented; requires approval).
- **[A]** — Assumption or inference that must be confirmed by a human before it is relied upon.

> Repository facts are stated as **verified**. Everything about the new subsystem is **proposed** until a human approves it and code is written. Regulatory, provider-availability and jurisdiction statements are **assumptions** requiring legal/compliance confirmation.

---

## 0. Executive Summary

VELTRIX today is a **paper-trading simulator**. The only balance is `DemoAccount.cash_balance`, an unbacked `Decimal(18,2)` USD scalar that any authenticated user can increase via `POST /api/portfolio/topup/` with no payment verification and no idempotency **[V]**. The README explicitly states there is no real fund movement, no payment provider and no custody integration **[V]**. No asset/network, deposit, ledger, wallet, webhook or treasury model exists **[V]**.

This document specifies how to add real multi-cryptocurrency deposits **without** converting simulated balances into real money and **without** breaking existing auth, simulated trading, history or admin.

**Core design positions**

1. **A proper double-entry ledger** (append-only, always balanced) becomes the source of truth for *real* assets. `DemoAccount.cash_balance` is left untouched as the *simulated* USD balance; the two live side-by-side and are never merged implicitly **[P]**.
2. **Deposits are credited only after independent on-chain verification and the configured confirmation threshold**, never on the strength of a webhook alone **[P]**.
3. **Idempotency and replay protection are database constraints**, not application checks **[P]**.
4. Custody starts with **one test network** (testnet/faucet only, no real keys) and a **recommended managed-custody or single-tenant KMS-backed approach**, subject to provider eligibility and regulatory review in Nigeria **[P/A]**.
5. Existing simulated `TopUpAPIView` is **isolated and gated off in production** via a setting, with tests preserved **[P]**.

**The single most important blocker** (from the audit): there is no independently verified, idempotent deposit layer, and no ledger backing the existing balance — so a design must introduce both before any real funds can be accepted. This document is that design.

---

## 1. Current-State Facts This Design Must Respect

All rows below are **[V]** unless marked.

### 1.1 Backend models (`veltrix_app/models.py`)

| Model | Key fields | Role today |
|---|---|---|
| `User(AbstractUser)` | `role` (client/admin), `is_verified` | Identity; **no balance** |
| `DemoAccount` | `OneToOne(user)`, `currency="USD"`, `cash_balance Decimal(18,2)` | **Only balance store — simulated USD** |
| `Instrument` | `symbol`, `asset_class`, `price Decimal(18,8)` | Tradable symbols; no chain/network concept |
| `Position` | `unique(user,instrument)`, `quantity Decimal(24,8)`, `avg_price Decimal(18,8)` | Simulated holdings |
| `Transaction` | `user`, `symbol`, `side` (buy/sell/topup), `quantity`, `execution_price`, `amount Decimal(18,2)`, `status` (simulated/pending/completed) | **Flat, single-row activity log — not double-entry** |
| `AuditLog` | `actor`, `user`, `action`, `details`, `created_at` | Admin audit trail |
| `SupportTicket`, `Conversation`, `Message`, `WatchlistItem` | — | Unrelated to funds |

No `Asset`, `Network`, `DepositAddress`, `ChainDeposit`, `Ledger*`, `WebhookEvent`, `Withdrawal`, `Custody` models exist **[V]**.

### 1.2 Funding & trading flow

- `POST /api/portfolio/topup/` → `TopUpAPIView` (`veltrix_app/api/views.py:490-540`): `IsAuthenticated`; parses `Decimal`; requires finite, `> 0`, `<= 1_000_000`; inside `transaction.atomic()` does `get_or_create` → `select_for_update()` → `cash_balance += amount` → writes `Transaction(side="topup", status="simulated", note="Demo top-up — no real funds credited.")` → returns `demo: true` **[V]**.
- `POST /api/orders/` → `OrderAPIView` (`views.py:383-487`): simulated buy/sell against `Instrument.price` with atomic + row locks **[V]**.
- `DashboardAPIView` (`views.py:115-133`) returns `account.cash_balance` to the frontend **[V]**.
- `HealthAPIView` returns `"mode": "simulation"` **[V]**.

### 1.3 Security posture already present (to preserve)

- SimpleJWT (60-min access, 1-day refresh, rotation + blacklist) `settings.py:222-229` **[V]**.
- `IsPlatformAdmin` on all `/api/admin/*`, `IsSupportStaff` for conversation moderation `permissions.py` **[V]**.
- Audit log written in the same transaction as admin mutations; `AuditLogMiddleware` logs authenticated writes **[V]**.
- Throttling anon 20/min, user 1000/day `settings.py:212-219` **[V]**.
- Decimal-based money math and `atomic()` + `select_for_update()` on funding **[V]**.
- Secrets env-only; `.env` git-ignored; no secrets committed **[V]**.

### 1.4 Integration & ops reality

- Only external integration is **yfinance market quotes** (`services/market.py`), gated by `MARKET_USE_LIVE` **[V]**.
- `channels`/`channels-redis` are installed but **not wired** (`asgi.py` is plain `get_asgi_application()`; frontend `api.js` comment confirms no Channels routing) **[V]**.
- **No Celery / no scheduler / no background worker** **[V]**. DB is Postgres via `DATABASE_URL` in production, SQLite fallback locally **[V]**. `select_for_update()` is effective only on Postgres **[V for code; A that production uses Postgres]**.
- Frontend already defines `wallet` and `deposits` routes (`Veltrix-Frontend/src/routing.js:11,33`) but no real deposit screen exists; `TopUpModal.jsx` is the demo top-up UI **[V]**.

---

## 2. Target Architecture

### 2.1 Component overview

```mermaid
flowchart TB
    subgraph Chain["Blockchains / Provider"]
        RPC[Chain RPC / Indexer / Custody API]
        WH[Provider Webhook Sender]
    end

    subgraph Ingest["Deposit Ingestion (Django)"]
        WHK[Webhook receiver\nsignature + replay checks]
        WATCH[Chain watcher / verifier\n(worker, scheduled)]
        DET[Deposit detector\nmatch address + txid]
    end

    subgraph Core["Financial Core"]
        LEDGER[(Double-entry Ledger\nappend-only postings)]
        CACHE[(Balance cache\nreconciled to ledger)]
        RECON[Reconciliation job]
    end

    subgraph App["Existing App (preserved)"]
        AUTH[JWT auth]
        DEMO[DemoAccount.cash_balance\nsimulated USD]
        TRADE[Simulated orders/Transaction]
        ADMIN[Admin API + AuditLog]
    end

    subgraph Custody["Custody / Treasury"]
        KMS[Key manager / HSM]
        HOT[Hot wallet]
        COLD[Cold storage]
    end

    RPC --> WATCH
    WH --> WHK
    WHK --> DET
    WATCH --> DET
    DET --> LEDGER
    LEDGER --> CACHE
    LEDGER --> RECON
    RPC --> RECON
    RECON --> ADMIN
    LEDGER --> ADMIN
    DEMO -. "separate, never merged" .- LEDGER
    TRADE --> DEMO
    CACHE --> ADMIN
    KMS --> HOT
    HOT --> RPC
    COLD --> HOT
```

### 2.2 Design principles

- **Separation of concerns:** chain I/O (ingest) ≠ financial truth (ledger) ≠ presentation (API/cache). **[P]**
- **Fail closed:** if verification, confirmation or signature checks are inconclusive, the deposit stays pending; nothing is credited. **[P]**
- **Append-only money:** no destructive updates to postings; corrections are new reversing entries. **[P]**
- **Demo/real isolation by construction:** real assets live in the ledger keyed by `Asset`; demo USD lives in `DemoAccount`. **[P]**
- **Idempotency at the database layer** for every externally-triggered event. **[P]**

---

## 3. Double-Entry Ledger

### 3.1 Chart of accounts

Account codes are hierarchical (`type:code`). Each account is scoped by `asset` and, for customer accounts, by `user`.

| Code | Account | Type | Normal side | Purpose |
|---|---|---|---|---|
| `1000` | `TREASURY:{asset}` | Asset | Debit | On-chain/controlled asset holdings for that asset |
| `1010` | `DEPOSITS_IN_TRANSIT:{asset}` | Asset | Debit | Detected/confirmed funds not yet swept to treasury |
| `1020` | `WITHDRAWALS_IN_TRANSIT:{asset}` | Asset | Debit | Assets broadcast but not final on-chain |
| `1090` | `CUSTOMER_RECEIVABLE:{asset}` | Asset | Debit | Owed by customers after a reversal/overdraft |
| `2000` | `CUSTOMER:{user}:{asset}` | Liability | Credit | What VELTRIX owes a specific customer |
| `2010` | `WITHDRAWALS_PAYABLE:{asset}` | Liability | Credit | Approved but unbroadcast withdrawals |
| `2090` | `ADJUSTMENT_CLEARING:{asset}` | Equity/Clearing | — | Suspense for manual corrections (requires approval) |
| `3000` | `RECONCILIATION_CLEARING:{asset}` | Equity/Clearing | — | Absorbs confirmed chain↔ledger differences pending investigation |
| `4000` | `DEPOSIT_FEE_REVENUE:{asset}` | Revenue | Credit | Platform deposit fees |
| `4010` | `WITHDRAWAL_FEE_REVENUE:{asset}` | Revenue | Credit | Platform withdrawal fees |
| `5000` | `NETWORK_FEE_EXPENSE:{asset}` | Expense | Debit | Gas/transaction fees paid by the platform |
| `5090` | `ADJUSTMENT_LOSS:{asset}` | Expense | Debit | Write-offs / irrecoverable discrepancies |

> **Model:** every posting belongs to one `LedgerEntry` and references exactly one `LedgerAccount`. `LedgerAccount` is unique on `(asset, code[, user])`. We do **not** hard-code "accounts" as hashed single rows for users; instead one `LedgerAccount` row per `(user, asset, CUSTOMER)` keeps aggregation simple and index-friendly. **[P]**

### 3.2 The balancing invariant

For every `LedgerEntry`, the sum of postings must be zero when signed by `normal_side`:

```
Σ ( posting.amount × sign(account.normal_side) )  ==  0
   amount is Decimal(38,18), always > 0; the side is derived from the account, not the magnitude
```

The posting service computes the sum before insert and raises `UnbalancedEntry` otherwise. A Postgres deferred constraint trigger re-checks at commit time as defence in depth **[P]**.

### 3.3 Immutability

- **No `UPDATE`/`DELETE` on `LedgerEntry` or `LedgerPosting`.** Enforced by (a) model `save()`/`delete()` overrides that raise after creation, and (b) a Postgres rule/trigger rejecting `UPDATE`/`DELETE` on the tables. **[P]**
- Corrections are **reversing entries**: a new `LedgerEntry` whose postings mirror the original and reference it via `reverses_entry_id`. **[P]**
- `LedgerEntry` is created only through the single service function `record_entry(...)`; views never write postings directly. **[P]**

### 3.4 Balance derivation and safe caching

- **Derived balance** (authoritative): `Σ credits − Σ debits` over `CUSTOMER:{user}:{asset}` postings (liability sign). **[P]**
- **Cache** `AssetBalance(user, asset, available, pending, updated_at)` is updated **inside the same DB transaction** as the posting, using `select_for_update()` on the cache row. **[P]**
- Any spend/withdrawal **must** lock the cache row and assert `available >= amount`, else reject. **[P]**
- A **verification job** periodically recomputes each balance from the ledger and compares it to the cache; any mismatch raises an alert and marks the account `RECONCILIATION_CLEARING` pending investigation. The cache is never the source of truth in disputes — the ledger is. **[P]**

### 3.5 Example balanced entries (all balanced)

Signs shown as `Dr` (debit) / `Cr` (credit). Amounts in the asset's base units.

**E1 — Deposit credited after finality (0.5% platform fee)**

| Account | Dr | Cr |
|---|---|---|
| `1000 TREASURY:BTC` | 1.00000000 | |
| `2000 CUSTOMER:u42:BTC` | | 0.99500000 |
| `4000 DEPOSIT_FEE_REVENUE:BTC` | | 0.00500000 |

`Dr 1.0 = Cr 0.995 + 0.005` ✅

**E2 — Network fee paid by platform to sweep funds**

| Account | Dr | Cr |
|---|---|---|
| `5000 NETWORK_FEE_EXPENSE:BTC` | 0.00012000 | |
| `1000 TREASURY:BTC` | | 0.00012000 |

✅

**E3 — Withdrawal requested and approved (amount 2.0, fee 0.01)**

| Account | Dr | Cr |
|---|---|---|
| `2000 CUSTOMER:u42:BTC` | 2.01000000 | |
| `4010 WITHDRAWAL_FEE_REVENUE:BTC` | | 0.01000000 |
| `2010 WITHDRAWALS_PAYABLE:BTC` | | 2.00000000 |

✅

**E4 — Withdrawal broadcast (assets leave hot wallet)**

| Account | Dr | Cr |
|---|---|---|
| `2010 WITHDRAWALS_PAYABLE:BTC` | 2.00000000 | |
| `1000 TREASURY:BTC` | | 2.00000000 |

✅

**E5 — Confirmed deposit reversed (chain reorg after credit)**

| Account | Dr | Cr |
|---|---|---|
| `2000 CUSTOMER:u42:BTC` | 1.00000000 | |
| `1000 TREASURY:BTC` | | 1.00000000 |

✅ — if the customer already spent the funds, `CUSTOMER:BTC` would go debit (negative liability). Instead we post:
`Dr 1090 CUSTOMER_RECEIVABLE:BTC / Cr 2000 CUSTOMER:u42:BTC` for the shortfall and flag an overdraft (see §5.8).

**E6 — Manual adjustment (suspense, two approvals)**

| Account | Dr | Cr |
|---|---|---|
| `3000 RECONCILIATION_CLEARING:BTC` | 0.00030000 | |
| `5090 ADJUSTMENT_LOSS:BTC` | | 0.00030000 |

✅ — every adjustment references a ticket + two admin approvers in `AuditLog`.

### 3.6 Interaction with `DemoAccount.cash_balance` and `Transaction`

This is the critical boundary. **Do not convert simulated balances into real funds.** **[V] policy / [P] enforcement**

- `DemoAccount.cash_balance` remains the **simulated USD** balance, used only by `DashboardAPIView`, `TopUpAPIView`, and simulated `OrderAPIView`. It is **never** debited or credited by deposit ingestion, the ledger, or any chain event **[P]**.
- Real assets are represented **only** as `CUSTOMER:{user}:{asset}` ledger accounts. USD stablecoins (USDT/USDC) are distinct `Asset` rows — they do **not** reuse `cash_balance` **[P]**.
- The existing `Transaction` model remains the **simulated trading/activity log**. Real deposits/withdrawals are recorded in `ChainDeposit`/`Withdrawal` + ledger, and are surfaced to the existing admin `Transaction` API only through a **separate, clearly-labelled read model** (see §7), never by writing `side="topup"` rows **[P]**.
- UI shows a hard visual boundary: a **"Demo / Simulated"** area (existing) and a **"Wallet / Real (testnet)"** area. `DemoBanner.jsx` already exists and can be extended **[V for file / P for change]**.
- Migration/backfill: **none required and none recommended.** Existing demo balances are left exactly as-is. The destructive `0002_zero_existing_accounts` migration is historical **[V]**.

---

## 4. Deposit Ingestion Pipeline

### 4.1 Asset and network configuration

`Asset` **[P]**
- `code` (e.g. `BTC`, `ETH`, `USDT`), `display_name`, `decimals` (base-unit precision), `is_active`.

`Network` **[P]**
- `asset` FK, `chain` (e.g. `bitcoin`, `ethereum`, `tron`), `token_standard` (`native`/`erc20`/`trc20`…), `contract_address` (nullable), `decimals`, `required_confirmations`, `min_deposit`, `dust_threshold`, `is_testnet`, `is_active`.
- `unique(asset, chain, token_standard, contract_address)`.
- **Testnet first:** every `Network` has `is_testnet`; go-live requires an explicit per-network approval flag.

### 4.2 Deposit address assignment

`DepositAddress` **[P]**
- `user` FK, `network` FK, `address`, `memo_or_tag` (nullable, for chains like XRP/XLM/TON), `provider_ref` (nullable), `created_at`, `is_active`.
- `unique(network, address)`; recommended `unique(user, network, is_active)` **partial** index so each user has one active address per network/asset.
- **Two approved strategies** (decision required, §14):
  1. *Managed provider:* provider issues per-user addresses (or sub-accounts/memos); we store the mapping.
  2. *Self-hosted:* derive from an HD wallet (xpub held by app, private keys held in KMS/HSM, ideally never in the app DB). Address index tracked per `(network, account_index, address_index)`.
- **Security:** addresses are public data; the private key/xpub is a secret and is held outside the repo/DB. Generating new addresses is idempotent by index; address reuse is avoided where the chain allows.

### 4.3 Chain transaction identification

`ChainDeposit` **[P]**
- `network` FK, `txid`, `vout`/`log_index` (nullable for account-model chains), `to_address`, `from_address` (nullable), `amount Decimal(38,18)`, `block_height` (nullable), `confirmations` (int), `status` (state machine §4.7), `raw_reference`, `detected_at`, `credited_at`.
- **`unique(network, txid, vout)`** — the primary idempotency key for chain events.
- Detection source of truth is the **chain** (RPC/indexer) or a **provider API**; a webhook is a *trigger to go look*, not the source of truth.

### 4.4 Confirmation and finality policy

- Each `Network.required_confirmations` defines finality. Deposit is credited only at `confirmations >= required_confirmations` **and** independent verification passes. **[P]**
- Finality is monotonic in normal operation; a **decrease** in confirmations (or `block_height` no longer canonical) triggers the reorg path (§4.8). **[P]**
- Recommended defaults (to be approved): Bitcoin 3–6, Ethereum 12–64, TRON 19–20, stablecoin L2s per bridge finality. Values are configuration, not constants. **[A]**

### 4.5 Authenticated webhook processing

`WebhookEvent` **[P]**
- `provider`, `event_id` (**unique** — replay protection), `signature_valid` (bool), `payload_sha256`, `received_at`, `processed_at`, `status`, `raw_body` (stored encrypted/redacted).
- Receiver `POST /api/webhooks/<provider>/` is `AllowAny` at the HTTP layer but:
  1. Reads the **raw body** (no re-serialization before hashing).
  2. Verifies **HMAC signature** with a provider secret (constant-time compare) and a **timestamp window** (e.g. ±5 min) to bound replay.
  3. Inserts `WebhookEvent` with `event_id` unique; duplicate → return `200` without reprocessing (idempotent ack).
  4. Returns `2xx` **immediately**, then enqueues processing. It never credits synchronously from the webhook body alone.
- Webhook secret stored in env/secret manager; never in DB or logs **[P]**.

### 4.6 Independent transaction verification

Before credit, the verifier **independently** fetches the transaction from the chain/provider API and asserts:
1. `txid` exists and is canonical.
2. `to_address` belongs to a known `DepositAddress` (ours) — never trust the webhook's claimed address.
3. `amount` and `asset/network` match.
4. `confirmations >= required_confirmations`.
5. No conflicting `ChainDeposit` for the same `(network, txid, vout)` (idempotency).

If webhook and independent verification **disagree**, the deposit goes to `MANUAL_REVIEW`; nothing is credited. **[P]**

### 4.7 Deposit lifecycle and permitted state transitions

`ChainDeposit.status` **[P]**

```
                       ┌──────────────► ORPHANED (reorg before credit)
                       │
DETECTED ──► CONFIRMING ──► CONFIRMED ──► CREDITED ──► SWEPT
   │              │              │            │
   │              │              │            └──► REORGED (post-credit reversal)
   │              │              └──► MANUAL_REVIEW
   ├──► UNDERPAID ─► MANUAL_REVIEW
   ├──► OVERPAID  ─► (credit actual) MANUAL_REVIEW if large
   └──► DUPLICATE ─► (merge into existing deposit, no new credit)
```

| From | To | Guard |
|---|---|---|
| DETECTED | CONFIRMING | address is ours; txid canonical; amount ≥ dust |
| CONFIRMING | CONFIRMED | `confirmations ≥ required`; verification passes |
| CONFIRMED | CREDITED | atomic ledger posting + cache update succeed; entry idempotent |
| CREDITED | SWEPT | funds moved treasury↔hot per policy (optional; may be off-ledger if same account) |
| DETECTED/CONFIRMING | ORPHANED | tx no longer canonical before credit |
| CREDITED | REORGED | canonical loss after credit → reversing entry (§4.8) |
| any | MANUAL_REVIEW | mismatch, underpayment, large overpayment, verification ambiguity |
| duplicate event | *(no new record)* | `unique(network,txid,vout)` or `WebhookEvent.event_id` hit |

Transitions are performed by a single service guarded by `select_for_update()` and are **monotonic** except for explicit documented reversals. Illegal transitions raise and are logged. **[P]**

### 4.8 Edge cases

- **Underpayment:** amount below `Network.min_deposit` → record as `UNDERPAID`, do **not** credit, open a manual/refund case. Between `min_deposit` and expected? The expected amount is not authoritative (addresses are reusable); we credit the **actual received amount** ≥ `min_deposit`. **[P]**
- **Overpayment:** credit the **actual** amount; flag large deviations for review. Never credit the "expected" value. **[P]**
- **Duplicate notifications:** idempotency keys (`WebhookEvent.event_id`, `unique(network,txid,vout)`) collapse duplicates to one credit. **[P]**
- **Chain reorganization:** if confirmations decrease below threshold or the tx leaves the canonical chain:
  - Before credit: `ORPHANED`, no ledger impact.
  - After credit: post a reversing entry (E5). If resulting customer balance < 0, create `CUSTOMER_RECEIVABLE` + overdraft flag rather than silently allowing negative liability; block withdrawals for that account and alert. **[P]**
- **Dust / spam:** ignore amounts below `dust_threshold`. **[P]**
- **Reused address across users:** prevented by `unique(network, address)` mapping to exactly one `DepositAddress`. **[P]**

---

## 5. Custody and Treasury

### 5.1 Managed vs self-hosted comparison

| Dimension | Managed custody / MPC provider | Self-hosted wallet infra |
|---|---|---|
| Key isolation | Provider HSM/MPC; no key material in VELTRIX | Requires your own KMS/HSM; keys are your sole responsibility |
| Signing perms | Policy engine, approval workflows, quorum signing | You build approvals/policies |
| Asset & network coverage | Broad, provider-maintained | You maintain node/indexer per chain |
| Withdrawals | Policy + allowlist + approvals | Fully custom; highest risk of mistake |
| Auditability | Provider logs + our ledger | Full control, full burden |
| Regulatory/KYC | Provider often handles some KYC/AML; eligibility gates | VELTRIX is the custodian — heavier obligations |
| Cost | Setup + per-tx/custody fees | Infra + ops + security headcount |
| Operational complexity | Lower for a small team | High (nodes, monitoring, upgrades, key ops) |
| Single point of failure | Provider outage | Your infra outage |

### 5.2 Recommendation (subject to review)

For a **small team operating from Nigeria**, the lowest *initial* risk is a **managed custody / MPC provider that is available and eligible in the operating jurisdiction**, combined with a **testnet-only pilot** and a strict internal treasury policy. **[P/A]**

However — **do not assume any provider is available or approved.** Provider eligibility (Nigeria support, KYC requirements, sanctions screening, entity/registration requirements) must be verified with the provider and with counsel. If no eligible provider exists, the fallback is **single-tenant self-hosted** with: keys in a cloud KMS/HSM (never in the app DB), hot wallet limited to a small float, cold storage for the bulk, quorum/two-person approval, and per-tx limits. **[P]**

### 5.3 Regulatory note (assumption — must be confirmed)

Operating a crypto deposit/custody service from Nigeria likely engages **SEC Nigeria** (digital asset rules), **CBN** (banking/payment rules), and AML/CFT/Travel-Rule obligations; stablecoin and custody treatment may change. **This document makes no legal claim.** A compliance review is a hard prerequisite before any mainnet/real-funds go-live. **[A]**

### 5.4 Monero (XMR) — separate evaluation

XMR is **excluded from the initial scope** and evaluated separately because:
- Privacy features raise heightened AML/CFT and exchange-listing/compliance friction **[A]**.
- Requires dedicated node/light-wallet infrastructure and careful handling of subaddresses/payment IDs **[A]**.
- Fewer managed custodial providers support XMR; some restrict it **[A]**.

Decision: **do not support XMR in phases 1–4**; revisit only after compliance sign-off and a dedicated infra design. **[P]**

---

## 6. Existing Application Compatibility

### 6.1 Backend — affected files (preserve behaviour)

| File | Impact | Rule |
|---|---|---|
| `veltrix_app/models.py` | Add new models only; **do not alter** `DemoAccount`, `Transaction`, `User` semantics | Additive migration **[P]** |
| `veltrix_app/api/views.py` (`TopUpAPIView`) | Gate behind setting; keep demo behaviour for tests | No breaking change **[P]** |
| `veltrix_app/api/views.py` (`DashboardAPIView`) | Keep `account.cash_balance` as simulated; **add** wallet balances separately | Additive field/section **[P]** |
| `veltrix_app/api/urls.py` | Add wallet + webhook routes | Additive **[P]** |
| `veltrix_app/api/serializers.py` | Add new serializers; leave existing ones intact | Additive **[P]** |
| `veltrix_app/api/admin_views.py` / `admin_urls.py` | Add read-only crypto deposit/recon views | Additive, `IsPlatformAdmin` **[P]** |
| `veltrix_project/settings.py` | Add `FUNDING_DEMO_TOPUP_ENABLED`, provider secrets, worker config | Toggle-based, secure defaults **[P]** |
| Existing tests | Must still pass unchanged | Regression gate **[P]** |

### 6.2 Frontend — affected files

| File | Impact |
|---|---|
| `src/routing.js` | `wallet`/`deposits` routes already exist **[V]**; wire components |
| `src/App.jsx` (`handleTopUp` 538–547, `cashBalance` 853) | Keep demo top-up; add wallet data fetch **[V]** |
| `src/components/TopUpModal.jsx` | Keep, but label explicitly as **demo/simulated** **[V]** |
| `src/components/DemoBanner.jsx` | Reuse for demo/real boundary **[V]** |
| `src/components/AppViews.jsx` | Add Wallet/Deposits views (new components) **[V]** |
| `src/api.js` | Add wallet service calls; `API_ROOT` pattern reused **[V]** |
| New: `DepositAddressCard.jsx`, `DepositHistoryTable.jsx`, `WalletView.jsx` | Proposed **[P]** |

### 6.3 Isolating demo top-ups and disabling safely in production

- Introduce `FUNDING_DEMO_TOPUP_ENABLED` (default **False** when `DEBUG=False`; **True** in tests) **[P]**.
- `TopUpAPIView` returns `403`/`410` when disabled, with a clear message, while the demo path remains fully exercised by tests when enabled **[P]**.
- Real deposits are **never** routed through `TopUpAPIView`. They use new wallet endpoints. **[P]**
- This preserves every existing test (`test_topup_credits_demo_cash_and_records_transaction`, etc.) by enabling the flag in the test settings **[V for tests / P for gating]**.

---

## 7. Failure Recovery, Concurrency and Security

### 7.1 Database transaction boundaries and concurrency

- Each externally-triggered credit is **one atomic unit**: insert/update `ChainDeposit` → `record_entry()` → update `AssetBalance` cache → write `AuditLog`. Any failure rolls back all of it. **[P]**
- `select_for_update()` on: the `ChainDeposit` row, the `AssetBalance` cache row, and the target `LedgerAccount` (or a per-account advisory lock). **[P]**
- Postgres is required for correctness (row locks + partial unique indexes + triggers); SQLite dev must not be used for real funds. **[P]**

### 7.2 Unique constraints and idempotency keys

| Constraint | Protects against |
|---|---|
| `WebhookEvent.event_id` unique | duplicate/replayed webhook |
| `ChainDeposit (network, txid, vout)` unique | duplicate chain event / double credit |
| `LedgerEntry.reference (type,id)` unique | replaying a business operation |
| `DepositAddress (network, address)` unique | address collision/exfiltration |
| `AssetBalance (user, asset)` unique | multiple cache rows / lost updates |

### 7.3 Webhook signature verification and replay prevention

HMAC over the **raw** body + timestamp; constant-time compare; timestamp window; `event_id` uniqueness; optional provider IP allowlist; secrets in env/secret manager; failed signatures logged but never trusted; processing asynchronous after a `200` ack. **[P]**

### 7.4 Ledger reconciliation and discrepancy alerts

Periodic job:
1. Recompute each `CUSTOMER` balance from postings; compare to `AssetBalance` cache → mismatch ⇒ alert + freeze account.
2. Compare `TREASURY:{asset}` ledger total to the **actual on-chain balance** of controlled addresses → drift ⇒ `RECONCILIATION_CLEARING:{asset}` memo + alert.
3. Verify every `CREDITED` deposit still exists on-chain and is canonical → possible reorg detection.
4. Verify no unbalanced `LedgerEntry` exists (defensive).
Alert channels: admin dashboard banner + email/log; discrepancies never auto-silently adjusted — they require human approval. **[P]**

### 7.5 Retry queues, failed jobs and manual recovery

- No Celery today **[V]**. Options: add a worker (Celery/RQ) or a DB-backed job table polled by a management command (`python manage.py run_deposit_worker`). Recommendation: **DB-backed `Job` table first** (no new infra) with bounded retries/backoff and a dead-letter state. **[P]**
- Each job is idempotent; retries re-run verification and re-attempt the same posting (which is a no-op if already credited). **[P]**
- Manual recovery tools (admin, audited): re-run verification for a deposit, force `MANUAL_REVIEW` resolution, retry a failed job, post an approved adjustment. All require two admins for value-changing actions. **[P]**

### 7.6 Authorization, audit, key management, emergency controls

- **AuthZ:** wallet reads require `IsAuthenticated` (own data only); admin crypto endpoints require `IsPlatformAdmin`; webhook endpoint is signature-gated, not JWT. **[P]**
- **Audit:** every state transition, approval, and manual action writes `AuditLog` in-transaction; ledger entries are immutable and self-auditing. **[P]**
- **Key management:** private keys/xpub never in DB or repo; KMS/HSM or provider MPC; key access requires quorum; key operations logged; rotation and break-glass procedures documented. **[P]**
- **Emergency controls (kill switches):** `FUNDING_DEPOSITS_ENABLED`, `FUNDING_WITHDRAWALS_ENABLED`, per-asset `Asset.is_active`, and a global `CUSTODY_FROZEN` flag. Freezing stops crediting/withdrawals but never corrupts the ledger. **[P]**
- **Withdrawal policy:** allowlist, per-tx and daily limits, cooling-off for new addresses, two-person approval, hot-wallet float cap. **[P]**

---

## 8. Threat Analysis

| Threat | Vector | Mitigation |
|---|---|---|
| Forged deposit credit | Attacker calls a funding endpoint | No public credit endpoint; credits only from verified chain events |
| Webhook forgery | Fake provider callback | HMAC + timestamp + independent on-chain verification |
| Webhook replay | Re-send captured callback | `event_id` unique + timestamp window |
| Double credit | Duplicate notification/retry | `unique(network,txid,vout)` + idempotent posting |
| Reorg double-credit | Credit then chain drops tx | Confirmation threshold + reversal postings + overdraft handling |
| Key compromise | Leaked key/xpub | KMS/HSM/MPC, no keys in DB/repo, quorum signing, cold storage |
| Insider fraud | Admin credits/adjustments | Two-person approval, immutable ledger, full audit, limits |
| Address collision | Two users share deposit address | `unique(network,address)` mapping |
| RPC/API MITM | Tampered chain data | TLS pinning/allowlist, cross-check provider vs node |
| Race condition | Concurrent spend/deposit | Row locks + derived-balance verification + cache assertion |
| DB tampering | Direct writes | Immutable postings (trigger), least-privilege DB roles, backups |
| DoS on webhook | Flood receiver | Fast ack + async processing + throttle + dedupe |
| Demo/real confusion | UI or API merge | Separate models, separate endpoints, explicit labelling |
| Overdraft after reorg | Spend of unconfirmed funds | Block spending pre-finality; receivable + freeze on negative |
| Secrets leakage | Logs/env | No secrets in logs; env/secret manager; redaction |

---

## 9. API Contracts (proposed)

### 9.1 Customer (JWT required)

```
GET  /api/wallet/assets/                 -> [{code, display_name, decimals, networks:[{chain,required_confirmations,min_deposit,is_testnet}]}]
GET  /api/wallet/addresses/              -> [{asset, network, address, memo, created_at}]           (creates if missing, idempotent)
GET  /api/wallet/balances/               -> [{asset, available, pending, updated_at}]
GET  /api/wallet/deposits/               -> paginated [{id, asset, network, amount, confirmations, required, status, txid, created_at}]
GET  /api/wallet/deposits/<id>/          -> single deposit detail (own only)
```

No customer endpoint ever accepts an amount to credit. **[P]**

### 9.2 Admin (`IsPlatformAdmin`)

```
GET  /api/admin/crypto/deposits/                 -> all deposits (filters: asset, network, status, date)
GET  /api/admin/crypto/deposits/<id>/            -> detail + chain evidence + posting ids
POST /api/admin/crypto/deposits/<id>/reverify/   -> re-run verification (idempotent)
GET  /api/admin/crypto/webhook-events/           -> webhook audit (signature status, dedupe)
GET  /api/admin/crypto/reconciliation/           -> ledger vs chain drift report
POST /api/admin/crypto/adjustments/              -> approved adjustment (2 approvers, audited)
POST /api/admin/crypto/freeze/                   -> toggle custody kill switch (audited)
```

### 9.3 Webhook (signature, no JWT)

```
POST /api/webhooks/<provider>/   -> 200 ack (dedupe), body verified, processing async; never credits synchronously
```

### 9.4 Example deposit detail response

```json
{
  "id": 91,
  "asset": "BTC",
  "network": "bitcoin-testnet",
  "amount": "1.00000000",
  "confirmations": 3,
  "required_confirmations": 3,
  "status": "CREDITED",
  "txid": "9c1f...",
  "to_address": "tb1q...",
  "credited_at": "2026-01-01T00:00:00Z",
  "ledger_entry_id": 4421
}
```

---

## 10. Proposed Data Models (Django sketch — not implemented)

```python
class Asset(models.Model):
    code = models.CharField(max_length=16, unique=True)
    display_name = models.CharField(max_length=64)
    decimals = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=False)

class Network(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="networks")
    chain = models.CharField(max_length=32)
    token_standard = models.CharField(max_length=16, default="native")
    contract_address = models.CharField(max_length=128, blank=True, default="")
    decimals = models.PositiveSmallIntegerField()
    required_confirmations = models.PositiveIntegerField()
    min_deposit = models.DecimalField(max_digits=38, decimal_places=18)
    dust_threshold = models.DecimalField(max_digits=38, decimal_places=18, default=0)
    is_testnet = models.BooleanField(default=True)
    is_active = models.BooleanField(default=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["asset","chain","token_standard","contract_address"], name="uniq_network")]

class LedgerAccount(models.Model):
    NORMAL = [("D","Debit"),("C","Credit")]
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    code = models.CharField(max_length=8)                 # 1000..5090
    user = models.ForeignKey("veltrix_app.User", null=True, blank=True, on_delete=models.PROTECT)
    normal_side = models.CharField(max_length=1, choices=NORMAL)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["asset","code","user"], name="uniq_ledger_account")]

class LedgerEntry(models.Model):
    reference_type = models.CharField(max_length=32)      # deposit|withdrawal|reversal|adjustment
    reference_id = models.CharField(max_length=64)
    reverses_entry = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["reference_type","reference_id"], name="uniq_ledger_entry_ref")]

class LedgerPosting(models.Model):
    entry = models.ForeignKey(LedgerEntry, on_delete=models.PROTECT, related_name="postings")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=38, decimal_places=18)   # always > 0
    class Meta:
        constraints = [models.UniqueConstraint(fields=["entry","account"], name="uniq_posting_account")]

class AssetBalance(models.Model):     # cache, reconciled to ledger
    user = models.ForeignKey("veltrix_app.User", on_delete=models.PROTECT)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    available = models.DecimalField(max_digits=38, decimal_places=18, default=0)
    pending = models.DecimalField(max_digits=38, decimal_places=18, default=0)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["user","asset"], name="uniq_asset_balance")]

class DepositAddress(models.Model):
    user = models.ForeignKey("veltrix_app.User", on_delete=models.PROTECT)
    network = models.ForeignKey(Network, on_delete=models.PROTECT)
    address = models.CharField(max_length=256)
    memo_or_tag = models.CharField(max_length=64, blank=True, default="")
    provider_ref = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["network","address"], name="uniq_deposit_address")]

class ChainDeposit(models.Model):
    STATUS = ["DETECTED","CONFIRMING","CONFIRMED","CREDITED","SWEPT","ORPHANED","REORGED","UNDERPAID","OVERPAID","MANUAL_REVIEW"]
    network = models.ForeignKey(Network, on_delete=models.PROTECT)
    txid = models.CharField(max_length=128)
    vout = models.CharField(max_length=32, default="")     # "" for account-model chains
    to_address = models.CharField(max_length=256)
    amount = models.DecimalField(max_digits=38, decimal_places=18)
    confirmations = models.PositiveIntegerField(default=0)
    block_height = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=[(s,s) for s in STATUS], default="DETECTED")
    detected_at = models.DateTimeField(auto_now_add=True)
    credited_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["network","txid","vout"], name="uniq_chain_deposit")]

class WebhookEvent(models.Model):
    provider = models.CharField(max_length=32)
    event_id = models.CharField(max_length=128)
    signature_valid = models.BooleanField(default=False)
    payload_sha256 = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, default="RECEIVED")
    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider","event_id"], name="uniq_webhook_event")]
```

**Posting service contract** **[P]**

```python
def record_entry(reference_type, reference_id, postings, *, reverses_entry=None) -> LedgerEntry:
    """postings: [(account, amount)] with amount > 0; validates balance,
    writes entry+postings, updates AssetBalance cache, writes AuditLog.
    Idempotent: returns existing entry when (reference_type, reference_id) exists."""
```

---

## 11. Implementation & Testing Plan

All phases are independently testable and use **testnet only** until a human approves mainnet.

| Phase | Deliverable | Touches chain? | Real funds/keys? |
|---|---|---|---|
| **0** | Gate `TopUpAPIView` behind `FUNDING_DEMO_TOPUP_ENABLED`; preserve tests | No | No |
| **1** | Ledger core: `Asset`, `LedgerAccount/Entry/Posting`, `record_entry`, immutability, balance cache + tests | No | No |
| **2** | `Network` config + `DepositAddress` (testnet addresses only) | Read-only testnet | Faucet only |
| **3** | Ingestion: `ChainDeposit`, verifier, webhook receiver, idempotency, **one** test-network deposit end-to-end | Testnet | Faucet only |
| **4** | Confirmations/finality, reorg/reversal, reconciliation job, admin views, alerts | Testnet | No |
| **5** | Withdrawals + treasury policy (depends on custody decision) | Testnet | No |
| **6** | Multi-asset rollout, mainnet readiness review, compliance sign-off | Mainnet | **Requires explicit approval** |

**No production funds or private keys may be used in any phase before phase 6 approval. Test keys and testnet faucets only.**

### 11.1 Service boundaries

- `veltrix_app/ledger/` — models + `record_entry` + balance cache (no chain code).
- `veltrix_app/custody/` — address derivation/assignment, provider adapters (interfaces).
- `veltrix_app/ingest/` — webhook receiver, verifier, detector, state machine.
- `veltrix_app/recon/` — reconciliation + alerting jobs.
- Existing `veltrix_app/api/` — thin views calling the above; no business logic.

### 11.2 Frontend changes (phase-scoped)

- Wallet view (`routing.js` already has `wallet`/`deposits`) showing **testnet** balances, addresses, deposit history.
- Explicit **Demo vs Wallet** boundary using `DemoBanner`.
- Admin: crypto deposits + reconciliation tables (mirror `AdminViews.jsx` patterns).

---

## 12. Test Scenarios

**Ledger**
- Balanced entry accepted; unbalanced rejected.
- Posting immutability: `UPDATE`/`DELETE` raises.
- Reversal mirrors original; balances return to prior state.
- Derived balance equals cache after N operations; mismatch detection fires.
- Decimal precision preserved for 8- and 18-dp assets; overflow rejected.

**Ingestion**
- Happy path: detected → confirming → confirmed → credited exactly once.
- Duplicate webhook (same `event_id`) → single credit.
- Duplicate chain event (same txid/vout) → single credit.
- Underpayment below `min_deposit` → no credit, `UNDERPAID`.
- Overpayment → credit actual amount, flag if large.
- Insufficient confirmations → stays `CONFIRMING`; no credit.
- Reorg before credit → `ORPHANED`, no ledger impact.
- Reorg after credit → reversing entry; overdraft handled with receivable + freeze.
- Forged webhook (bad signature) → rejected, no credit, logged.
- Replay with old timestamp → rejected.
- Webhook/chain mismatch → `MANUAL_REVIEW`, no credit.

**Concurrency**
- Two simultaneous credits to same account → both applied, no lost update.
- Concurrent withdrawal attempt exceeding balance → exactly one succeeds.

**Security/authz**
- Customer cannot credit own balance via any endpoint.
- Customer cannot read another user's deposits/addresses.
- Non-admin cannot hit admin crypto endpoints.
- Audit row written for every transition/adjustment.

**Regression (must remain green)**
- All existing `tests.py`, `tests_admin_api.py`, `tests_login.py`, `tests_profile_api.py` pass unchanged.
- Demo top-up works when flag enabled; returns 403/410 when disabled.

**Frontend**
- Demo vs wallet labels render; deposit screen shows testnet data; admin tables render.

---

## 13. Rollout and Rollback

**Rollout**
1. Merge phases 0–1 (no chain) behind flags; run full regression.
2. Enable testnet ingestion in staging; run synthetic testnet deposits.
3. Add reconciliation + alerting; dry-run for a fixed period.
4. Obtain custody, compliance and provider eligibility approvals.
5. Phase 6 mainnet: enable one asset, small caps, hot-wallet float limit, monitoring.

**Rollback**
- Feature flags `FUNDING_DEPOSITS_ENABLED` / `FUNDING_WITHDRAWALS_ENABLED` / `CUSTODY_FROZEN` stop new activity **without** touching the ledger.
- Ledger is append-only: rollout bugs are corrected with reversing entries, not edits.
- DB migrations are additive; rollback = disable flags + revert app deploy; data remains consistent.

---

## 14. Decisions Requiring Human Approval

1. **Custody model:** managed provider vs self-hosted; and which provider (subject to Nigeria eligibility + regulatory review).
2. **Provider availability/eligibility:** confirm in writing before any dependency.
3. **Deposit attribution:** per-user addresses vs shared address + memo/sub-accounts.
4. **Confirmation/finality thresholds per network** and reorg reversal semantics.
5. **`TopUpAPIView` fate:** disable in production (recommended), staff-only, or repurpose.
6. **Ledger adoption:** approve new double-entry ledger as real-funds source of truth (recommended) vs extending `Transaction`.
7. **Worker approach:** DB-backed job table vs adding Celery/RQ.
8. **Fee policy:** deposit/withdrawal/network-fee handling and who bears gas.
9. **Asset/network go-live order;** initial asset + chain.
10. **Compliance/legal sign-off** for Nigeria operating model, KYC/AML, Travel Rule.
11. **Monero:** confirm exclusion from initial scope.
12. **Key management:** KMS/HSM choice, quorum policy, break-glass.

---

## 15. Recommended Implementation Sequence and First Coding Task

**Sequence:** Phase 0 → Phase 1 → Phase 2 → Phase 3 (single testnet asset) → Phase 4 → Phase 5 → Phase 6, with regression green after each phase and no real keys/funds until phase 6 approval.

**First small coding task (Phase 0 — tiny, independently testable):**

> Add a `FUNDING_DEMO_TOPUP_ENABLED` setting (default `False` when `DEBUG=False`, `True` in test settings) and make `TopUpAPIView` return `403` when it is disabled, while leaving the demo behaviour and all existing top-up tests intact.

This is the smallest safe step: it removes the "unbacked credit" exposure in production without touching the ledger, chain code, key management, or any existing model, and it is fully covered by the existing test suite plus one new test for the disabled path.

**Immediately after (Phase 1, still no chain):** implement the double-entry primitive (`Asset`, `LedgerAccount`, `LedgerEntry`, `LedgerPosting`, `AssetBalance`, `record_entry`) with immutability and the balance-mismatch detection test, since every later phase depends on it.

---

## Appendix A — Verified vs Proposed vs Assumption (quick index)

- **Verified:** existing models/views/urls/serializers/settings/security/throttling; demo top-up behaviour; absence of crypto/webhook/ledger/custody code; absence of Celery; unwired `channels`; frontend routes/services; README disclaimers.
- **Proposed:** entire new subsystem (ledger, ingestion, custody adapter, admin, API contracts, flags, tests).
- **Assumption:** production DB is Postgres; provider availability in Nigeria; regulatory classification; recommended confirmation counts; provider eligibility. **All must be confirmed by a human.**

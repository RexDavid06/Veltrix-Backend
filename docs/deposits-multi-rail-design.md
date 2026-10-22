# VELTRIX — Multi-Rail Client Deposits: Crypto, Bank Transfer & Card

| Field | Value |
|---|---|
| Status | **Draft for human approval** (design only — no code, migrations, packages, settings, or financial transactions were changed) |
| Scope | Unified deposit architecture across three rails (crypto, bank transfer, card), double-entry ledger as source of truth, per-rail risk handling, reconciliation, compliance gating, application integration |
| Repositories | `~/Veltrix-Backend` (Django 5.2.18 + DRF 3.18.3), `~/Veltrix-Frontend` (React 18 + Vite 5) |
| Companion document | `docs/crypto-deposits-custody-design.md` — the crypto-specific deep dive (custody, HD wallets, chain finality). This document is **the parent**: it supersedes that document's architecture where they conflict and defers to it for crypto custody detail. |
| Related audit | Phase 1 frontend audit + Phase 2 backend audit (results cited throughout) |

**Legend used everywhere in this document**

- **[V]** — Verified from repository source code at the time of writing.
- **[P]** — Proposed design (not implemented; requires approval).
- **[A]** — Assumption or inference that must be confirmed by a human before it is relied upon.

> Repository facts are stated as **verified**. Everything about the new subsystem is **proposed** until a human approves it and code is written. Regulatory, provider-availability, fee-level and jurisdiction statements are **assumptions** requiring legal/compliance confirmation. No statement here is legal advice.

---

## 0. Executive Summary

VELTRIX today is a **paper-trading simulator**. The only balance is `DemoAccount.cash_balance` — an unbacked `Decimal(18,2)` USD scalar that any authenticated user can inflate via `POST /api/portfolio/topup/` with no payment verification, no idempotency and no ledger backing **[V]** (`veltrix_app/models.py:26`, `veltrix_app/api/views.py:491`). There is no wallet, ledger, deposit, payment, custody, reconciliation or webhook model anywhere in the codebase **[V]**. The frontend already routes `/wallet`, `/deposits`, `/withdrawals` and `/transactions` but renders "real-money deposits are not available yet" placeholders **[V]** (`Veltrix-Frontend/src/routing.js:35-38`, `src/components/AppViews.jsx:314`).

This document specifies how to accept **real client money across three rails** — crypto, bank transfer, and card — without converting simulated balances into real money and without breaking existing auth, simulated trading, history or admin.

### 0.1 Core design positions

1. **One deposit state machine, three rail adapters.** Crypto, bank and card are the *same* business object (`Deposit`) with different adapters. They differ in how money is *observed* and how it is *finalised* — not in how it is recorded or credited **[P]**.
2. **A proper double-entry ledger** (append-only, always balanced) becomes the source of truth for *real* assets. `DemoAccount.cash_balance` stays untouched as the *simulated* USD balance; the two never merge **[P]**.
3. **Credit is driven only by independently verified external facts** — a confirmed chain transaction, a settled bank credit, an authorised + captured card payment. Never by the client's request, never by a webhook body alone **[P]**.
4. **Idempotency and replay protection are database constraints**, not application checks **[P]**.
5. **`CREDITED` and `AVAILABLE` are different states.** Card deposits are credited to the ledger but held in a dispute reserve window before becoming withdrawable. This is the single most important difference between the three rails and it is modelled explicitly **[P]**.
6. **KYC is a hard gate.** No deposit intent may be created by an unverified user, on any rail **[P]**.
7. **Every rail reconciles.** Ledger ↔ provider/chain/bank statement, continuously, with drift raising an alert and never an automatic silent fix **[P]**.

### 0.2 Rail summary

| | Crypto | Bank transfer (local NGN) | Card |
|---|---|---|---|
| Attribution key | deposit address | reserved account number | PSP payment-session id |
| Who tells us money arrived | chain indexer / custodian webhook | PSP + NIBSS/bank settlement | PSP webhook |
| What "final" means | N confirmations & canonical chain | funds settled & unreversed | authorised → captured → settled → **dispute window expired** |
| Time to spendable | seconds–hours | minutes–hours | instant (crypto/bank) / **delayed** (card) |
| Cost per unit | network fee | ~1–1.5% + VAT **[A]** | 1.5–3% + fixed + FX **[A]** |
| **Reversal risk** | low (reorg only) | **medium** (card-funded NGN chargeback) | **high** (chargeback, up to ~120 days) **[A]** |
| Build priority | **1st** (design complete) | 2nd (likely highest volume in Nigeria) | 3rd (hardest risk profile) |

### 0.3 The single most important blocker

There is no independently verified, idempotent deposit layer, and no ledger backing the existing balance. Both must be introduced before any real funds can be accepted. The ledger is **shared by all three rails** and must be built first — retrofitting it after real money flows is a migration of live financial records, which is materially more dangerous than building it before **[P]**.

---

## 1. Current-State Facts This Design Must Respect

All rows below are **[V]** unless marked.

### 1.1 Backend models (`veltrix_app/models.py`)

| Model | Line | Role today | Constraint for this design |
|---|---|---|---|
| `User(AbstractUser)` | 11 | `role` (client/admin), `is_verified` (email verification), `is_admin()` | Reused as the deposit owner. `is_verified` currently means *email verified*, **not** KYC-verified — see §3.1 |
| `DemoAccount` | 26 | `cash_balance` — simulated USD, `Decimal(18,2)` | **Never** credited by a real deposit. See §2.4 |
| `Instrument` | 36 | Tradable symbols with `price` | Market data only; not a settlement asset |
| `Position` | 53 | Per-user holdings with `avg_price` | Simulated trading only **[V]** |
| `Transaction` | 85 | Flat activity log; `side` ∈ buy/sell/**topup**, `status` ∈ simulated/pending/completed | Stays the *simulated* log. Real money is never written as `side="topup"` **[P]** |
| `SupportTicket` | 110 | Client support tickets | Reused for deposit disputes/refunds **[P]** |
| `AuditLog` | 125 | `user`, `actor`, `action`, `details` | **Every** deposit state transition writes here, in-transaction **[P]** |
| `Conversation` / `Message` | 137 / 187 | Support chat | Unchanged **[V]** |

### 1.2 Backend endpoints that exist today

| Method | Path | View | Money relevance |
|---|---|---|---|
| POST | `/api/portfolio/topup/` | `TopUpAPIView` (`api/views.py:491`) | **The only funding endpoint.** Mints simulated USD. Gated by `FUNDING_DEMO_TOPUP_ENABLED` (`settings.py:185`), forced off when `DEBUG=False` **or** `DATABASE_URL` is set (`settings.py:195-197`) |
| GET | `/api/dashboard/` | `DashboardAPIView` (`api/views.py:116`) | Returns `account.cash_balance` (simulated) + last 10 `Transaction` rows. No dedicated transaction-history endpoint exists **[V]** |
| POST | `/api/orders/` | `OrderAPIView` (`api/views.py:384`) | Simulated buy/sell; debits/credits `cash_balance` under `select_for_update()` (`:427`, `:435`, `:455`) |
| GET | `/api/instruments/`, `/api/instruments/<symbol>/` | `api/views.py:42` | Price feed via `veltrix_app/services/market.py` (yfinance) |
| GET | `/api/admin/stats/`, `/api/admin/users/`, `/api/admin/transactions/`, `/api/admin/audit/` | `api/admin_views.py` | Read-only admin; `IsPlatformAdmin` (`api/permissions.py:17`) |

> There is **no** `/api/transactions/` endpoint, **no** wallet endpoint, **no** deposit/withdrawal/payment model, **no** payment-provider SDK, **no** crypto library, **no** Celery, and **no** worker of any kind **[V]** (`requirements.txt` has no payment or blockchain dependency).

### 1.3 Frontend state

| Item | Location | State |
|---|---|---|
| Routes `wallet`, `deposits`, `withdrawals`, `transactions` | `src/routing.js:24-51` | **Exist**, render placeholders **[V]** |
| `Money()` component | `src/components/AppViews.jsx:314` | Says *"real-money deposits are not available yet"* **[V]** |
| `Kyc()` component | `src/components/AppViews.jsx:393` | Static form, no backend submission **[V]** |
| `TopUpModal.jsx` | whole file | Demo funding modal; explicitly promises *"no payment details are collected"* (`:152-156`) **[V]** |
| `demoTopUpEnabled()` | `src/flags.js:16` | Requires `VITE_DEMO_TOPUP_ENABLED=1` **and** `import.meta.env.DEV`; compiled out of prod builds **[V]** |
| API client | `src/api.js` | axios instance, Bearer interceptor (`:185`), 401→refresh→retry (`:198`) |
| Service pattern | `src/services/profileFeed.js` | Thin fetch helpers, one file per domain |

### 1.4 Platform facts

- **Database**: `DATABASE_URL` → `dj_database_url`; else SQLite at `instance/veltrix.db` (`settings.py:109-116`) **[V]**. Production/docker is PostgreSQL 15 (`docker-compose.yml:7`) **[V]**. `select_for_update()` is a **no-op on SQLite** **[V]** — real-funds development requires Postgres.
- **Auth**: SimpleJWT only (`settings.py:226`), `AUTH_USER_MODEL = "veltrix_app.User"` (`:146`), `IsAuthenticated` default (`:229`). Throttles: anon 20/min, user 1000/day (`:232-239`) **[V]**.
- **Settings style**: `os.environ.get(...)` with explicit defaults; the demo-topup flag has a hard production interlock (`settings.py:195-197`) **[V]**. This document proposes the same interlock style for every new flag (§12).
- **Migrations**: `0001_initial`, `0002_zero_existing_accounts`; both applied **[V]**. `0002` was destructive (zeroed balances, deleted positions/transactions) **[V]** — a cautionary precedent for any migration touching money.
- **CI**: GitHub Actions asserts `DATABASE_URL` is `postgres://`/`postgresql://` **[V]**.

---

## 2. Architecture

### 2.1 One state machine, three adapters

```
                      ┌──────────────────────────────┐
   Client ──────────► │  DepositIntent (pre-payment) │  rail-specific instructions issued
                      └───────────────┬──────────────┘
                                      │  money moves outside our systems
                                      ▼
   Chain indexer ──┐            ┌─────────────┐
   PSP webhook  ───┼───────────►│  Deposit    │  observed, verified, finalised, credited
   Bank settlement ┘            └──────┬──────┘
                                      │  record_entry()  (the ONLY write path)
                                      ▼
                            ┌───────────────────┐
                            │  Double-Entry     │
                            │  Ledger           │  append-only, always balanced
                            └─────────┬─────────┘
                                      ▼
                            ┌───────────────────┐
                            │  AssetBalance     │  available / reserved / pending (cache)
                            └───────────────────┘
```

The rail adapters are **observation** adapters. They answer exactly two questions per rail:

| Adapter question | Crypto | Bank | Card |
|---|---|---|---|
| "Did money arrive, and how much?" | confirmed chain tx to our address | settled credit to our reserved account | captured payment on our PSP session |
| "Is it final?" | N confirmations, canonical chain | settled and not reversed | captured **and** dispute window elapsed |

Everything downstream — ledger, balances, history, admin, reconciliation — is rail-agnostic.

### 2.2 Unified deposit lifecycle

```
PENDING ──► DETECTED ──► VERIFYING ──► VERIFIED ──► CREDITED ──► AVAILABLE
   │            │             │            │            │             │
   │            │             │            │            │             └─► REVERSED
   │            │             │            │            └─► RESERVED (card dispute window)
   │            │             │            └─► MANUAL_REVIEW
   │            │             └─► REJECTED
   │            └─► UNDERPAID / OVERPAID
   └─► EXPIRED (intent never paid)
```

| From | To | Guard (all **[P]**) |
|---|---|---|
| `PENDING` | `DETECTED` | Provider/event confirms a movement matching the intent (address / account / session) |
| `DETECTED` | `VERIFYING` | Movement identified; amount read from the **source of truth** (chain/PSP/bank), not the webhook body |
| `VERIFYING` | `VERIFIED` | Amount ≥ min, asset/currency correct, destination is ours, no duplicate, finality threshold met |
| `VERIFIED` | `CREDITED` | Atomic ledger posting + balance cache update succeed; idempotent on `(rail, provider_ref)` |
| `CREDITED` | `AVAILABLE` | Crypto & bank: immediately. Card: `available_at` reached (dispute reserve window expired) |
| `CREDITED` | `RESERVED` | Card only — funds are on the ledger; withdrawal of this principal is gated until `available_at` (§2.3) |
| `RESERVED` | `AVAILABLE` | Reserve window elapsed with no dispute |
| `RESERVED` | `REVERSED` | Chargeback/dispute received — reversing ledger entry **[P]** |
| `CREDITED`/`RESERVED` | `REVERSED` | Bank transfer reversed (card-funded NGN) or crypto reorg post-credit **[P]** |
| any | `MANUAL_REVIEW` | Amount/asset mismatch, duplicate-with-different-amount, unverifiable source, name mismatch |
| any | `REJECTED` | Under min, wrong asset, failed verification, provider explicitly declined |
| `PENDING` | `EXPIRED` | Intent TTL elapsed without payment |

State transitions are performed by **one service**, guarded by `select_for_update()`, and are **monotonic** except for explicitly documented reversals. Illegal transitions raise and are logged **[P]**.

### 2.3 `CREDITED` vs `AVAILABLE` — the reserve window

This is the one place the three rails genuinely diverge, and it must be modelled rather than hand-waved.

**Crypto and bank:** finality is reached quickly and reversal risk is low or bounded. The deposit goes `CREDITED → AVAILABLE` in the same transaction.

**Card:** the money is captured and in our PSP balance, but the cardholder can dispute for up to ~120 days **[A]**. If we let the client withdraw the crypto immediately and they then dispute, we are out the fiat — a real, recurring loss.

**Decision (approved by project owner): reserve window before available.** Card deposits are:
1. Credited to the ledger immediately (the client sees the balance) — the money genuinely arrived.
2. Marked **reserved**: the principal cannot be **withdrawn** until the window elapses (under the default policy (b), trading is still permitted — see below).
3. Promoted to `AVAILABLE` when `available_at` passes with no dispute.

**Critical distinction:** *reserved* funds remain the client's money on the ledger (they are a liability to us) — they are **not** swept into platform revenue. What is restricted is the client's *ability to remove value from the platform* while the dispute window is open, i.e. our exposure to the dispute **[P]**.

**Trading with reserved funds — recommended:** allow trading, but on dispute the reversal may drive the customer balance negative, which is handled by the **same** `CUSTOMER_RECEIVABLE` + freeze path the crypto design already defines for post-credit reorgs (§3.5 E5 of `crypto-deposits-custody-design.md`). This avoids a second, parallel overdraft mechanism **[P]**.

**Reserve window duration — requires human confirmation (see §16):** the card scheme dispute window is ~120 days from the initial transaction date **[A]**. A 120-day freeze on every card deposit is commercially close to unusable. Options:
- **(a) Full window (120d), both trading and withdrawal blocked** — safest, lowest conversion, best evidence for chargeback defence.
- **(b) Full window for the *withdrawal* only (recommended)** — client can trade immediately; *withdrawal* of card-funded principal is blocked until `available_at`. Withdrawals are where fiat leaves the platform; trading only re-prices the balance, and a loss can still be recovered as `CUSTOMER_RECEIVABLE`.
- **(c) Tiered by client** — low-risk, low-volume clients get a short window (e.g. 7 days); high-risk get the full window. More revenue, more fraud exposure, needs a risk engine.
- **(d) External guarantee** — only via a chargeback-insurance/guarantee product, which is a commercial relationship, not a technical control.

This document implements **(b)** as the default because it preserves the most value while still blocking the irreversible exit, and flags the window length as a business decision, not an engineering one **[P]**. Under (b) the ledger is unchanged from (a) — only the **withdrawal eligibility check** differs, which lives in the withdrawal service (§6.4), not in the ledger or the deposit state machine. Switching to (a) is a one-line change to that check **[P]**.

### 2.4 Isolation from the simulated economy

The single most important boundary in this system:

- `DemoAccount.cash_balance` remains the **simulated** USD balance used by `DashboardAPIView`, `TopUpAPIView` and the simulated `OrderAPIView`. It is **never** debited or credited by deposit ingestion, the ledger, or any provider event **[P]**.
- Real assets are represented **only** as `CUSTOMER:{user}:{asset}` ledger accounts. `DemoAccount.cash_balance` and `AssetBalance` are different objects with different owners, different currencies and different semantics **[P]**.
- Real deposits/withdrawals are recorded in `Deposit` + the ledger, and surfaced to the existing admin `Transaction` API only through a **separate, clearly-labelled read model** — never by writing `side="topup"` rows **[P]**.
- The UI shows a hard visual boundary: an existing **"Demo / Simulated"** area and a new **"Wallet / Real"** area. `DemoBanner.jsx` already exists and is extended for this **[V for file / P for change]**.
- **No migration or backfill** of existing demo balances is recommended **[P]**. `0002_zero_existing_accounts` is historical and destructive **[V]** — a precedent we do not repeat.

### 2.5 `is_verified` is not KYC

`User.is_verified` is set by the **email-verification** flow (`veltrix_app/api/verify.py:40`) and is documented in `UserProfileSerializer` as read-only privilege **[V]**. The frontend `Kyc()` component is a static form with no backend submission **[V]**.

Therefore KYC cannot be inferred from `is_verified`. This design introduces a **separate `KycProfile`** model (§3.1) and gates deposits on **its** status. Reusing `is_verified` would silently mean "email confirmed" in one place and "KYC approved" in another — a compliance-critical ambiguity **[P]**.

---

## 3. Domain Model

### 3.1 `KycProfile` — the gate

```
KycProfile(user 1—1, status, tier, verified_at, verified_by, provider_ref, risk_score, created_at, updated_at)
    status: UNVERIFIED → PENDING → IN_REVIEW → APPROVED | REJECTED | EXPIRED
```

- A user with `status != APPROVED` **cannot create a deposit intent on any rail** **[P]**.
- `tier` drives limits (§7.3) — e.g. tier 1 capped, tier 2 higher.
- `KycProfile` is separate from `User.is_verified` (§2.5) **[P]**.
- **Who approves?** Manual admin review (staff), or an external KYC provider (needs Nigeria-eligible vendor, e.g. a BVN/NIN-based provider) **[A]**. Undecided — §16.

### 3.2 `Asset` — the unit of everything

One `Asset` table serves **both** crypto and fiat. This is the key unification: `NGN`, `USD`, `EUR`, `BTC`, `USDT`, `USDC` are all assets, all balanceable, all ledger-scopeable.

```
Asset(code, display_name, decimals, asset_class, is_active, is_fiat)
    asset_class: FIAT | CRYPTO
    decimals:    2 for NGN/USD/EUR, 8 for BTC, 6 for USDT (ERC20), 0 for JPY-style
    is_fiat:     True → no Network rows; False → requires >= 1 active Network
```

- `decimals` is the **base-unit precision**, and every monetary column is `Decimal(38, 18)` in base units to accommodate 18-dp tokens without schema churn **[P]**.
- Fiat assets have **no** `Network` rows — a bank account is not a network **[P]**.
- A stablecoin (USDT) is **not** the same asset as USD and **does not** reuse `cash_balance` **[P]**.
- Card deposits settle into a **fiat** asset (NGN or USD); crypto deposits settle into a **crypto** asset. A card-funded purchase of BTC is a *conversion*, modelled as an explicit ledger pair, not a deposit **[P]** — see §3.6.

### 3.3 `Deposit` — the unified record

```
Deposit(
    id,
    user,                                  FK User, PROTECT
    rail,                                  CRYPTO | BANK | CARD
    asset,                                 FK Asset, PROTECT
    status,                                §2.2 state machine
    expected_amount,                       Decimal(38,18), base units
    received_amount,                       Decimal(38,18), nullable until DETECTED
    provider,                              FK DepositProvider
    provider_ref,                          provider-side id; unique with provider
    idempotency_key,                       client-supplied or server-generated
    source_currency,                       provider-side currency (e.g. NGN)
    source_amount,                         provider-side amount before FX
    fx_rate,                               Decimal(38,18) applied, nullable
    fee_amount,                            platform fee withheld (rail-specific)
    credited_at, available_at,             timestamps
    ledger_entry,                          FK LedgerEntry, nullable, SET_NULL
    reconciliation_note,                   Text
    created_at, updated_at
)
```

Constraints:
- `unique(provider, provider_ref)` — **the** replay guard. A provider event ID that arrives twice cannot create two deposits **[P]**.
- `unique(user, idempotency_key)` — client double-submit guard **[P]**.
- `received_amount ≥ 0`; `expected_amount > 0`; `received_amount` is the **authoritative** amount — `expected_amount` is a hint and is never used to credit **[P]**.

### 3.4 Rail detail models

```
CryptoDeposit(deposit 1—1, network, address, memo, txid, vout,
              confirmations, required_confirmations, block_height,
              provider_tx_status)

BankDeposit(deposit 1—1, provider_account, provider_reference,
            sender_name, sender_bank, received_at, settled_at,
            reversed_at, reversal_reason, is_card_funded)

CardDeposit(deposit 1—1, psp_session_id, card_brand, card_last4,
            auth_code, captured_at, settled_at, dispute_status,
            dispute_opened_at, dispute_deadline, dispute_amount,
            reversal_reason)
```

- `BankDeposit.provider_account` — the reserved/virtual account number issued for this deposit. For local rails this **is** the attribution key **[P]**.
- `BankDeposit.is_card_funded` — set when the PSP reports the transfer source was card-based; these carry elevated chargeback risk and are flagged for monitoring **[A/P]**.
- `CardDeposit.card_last4` — only the last four digits are ever stored. **Never a PAN, never a CVV, never a full expiry + CVV combination** **[P]**.
- `CardDeposit.dispute_deadline` — `captured_at + window`; the account manager uses it to release reserves.

### 3.5 Ledger (adopted from the crypto design)

The chart of accounts, balancing invariant and immutability rules in `docs/crypto-deposits-custody-design.md` §3.1–§3.5 are adopted unchanged and extended with **one** account:

| Code | Account | Type | Purpose |
|---|---|---|---|
| `1000` | `TREASURY:{asset}` | Asset | Assets VELTRIX controls (custody, bank, PSP balances) |
| `1010` | `DEPOSITS_IN_TRANSIT:{asset}` | Asset | Detected but not yet finalised |
| `1020` | `WITHDRAWALS_IN_TRANSIT:{asset}` | Asset | Broadcast but not final |
| `1090` | `CUSTOMER_RECEIVABLE:{asset}` | Asset | Owed *by* customers (reversal shortfall) |
| `2000` | `CUSTOMER:{user}:{asset}` | Liability | What VELTRIX owes a customer — **total, including reserved** |
| `2010` | `WITHDRAWALS_PAYABLE:{asset}` | Liability | Approved but unbroadcast withdrawals |
| `2090` | `ADJUSTMENT_CLEARING:{asset}` | Clearing | Suspense for manual corrections |
| `3000` | `RECONCILIATION_CLEARING:{asset}` | Clearing | Absorbs confirmed discrepancies |
| `4000` | `DEPOSIT_FEE_REVENUE:{asset}` | Revenue | Platform deposit fees |
| `4010` | `WITHDRAWAL_FEE_REVENUE:{asset}` | Revenue | Platform withdrawal fees |
| `5000` | `NETWORK_FEE_EXPENSE:{asset}` | Expense | Gas/settlement fees paid by VELTRIX |
| `5090` | `ADJUSTMENT_LOSS:{asset}` | Expense | Write-offs |

**Extension — the reserve bucket is a sub-ledger, not a new account.** The customer's total liability stays in `2000 CUSTOMER:{user}:{asset}`. The reserved/withdrawable split is tracked on `Deposit` (`credited_at`, `available_at`) and materialised in the `AssetBalance` cache. This keeps the chart of accounts stable and the liability single-sourced **[P]**.

**Extension — fee accounts per rail.** Deposit fees on card/bank are revenue in the *fiat* asset; crypto network fees are expense in the crypto asset **[P]**.

**Extension — a cross-asset settlement account.** The crypto design's balancing invariant (§3.2) sums postings within one entry, and every `LedgerAccount` is scoped to a single `asset` **[V]**. An entry therefore balances **within one asset** — a single entry that debits `TREASURY:USD` and credits `CUSTOMER:u42:NGN` would be summing dollars against naira, which is meaningless **[P]**.

Any deposit that crosses currencies must therefore be split into **one entry per asset**, with the currency change passing through a paired position account:

| Code | Account | Type | Purpose |
|---|---|---|---|
| `1100` | `FX_POSITION:{asset}` | Mixed | Counter-leg for cross-asset settlement. Debit balance = long the asset; **credit balance = short the asset** (an allowed, meaningful state, exactly like a bank overdraft) |

The paired `FX_POSITION:USD` / `FX_POSITION:NGN` accounts are the open FX exposure VELTRIX carries until the position is closed. Their net value is zero at the transaction rate but **not** risk-free — the position moves with the market, which is exactly the exposure the accounts exist to make visible **[P]**.

> **Note on the crypto design's invariant.** The crypto design requires postings to carry a *positive* amount with the side derived from the account's `normal_side` (§3.2) **[V]**. `FX_POSITION` is the one account where the balance legitimately carries either sign, so it needs an explicit convention (debit balance = long, credit balance = short) and must be excluded from any "every account is single-sided" assertion **[P]**.

This is a genuine **extension** to the crypto design's chart of accounts and must be reflected there before implementation **[P]**.

Posting service — contract extended from the crypto design §10:

```python
def record_entry(reference_type, reference_id, postings, *, reverses_entry=None) -> LedgerEntry
def record_multi_asset_entry(reference_type, reference_id, entries_by_asset, *, reverses_entry=None) -> list[LedgerEntry]
```

`reference_type` ∈ `deposit|withdrawal|reversal|adjustment|conversion`. `reference_id` = `Deposit.id` — so the ledger's own unique constraint is the second layer of credit idempotency, behind the DB's `(provider, provider_ref)` **[P]**.

**Multi-asset deposits post more than one entry.** A cross-currency deposit posts one entry per asset, all sharing the same `reference_type`/`reference_id` pair. That means the crypto design's `unique(reference_type, reference_id)` constraint on `LedgerEntry` (`crypto-deposits-custody-design.md:597`) **cannot be used as-is** for cross-asset deposits — it must become `unique(reference_type, reference_id, asset)`, or the constraint must move to the posting level **[P]**.

**This is a required amendment to the crypto design's `LedgerEntry` model**, to be applied in the same Phase 0 migration that introduces the ledger **[P]**. Flagged in §16.

### 3.6 Conversions (card fiat → crypto purchase)

A client with NGN in their wallet who buys BTC is a **conversion**, not a deposit. It is likewise one entry per asset:

```
Dr 2000 CUSTOMER:u42:NGN     500,000.00
   Cr 1100 FX_POSITION:NGN    500,000.00
   (asset = NGN)

Dr 1100 FX_POSITION:BTC           0.00310
   Cr 2000 CUSTOMER:u42:BTC        0.00310
   (asset = BTC)
```

The NGN leg releases the customer's NGN liability; the BTC leg creates it. `4000 CONVERSION_REVENUE` is credited separately if a spread is charged (or the spread is realised as a difference between the two `FX_POSITION` legs) **[P]**.

The FX rate must be captured with a timestamp and a named source, and the spread is a deliberate revenue line rather than a rounding artefact (§7.2) **[P]**.

Conversions are **out of scope for this document's deposit work** but must be designed before card-funded trading is enabled — without them, card deposits in NGN cannot be spent on crypto assets and the ledger cannot represent the client actually holding BTC **[P]**. Flagged in §16.

### 3.7 `AssetBalance` — the cache

Extended from the crypto design's `(available, pending)` to:

```
AssetBalance(user, asset, available, reserved, pending, updated_at)
    unique(user, asset)
```

- `available` — withdrawable **now**.
- `reserved` — on the ledger, not withdrawable yet (card dispute window). Trading is permitted under policy (b) (§2.3), so `reserved` is a *withdrawal* gate, not a trading gate.
- `pending` — observed but not yet finalised (not on the ledger; a memo of in-flight deposits).
- `available + reserved` must equal the ledger-derived `CUSTOMER:{user}:{asset}` balance. The reconciliation job asserts this every cycle and **freezes the account on mismatch** **[P]**.
- Every mutation takes `select_for_update()` on the row **[P]**.

### 3.8 `DepositProvider` — pluggable rails

```
DepositProvider(code, display_name, rail, is_active, is_test,
                config_json, supported_assets M2M, fee_schedule M2M,
                webhook_secret_ref, created_at, updated_at)
```

`config_json` holds non-secret configuration (base URLs, account identifiers). **Secrets live in the environment / secret manager and are never in the DB** **[P]**.

`code` examples: `crypto_managed`, `crypto_selfhosted`, `bank_local_va`, `bank_wire`, `card_psp`. The `code` is the swap point for the **undecided custody model** (§16): the adapter interface (§4) is stable regardless of which provider is chosen **[P]**.

### 3.9 `ProviderEvent` — the raw, replay-safe inbox

```
ProviderEvent(provider, event_id, event_type, signature_valid,
              payload_sha256, raw_payload, received_at, processed_at,
              status, error)
    unique(provider, event_id)   ← replay protection
```

- Every inbound webhook lands here **first**, before any business logic. `raw_payload` is stored (redacted of PAN/CVV if a card provider ever sends them — it should not) **[P]**.
- `unique(provider, event_id)` makes a replayed webhook a no-op: the second insert raises and the handler returns `200` without reprocessing **[P]**.
- Processing is asynchronous — the HTTP handler acks `2xx` immediately and enqueues a `Job` (§8.4) **[P]**.

### 3.10 `ReconciliationRun` — evidence

```
ReconciliationRun(provider, rail, period_start, period_end, ran_at,
                  provider_total, ledger_total, difference, status,
                  discrepancy_count, notes)
```

Every run is persisted so disputes have a paper trail **[P]**.

---

## 4. Rail Adapters

All adapters implement the same interface. Only observation and finality differ.

```python
class DepositAdapter:
    code: str
    rail: str

    def create_intent(self, user, asset, amount, *, idempotency_key) -> Intent: ...
    def poll(self, since) -> list[ProviderEvent]: ...
    def verify(self, event: ProviderEvent) -> VerifiedDeposit: ...
    def finality(self, verified: VerifiedDeposit) -> FinalityDecision: ...
    def reverse(self, deposit: Deposit, *, reason) -> ReversalRequest: ...
```

`VerifiedDeposit` carries: `provider_ref`, `received_amount`, `source_currency`, `source_amount`, `fx_rate`, `destination_key` (address / account number / session id), `is_final`, `evidence` (JSON), `anomalies` (list).

### 4.1 Crypto adapter

Detail is in `docs/crypto-deposits-custody-design.md` (§4 deposit ingestion, §5 custody). The multi-rail-relevant summary:

- **Attribution**: per-user unique address (recommended) or shared address + memo. `DepositAddress` is `unique(network, address)` so one address can never map to two users **[P]**.
- **Source of truth is the chain**, not the webhook. The webhook is a trigger to go look **[V for principle in crypto design §4.3 / P here]**.
- **Independent verification** (§4.6 of crypto design): txid canonical, destination is ours, amount/asset match, confirmations ≥ threshold, no duplicate **[P]**.
- **Finality**: `Network.required_confirmations` — configuration, never a constant in code. Defaults proposed: Bitcoin 3–6, Ethereum 12–64, TRON 19–20 **[A]**.
- **Custody model is undecided** — the adapter interface is provider-agnostic so `crypto_managed` and `crypto_selfhosted` are interchangeable **[P]**. This is the deliberate answer to "not decided yet": we build the seam, not the choice.
- **Post-credit reorg** → reversing entry; if the balance goes negative → `CUSTOMER_RECEIVABLE` + freeze + alert **[P]**.
- **Wrong-network deposits** (USDT-ERC20 sent where TRC20 was expected) are **not creditable**; they go to `MANUAL_REVIEW` and typically need a provider-side recovery process. This is a known operational cost and is why the UI must show the network unambiguously **[P]**.

### 4.2 Bank transfer adapter

Two very different problems share one adapter shape.

#### 4.2.1 Local NGN (Nigeria) — the volume rail

**Design: dedicated reserved account per deposit.**

1. Client requests a deposit intent for NGN.
2. We ask the PSP to reserve a **unique virtual account number** for that deposit (this is the attribution key).
3. We show the client: account number, bank, amount, expiry, and a human reference.
4. Client transfers from their Nigerian bank app.
5. PSP notifies us (webhook) and/or we poll their transactions API.
6. We **independently verify** by querying the PSP with `provider_ref` — never trust the webhook body alone.
7. We compare the settled amount against `expected_amount` and check it settled to **our** reserved account.
8. Credit, with a `source_amount` / `source_currency` of NGN.

Why dedicated accounts beat a shared account + narration reference: narration is frequently dropped by Nigerian banking apps and users forget it. With a dedicated account number, **the account number *is* the identity** — no fuzzy matching, no name-match heuristics, no manual reconciliation for the common case **[P]**.

Why not shared-account + amount-matching: two clients sending the same amount collide; amounts are not unique; reconciliation becomes guesswork **[P]**.

**The bank-rail-specific risks:**

| Risk | Detail | Mitigation **[P]** |
|---|---|---|
| **Card-funded NGN transfer** | The client funded their bank transfer with a card. Days later the card is charged back and the PSP reverses the NGN credit — but the client already withdrew the crypto. This is the **single largest loss vector in the local rail** **[A]** | Track `is_card_funded`; hold card-funded deposits in `reserved` until a chargeback window expires; cap card-funded deposit value per client; alert on every reversal; consider declining card-funded transfers above a threshold |
| Third-party payer | Someone else pays on the client's behalf → sender name ≠ account holder | Compare `sender_name` to the user's name; mismatch → `MANUAL_REVIEW`, not auto-credit |
| Amount mismatch | Under/overpayment | Credit the **actual** settled amount if ≥ min and ≤ max; else `MANUAL_REVIEW` |
| Reversal after credit | Bank-side reversal | Reversing entry; `CUSTOMER_RECEIVABLE` + freeze if already spent |
| Wrong account | Transfer to a stale/expired reserved account | Reserved accounts expire; a credit to an expired account → `MANUAL_REVIEW` |
| Duplicate reference | PSP reuses a reference | `unique(provider, provider_ref)` + PSP-side idempotency |
| Limits | CBN per-transaction and daily limits | Enforce at intent creation (§7.3) |

#### 4.2.2 International wire (SWIFT/IBAN) — the deferred rail

Included for completeness; **out of scope for the first implementation** **[P]**.

- **No webhooks.** Money arrives in a nostro/vostro account and you discover it by polling a bank API or ingesting a statement file (MT940/camt.053) on a schedule.
- **Attribution is inference**: amount + reference + sender name, within a time window, with fuzzy matching. Ambiguity is the norm, not the exception.
- **T+1..T+5** settlement; correspondent fees; FX conversion on receipt (CBN rules apply to NGN receipts) **[A]**.
- **Operational cost is high**: a human reconciles unmatched wires daily.
- Recommendation: build a **read-only inbound-wires view** plus an **admin-assisted manual credit** (with two-person approval) before attempting automated matching **[P]**.

### 4.3 Card adapter

**Never touch the PAN.** The card number and CVV are entered in the PSP's hosted fields or iframe, or the client is redirected to the PSP's checkout. VELTRIX receives only a token, brand, last4 and outcome. This keeps the PCI scope minimal and eliminates card data from our database, logs and infrastructure entirely **[P]**.

**Flow:**

1. Client selects card deposit, amount, currency.
2. We create a PSP payment session (`psp_session_id`) server-side. We never ask the client for card data.
3. The client completes the payment in the PSP's UI — including any 3-D Secure / SCA step-up.
4. The PSP notifies us (webhook) and/or we poll the session.
5. We **independently verify**: query the PSP for the session and assert `status == succeeded`, `amount == expected`, `currency == expected`.
6. We capture the settlement details, credit the ledger, and set `available_at = captured_at + CARD_RESERVE_WINDOW_DAYS`.
7. If a dispute arrives before `available_at`: `RESERVED → REVERSED`, reversing ledger entry, `CUSTOMER_RECEIVABLE` if already spent.

**Card-specific risks and mitigations [P]:**

| Risk | Detail | Mitigation |
|---|---|---|
| **Chargeback / dispute** | Up to ~120 days **[A]**. We may have already withdrawn the crypto | Reserve window (§2.3) blocks withdrawal of card-funded principal; reversal path; per-client caps |
| Friendly fraud | Client claims they never authorised it | Liability shift only when 3DS passed **and** AVS/CVV matched **and** no prior disputes |
| Card testing | Fraudsters probing many small cards | Velocity limits per IP/device/user; decline rapid sequences; the auth rate-limit at the PSP |
| Stolen card | Genuine card, real money | 3DS step-up; liability shift conditions; monitor dispute rate |
| FX exposure | Charged in USD, ledger wants NGN | Explicit `source_currency`/`fx_rate` on the deposit; FX is a *cost*, not a rounding artefact |
| Interchange + FX markup | ~1.5–3% + fixed + FX markup **[A]** | Fee schedule; possibly charge a card deposit fee explicitly |
| Partial capture / refund | PSP refunds | Reversal entry; if refunded after credit → `REVERSED` |
| Auth-but-no-capture | Funds authorised, never captured | Only credit on **captured**, not merely **authorised** |

**Recommended control: credit on `captured`, not `authorised`.** An authorisation is a temporary hold that can expire or fail; a capture is money that moved **[P]**.

---

## 5. Ledger Integration

### 5.1 Posting patterns per rail

**Crypto deposit** (from the crypto design §3.5 E1):
```
Dr 1000 TREASURY:BTC          1.00000000
   Cr 2000 CUSTOMER:u42:BTC      0.99500000
   Cr 4000 DEPOSIT_FEE_REVENUE:BTC  0.00500000
```

**Bank deposit** (NGN in, NGN credited — no FX for a same-currency local deposit):
```
Dr 1000 TREASURY:NGN          500000.00
   Cr 2000 CUSTOMER:u42:NGN    498750.00
   Cr 4000 DEPOSIT_FEE_REVENUE:NGN  1250.00
```

**Card deposit** (USD charged, NGN credited, FX applied). Three entries, one per asset (§3.5). Client sent 100.00 USD; rate 1,535 NGN/USD → 153,500.00 NGN gross; 2,500.00 NGN platform fee; 151,000.00 NGN credited to the client:

```
Entry 1 — asset = USD   (we hold the USD; the FX position is short USD)
Dr 1000 TREASURY:USD              100.00
   Cr 1100 FX_POSITION:USD          100.00

Entry 2 — asset = NGN   (the NGN claim is delivered to the customer)
Dr 1100 FX_POSITION:NGN        151,000.00
   Cr 2000 CUSTOMER:u42:NGN     151,000.00

Entry 3 — asset = NGN   (the spread is recognised as platform revenue)
Dr 1100 FX_POSITION:NGN          2,500.00
   Cr 4000 DEPOSIT_FEE_REVENUE:NGN  2,500.00
```

Each entry balances within its own asset **[P]**.

**Why three entries and not one:** the customer's NGN claim (151,000) and the platform's fee (2,500) are two different dispositions of the same 153,500 NGN FX claim, so they are two separate postings against the same position account. Collapsing them hides where the spread came from.

**What the position means:** after these three entries, `FX_POSITION:NGN` is debit 153,500 (long NGN) and `FX_POSITION:USD` is credit 100 (short USD). At the transaction rate of 1,535 the position is **value-neutral** — the customer received 151,000 NGN of claim, we received 2,500 NGN of margin, and the 100 USD sits in treasury. But it is *not* risk-free: if NGN weakens, the 153,500 NGN claim is worth less than the 100 USD we received, and the loss lands on the open position **[P]**. That is precisely the FX risk a card-funded NGN deposit creates, and it is why `FX_POSITION` must be reconciled and reported like any other balance.

**Card dispute reversal** (client disputes the 100.00 USD charge after we credited NGN) — three reversing entries, one per asset:

```
Entry 4 — asset = USD   (reverses Entry 1)
Dr 1100 FX_POSITION:USD              100.00
   Cr 1000 TREASURY:USD              100.00

Entry 5 — asset = NGN   (reverses Entry 2)
Dr 2000 CUSTOMER:u42:NGN        151,000.00
   Cr 1100 FX_POSITION:NGN      151,000.00

Entry 6 — asset = NGN   (reverses Entry 3)
Dr 4000 DEPOSIT_FEE_REVENUE:NGN   2,500.00
   Cr 1100 FX_POSITION:NGN        2,500.00
```

Both `FX_POSITION` legs return to zero, the fee revenue is reversed, and treasury gives back the 100 USD the PSP claws **[P]**.

If the client already traded or withdrew the credited NGN, `CUSTOMER:u42:NGN` goes negative on Entry 5. Per crypto design §3.5 E5, post `Dr 1090 CUSTOMER_RECEIVABLE:NGN / Cr 2000 CUSTOMER:u42:NGN` for the shortfall, set an overdraft flag, block withdrawals and alert **[P]**.

**Bank transfer reversal** (card-funded NGN reversed by the PSP):

```
Dr 2000 CUSTOMER:u42:NGN      498,750.00
   Cr 1000 TREASURY:NGN        500,000.00
```

One asset, one entry, balances. If the customer already spent it → `CUSTOMER:u42:NGN` goes negative → same `CUSTOMER_RECEIVABLE` + freeze path **[P]**.

### 5.2 Immutability and corrections

- No `UPDATE`/`DELETE` on `LedgerEntry` / `LedgerPosting`. Enforced by model overrides **and** a Postgres trigger **[P]**.
- Corrections are **reversing entries** referencing the original (`reverses_entry`) **[P]**.
- Every entry and posting writes an `AuditLog` row in the same transaction **[P]**.

### 5.3 Atomicity

Each externally-triggered credit is **one atomic unit**: update `Deposit` → `record_entry()` → update `AssetBalance` cache → write `AuditLog`. Any failure rolls back all of it **[P]**.

`select_for_update()` is taken on: the `Deposit` row, the `AssetBalance` cache row, and the `CUSTOMER:{user}:{asset}` `LedgerAccount` row **[P]**.

**Postgres is required** — `select_for_update()` is a no-op on SQLite, and partial unique indexes / triggers are Postgres features **[V]**. Local SQLite development must never hold real funds.

---

## 6. Custody & Treasury

### 6.1 Crypto custody — undecided by design

Per project-owner decision, the custody model is **not yet chosen**. The architecture commits to the *seam* rather than the choice:

- `DepositProvider(code="crypto_managed", ...)` — a managed custodian issues addresses and holds keys. We store the address↔user mapping only. **We never hold a private key** **[P]**.
- `DepositProvider(code="crypto_selfhosted", ...)` — we derive addresses from an xpub held in a KMS/HSM, private keys never in the app DB or repo **[P]**.

Both implement `DepositAdapter`. Switching provider = changing `DepositProvider.is_active`, not rewriting code **[P]**.

**Recommendation carried forward** from the crypto design §5.2: for a small team operating from Nigeria, a **managed provider that is eligible in the operating jurisdiction** carries the lowest initial risk — but provider eligibility must be verified in writing with the provider and with counsel before any dependency is added **[P/A]**. Nigerian-eligible options to evaluate include local licensed exchanges/custodians as well as international processors **[A — no provider is recommended or endorsed here]**.

### 6.2 Bank treasury

- A single **collection account** receives reserved-account credits. Per-deposit attribution comes from the reserved account number, not from the collection account itself **[P]**.
- A **float/buffer** account holds funds expected but not yet credited **[P]**.
- **CBN/FX**: any NGN receipt rules (including conversion requirements for foreign currency received) must be confirmed with counsel and the receiving bank **[A]**.

### 6.3 Card treasury

- Card funds land in a **PSP balance**. We are exposed to the PSP as a merchant, not as a bank **[P]**.
- **Reserve fund**: a working-capital float is required to absorb disputes in the window between a dispute landing and the client's funds being recovered via `CUSTOMER_RECEIVABLE` **[P]**.
- Settlement timing (T+N) means PSP balance ≠ ledger treasury at all times; reconciliation (§9) accounts for in-flight settlement **[P]**.

### 6.4 Withdrawal policy (out of scope, noted)

Withdrawals require: allowlist of destinations, per-transaction and daily limits, cooling-off for new destinations, two-person approval above a threshold, hot-wallet float cap, and — critically — **the reserved-balance check**: a client may not withdraw card-funded principal inside the reserve window (§2.3) **[P]**. Full design is deferred.

---

## 7. Fees, FX & Limits

### 7.1 Fee model

```
FeeSchedule(provider, rail, asset, fee_type, fee_percent, fee_fixed,
             min_fee, max_fee, absorbed_by, effective_from, is_active)
fee_type: DEPOSIT | WITHDRAWAL | NETWORK | FX_SPREAD
absorbed_by: PLATFORM | CLIENT
```

- **Crypto**: network fee is an expense (gas); a deposit fee may be charged in the crypto asset and withheld at credit time (§5.1) **[P]**.
- **Bank**: the PSP fee is typically charged out of the settled amount, so the client receives `settled_amount − fee` **[P]**.
- **Card**: interchange is charged by the acquirer and is a platform cost; it may be passed to the client explicitly or absorbed as a marketing cost **[P]**.

### 7.2 FX

- Every non-`source_currency == asset` deposit stores `source_amount`, `source_currency` and `fx_rate` **[P]**.
- The rate is captured **at the moment of finalisation**, from a named source, with a timestamp **[P]**.
- FX spread is a deliberate revenue line, not a rounding artefact (§16) **[P]**.

### 7.3 Limits

```
LimitTier(tier, asset, max_deposit_per_tx, max_deposit_per_day,
          max_deposit_per_month, max_withdrawal_per_day)
```

Enforced **at intent creation** (so a client cannot open an intent they can never use) and re-checked at credit time **[P]**. `LimitTier` is driven by `KycProfile.tier` (§3.1).

---

## 8. Processing, Concurrency & Recovery

### 8.1 Job queue

No Celery today **[V]**. A **DB-backed `Job` table** polled by a management command (`python manage.py run_deposit_worker`) avoids new infrastructure and survives restarts **[P]**.

```
Job(id, kind, payload_json, run_at, attempts, max_attempts, status,
    last_error, locked_by, locked_at, created_at, updated_at)
    kind: DETECT | VERIFY | CREDIT | RESERVE_RELEASE | RECONCILE | SWEEP
```

- Jobs are **idempotent**; retries re-run verification and re-attempt the same posting, which is a no-op if already credited **[P]**.
- Bounded retries with exponential backoff; a dead-letter state after `max_attempts` that raises an admin alert **[P]**.
- Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres) so multiple workers can run safely **[P]**.

### 8.2 Webhook handling

```
POST /api/webhooks/<provider>/
```

1. Read the **raw body** (no re-serialisation before hashing).
2. Verify the provider signature with HMAC + constant-time compare, plus a timestamp window (±5 min) to bound replay.
3. Insert `ProviderEvent` with `unique(provider, event_id)`; duplicate → return `200` without processing.
4. Return `2xx` **immediately**, enqueue a `Job`.
5. **Never credit synchronously from the webhook body.** The job re-verifies against the source of truth.

Secrets live in env/secret manager, never in the DB or logs **[P]**.

### 8.3 Idempotency matrix

| Constraint | Protects against |
|---|---|
| `ProviderEvent (provider, event_id)` unique | duplicate/replayed webhook |
| `Deposit (provider, provider_ref)` unique | double credit from a repeated event |
| `LedgerEntry (reference_type, reference_id)` unique | replaying a business operation |
| `DepositAddress (network, address)` unique | address collision / cross-user exfiltration |
| `AssetBalance (user, asset)` unique | multiple cache rows / lost updates |
| `BankDeposit.provider_account` unique | two deposits claiming one account |
| `CardDeposit.psp_session_id` unique | two deposits claiming one session |
| `Job` unique on `(kind, dedupe_key)` | duplicate job execution |

### 8.4 Manual recovery tools (admin, audited)

- Re-verify a deposit (idempotent).
- Force a deposit into `MANUAL_REVIEW` and resolve it.
- Retry a failed job.
- Post an approved adjustment (requires **two** admins).
- Release a reserve early (requires two admins, reason recorded).

All write `AuditLog` rows **[P]**.

---

## 9. Reconciliation

Reconciliation is the difference between a broker and an amateur. Every rail gets it.

### 9.1 Three-way reconciliation

For each provider and each period:

| Check | Crypto | Bank | Card |
|---|---|---|---|
| **A. Ledger ↔ provider** | Ledger `TREASURY:{asset}` total vs actual on-chain balance of controlled addresses | Ledger `TREASURY:NGN` vs PSP settlement balance + bank statement | Ledger `TREASURY:USD` vs PSP balance |
| **B. Provider ↔ events** | Every `CREDITED` deposit's tx still exists and is canonical | Every settled transfer in the PSP report has a `Deposit` | Every captured session has a `Deposit` |
| **C. Cache ↔ ledger** | `AssetBalance.available + reserved` vs ledger-derived `CUSTOMER` balance (all rails) | same | same |

- Drift posts to `RECONCILIATION_CLEARING:{asset}` and raises an alert. **Never auto-silently adjust** — human approval required **[P]**.
- A `CUSTOMER_RECEIVABLE` shortfall raises a `REVERSED`/`REVERSING` state on the deposit and freezes the account's withdrawals until resolved **[P]**.
- `ReconciliationRun` persists the evidence per run **[P]**.

### 9.2 Cadence

| Job | Cadence |
|---|---|
| Provider event polling | Every 30–60 s (or webhook-driven) |
| Cache ↔ ledger check | Every 5 min |
| Provider ↔ ledger three-way | Hourly |
| Statement ingest (banks/wires) | Daily |
| Reserve release sweep | Hourly |
| Full sweep report | Daily, 08:00 local |

### 9.3 Alerts

Admin dashboard banner + email + structured log. Alert on: reconciliation drift, provider event signature failures above a threshold, reversal rate spike, dispute rate spike, balance mismatch, job dead-letter, provider outage **[P]**.

---

## 10. Compliance & Security

### 10.1 KYC gate

No deposit intent without `KycProfile.status == APPROVED`, on **any** rail **[P]**. Enforced in the service layer, not just the view, so no path bypasses it **[P]**.

### 10.2 AML / Travel Rule

- **Travel Rule**: originator data (name, account number, address/country) must accompany the transfer and be recorded for crypto and bank rails **[A — obligations and exact data set must be confirmed by counsel]**.
- `KycProfile` carries the customer's identity data needed to satisfy this **[P]**.
- Sanctions screening on counterparties and on withdrawal destinations **[P]**.
- Structuring detection: multiple deposits just under a reporting threshold **[A — thresholds to be confirmed]**.
- Suspicious-activity cases raise a support ticket and freeze the affected deposits pending review **[P]**.

### 10.3 Regulatory posture (assumption — must be confirmed)

Operating a crypto deposit/custody service, and accepting client funds across these rails, from Nigeria likely engages **SEC Nigeria** (digital asset rules), **CBN** (payment/banking/FX rules) and AML/CFT/Travel Rule obligations **[A]**. A compliance review is a **hard prerequisite** before any real-funds go-live. This document makes no legal claim **[A]**.

### 10.4 Threat model (multi-rail additions)

Extends the crypto design §8 table:

| Threat | Vector | Mitigation |
|---|---|---|
| **Forged bank credit** | Attacker calls the bank webhook | HMAC + timestamp + **independent PSP query** before crediting |
| **Attribution hijack** | Credit arrives for the wrong client | Reserved account number is the key; sender-name mismatch → `MANUAL_REVIEW` |
| **Card PAN leakage** | PAN stored/logged by us | PSP-hosted fields only; we never see the PAN; last4 only; log redaction |
| **Dispute after withdrawal** | Client withdraws, then disputes | Reserve window blocks withdrawal of card-funded principal; `CUSTOMER_RECEIVABLE` + freeze |
| **Card-funded NGN reversal** | Client funds a bank transfer with a card, then disputes | `is_card_funded` tracking; reserve + caps + reversal alerts |
| **Duplicate credit across rails** | Same money credited twice via two rails | Every credit requires a `provider_ref` unique per provider + ledger reference uniqueness |
| **Race on balance** | Concurrent spend and deposit | `select_for_update()` + derived-balance verification + cache assertion |
| **Demo/real confusion** | Client believes simulated funds are real | Separate models, separate endpoints, explicit UI labelling |
| **Provider outage** | PSP/bank/custodian down | Queue absorbs; alerts; no partial credits |
| **Secret leakage** | Webhook secrets in logs/env dumps | No secrets in logs; env/secret manager only; redaction |

### 10.5 PCI scope

Keep the card flow **entirely within the PSP's hosted fields / checkout**. Do not proxy card data through VELTRIX. VELTRIX stores: token, brand, last4, expiry month/year, auth code. Never: PAN, CVV, full track data **[P]**.

### 10.6 Emergency controls

| Flag | Effect |
|---|---|
| `FUNDING_DEPOSITS_ENABLED` | Master switch for all real deposit intake |
| `FUNDING_DEPOSITS_CRYPTO_ENABLED` | Per-rail switch |
| `FUNDING_DEPOSITS_BANK_ENABLED` | Per-rail switch |
| `FUNDING_DEPOSITS_CARD_ENABLED` | Per-rail switch |
| `FUNDING_WITHDRAWALS_ENABLED` | Withdrawal switch |
| `FUNDING_RESERVE_RELEASE_ENABLED` | Reserve auto-release switch (freeze = no promotion) |
| `CUSTODY_FROZEN` | Global freeze on crediting/withdrawals; never corrupts the ledger |
| `FUNDING_DEMO_TOPUP_ENABLED` | **Existing** — simulated top-up, already forced off in production (`settings.py:195-197`) |

Every flag defaults **off**, follows the existing `os.environ.get(...) == "1"` style, and — like the existing demo flag — is **forced off** when the deployment looks like production **[P]**.

---

## 11. API Contracts

### 11.1 Customer (JWT required)

```
GET    /api/wallet/assets/                -> [{code, display_name, decimals, asset_class, networks:[...]}]
GET    /api/wallet/balances/               -> [{asset, available, reserved, pending, updated_at}]
GET    /api/wallet/deposits/               -> paginated [{id, rail, asset, amount, status, created_at, ...}]
GET    /api/wallet/deposits/<id>/          -> detail (own only)

POST   /api/wallet/deposit-intents/        {rail, asset, amount, source_currency?}
                                         -> {deposit_id, status:"PENDING", instructions:{...}, expires_at}
GET    /api/wallet/deposit-intents/<id>/   -> instructions + live status

# rail-specific instruction payloads
#   CRYPTO: {network, address, memo, required_confirmations, min_deposit, expires_at}
#   BANK:   {bank_name, account_number, account_name, amount, reference, expires_at}
#   CARD:   {psp_session_id, public_key, expires_at}   (client completes on PSP)

POST   /api/wallet/deposits/<id>/cancel/  -> cancel an unpaid intent
```

**No customer endpoint ever accepts an amount to credit.** The client states the amount it *intends* to pay at intent creation; the system credits only what it independently observes arriving **[P]**.

### 11.2 Admin (`IsPlatformAdmin`)

```
GET    /api/admin/deposits/                        -> all deposits (filters: rail, status, provider, date)
GET    /api/admin/deposits/<id>/                   -> detail + evidence + ledger entry
POST   /api/admin/deposits/<id>/reverify/          -> re-run verification (idempotent)
POST   /api/admin/deposits/<id>/review/            -> resolve MANUAL_REVIEW (two admins for value changes)
POST   /api/admin/deposits/<id>/release-reserve/   -> early reserve release (two admins)
POST   /api/admin/deposits/<id>/adjust/            -> approved adjustment (two admins)
GET    /api/admin/provider-events/                 -> webhook inbox + signature audit
GET    /api/admin/reconciliation/                  -> drift report
GET    /api/admin/reconciliation/runs/             -> historical runs
GET    /api/admin/providers/                       -> provider config (secrets masked)
POST   /api/admin/providers/<id>/toggle/           -> enable/disable a rail (audited)
```

### 11.3 Webhooks (signature-gated, no JWT)

```
POST   /api/webhooks/crypto/<provider>/
POST   /api/webhooks/bank/<provider>/
POST   /api/webhooks/card/<provider>/
```

All three behave identically: raw body → HMAC verify → dedupe insert → `2xx` ack → enqueue **[P]**.

### 11.4 Example deposit detail

```json
{
  "id": 91,
  "rail": "card",
  "asset": "NGN",
  "status": "RESERVED",
  "expected_amount": "150000.00",
  "received_amount": "150000.00",
  "source_currency": "USD",
  "source_amount": "100.00",
  "fx_rate": "1500.000000000000000000",
  "fee_amount": "2500.00",
  "credited_at": "2026-01-01T00:00:00Z",
  "available_at": "2026-05-01T00:00:00Z",
  "ledger_entry_id": 4421,
  "card": { "brand": "visa", "last4": "4242", "dispute_status": "none" }
}
```

---

## 12. Settings

```python
# All funding flags default OFF and are forced OFF in production-like
# deployments, mirroring FUNDING_DEMO_TOPUP_ENABLED (settings.py:185-197).
FUNDING_DEPOSITS_ENABLED          = os.environ.get("FUNDING_DEPOSITS_ENABLED", "0") == "1"
FUNDING_DEPOSITS_CRYPTO_ENABLED   = os.environ.get("FUNDING_DEPOSITS_CRYPTO_ENABLED", "0") == "1"
FUNDING_DEPOSITS_BANK_ENABLED     = os.environ.get("FUNDING_DEPOSITS_BANK_ENABLED", "0") == "1"
FUNDING_DEPOSITS_CARD_ENABLED     = os.environ.get("FUNDING_DEPOSITS_CARD_ENABLED", "0") == "1"
FUNDING_WITHDRAWALS_ENABLED       = os.environ.get("FUNDING_WITHDRAWALS_ENABLED", "0") == "1"
FUNDING_RESERVE_RELEASE_ENABLED   = os.environ.get("FUNDING_RESERVE_RELEASE_ENABLED", "0") == "1"
CUSTODY_FROZEN                    = os.environ.get("CUSTODY_FROZEN", "0") == "1"

FUNDING_DEPOSIT_MIN = Decimal(os.environ.get("FUNDING_DEPOSIT_MIN", "0"))
FUNDING_DEPOSIT_MAX = Decimal(os.environ.get("FUNDING_DEPOSIT_MAX", "0"))

# Card dispute reserve window (days). Business decision, not an engineering one.
CARD_RESERVE_WINDOW_DAYS = int(os.environ.get("CARD_RESERVE_WINDOW_DAYS", "120"))

# Provider secrets — env/secret manager only, never the database.
CRYPTO_PROVIDER_SECRET  = os.environ.get("CRYPTO_PROVIDER_SECRET", "")
BANK_PROVIDER_SECRET    = os.environ.get("BANK_PROVIDER_SECRET", "")
CARD_PROVIDER_SECRET    = os.environ.get("CARD_PROVIDER_SECRET", "")
CARD_PROVIDER_PUBLISHABLE_KEY = os.environ.get("CARD_PROVIDER_PUBLISHABLE_KEY", "")
```

A **production interlock** must also force these off when `DATABASE_URL` is set or `DEBUG` is false — the pattern already used at `settings.py:195-197` **[P]**.

---

## 13. Application Integration

### 13.1 Backend — affected files (all additive)

| File | Change | Rule |
|---|---|---|
| `veltrix_app/models.py` | New models only | Do not alter `DemoAccount`, `Transaction`, `User` semantics **[P]** |
| `veltrix_app/api/views.py` | `TopUpAPIView` untouched; `DashboardAPIView` gains an additive wallet section | `cash_balance` stays simulated **[P]** |
| `veltrix_app/api/urls.py` | Add `wallet/`, `admin/` deposit routes, webhook routes | Additive **[P]** |
| `veltrix_app/api/serializers.py` | New serializers; existing untouched | Additive **[P]** |
| `veltrix_app/api/admin_views.py` / `admin_urls.py` | New read + action views | `IsPlatformAdmin` **[P]** |
| `veltrix_app/services/` | New `ledger/`, `adapters/`, `jobs/`, `recon/` packages | Service layer, no chain/provider logic in views **[P]** |
| `veltrix_app/management/commands/run_deposit_worker.py` | DB-backed worker | No new infra **[P]** |
| `veltrix_project/settings.py` | Add flags + provider secrets | Toggle-based, secure defaults **[P]** |
| `veltrix_app/tests*.py` | New test modules | Existing tests must pass **unchanged** **[P]** |

### 13.2 Frontend — affected files

| File | Change |
|---|---|
| `src/routing.js` | `wallet`/`deposits` routes exist **[V]** — wire components |
| `src/App.jsx` | Add wallet fetch alongside `handleTopUp` (`:598`) and `reloadDashboard` (`:330`) |
| `src/components/AppViews.jsx` | Replace the `Money()` placeholder (`:314`) with real `WalletView` / `DepositsView` |
| `src/components/TopUpModal.jsx` | Keep, but label explicitly **demo/simulated** — it already promises *"no payment details are collected"* (`:153`) and that promise must remain true |
| `src/components/DemoBanner.jsx` | Reuse to draw the demo/real boundary |
| `src/api.js` | Add wallet calls; existing interceptor pattern reused |
| `src/services/walletFeed.js` | New — thin fetch helpers matching `profileFeed.js` |
| `src/services/depositFlow.js` | New — rail-specific instruction rendering + polling |
| New: `WalletBalances.jsx`, `DepositRailPicker.jsx`, `CryptoInstructions.jsx`, `BankInstructions.jsx`, `CardInstructions.jsx`, `DepositHistoryTable.jsx` | Proposed |

**Card UI rule:** the card form lives on the **PSP's** domain. VELTRIX renders a button that redirects/opens the PSP checkout. No card input fields in our React tree **[P]**.

### 13.3 Isolating demo top-ups

- `FUNDING_DEMO_TOPUP_ENABLED` already exists and is already forced off in production **[V]**. No change needed **[P]**.
- Real deposits **never** route through `TopUpAPIView`; they use the new wallet endpoints **[P]**.
- The existing demo-topup tests keep passing because the flag stays enabled in test settings **[V]**.

---

## 14. Implementation Plan

Every phase is independently testable. **No real funds, no mainnet, and no production provider keys before Phase 7 approval.**

| Phase | Deliverable | Touches a provider? | Real funds? |
|---|---|---|---|
| **0** | `KycProfile` + deposit gate + `Asset`/`AssetBalance`/ledger models + `record_entry()` + immutability + cache-mismatch detection | No | No |
| **1** | `Deposit` + rail detail models + `DepositProvider` + `ProviderEvent` + adapter interface + `Job` table + worker command (no rails wired) | No | No |
| **2** | **Crypto rail** — `DepositAddress`, `Network`, custodian adapter, verification, finality, one testnet asset end-to-end | Testnet | Faucet only |
| **3** | Crypto reconciliation + alerts + admin views + reserve/release machinery (shared, no-op for crypto) | Testnet | No |
| **4** | **Bank rail (local NGN)** — provider adapter, reserved accounts, settlement verification, card-funded flag, reversal path | PSP sandbox | Sandbox only |
| **5** | **Card rail** — PSP session creation, hosted checkout, capture-based crediting, dispute handling, reserve window | PSP sandbox | Test cards |
| **6** | **International wire** (read-only view + admin-assisted manual credit with two-person approval) | Bank sandbox | No |
| **7** | Withdrawals, treasury sweep, FX/conversion design, full reconciliation | Yes | **Requires explicit human approval** |
| **8** | Mainnet readiness: one asset, one rail, small caps, monitoring, alerting on | Yes | **Requires explicit human approval** |

**Phase 0–1 are provider-agnostic and are the reusable core.** They are worth doing first regardless of which rail ships first, because the crypto design §15 already identifies the ledger as the first coding task **[V]**.

---

## 15. Test Scenarios

**Ledger**
- Balanced entry accepted; unbalanced rejected.
- Posting immutability: `UPDATE`/`DELETE` raises.
- Reversal mirrors original; balances return to prior state.
- Derived balance equals cache after N operations; mismatch detection fires and freezes the account.
- Decimal precision preserved for 2-dp (NGN) and 18-dp assets.

**Deposit state machine (all rails, parameterised)**
- Happy path: `PENDING → DETECTED → VERIFYING → VERIFIED → CREDITED → AVAILABLE`, credited exactly once.
- Duplicate provider event (same `event_id`) → single credit.
- Duplicate deposit (same `provider_ref`) → single credit.
- Illegal transition raises.
- Underpayment → no credit, `REJECTED`/`MANUAL_REVIEW`.
- Overpayment → credit the **actual** amount.
- Forged webhook (bad signature) → rejected, no credit, logged.
- Replay with an old timestamp → rejected.
- Webhook vs independent-verification mismatch → `MANUAL_REVIEW`, no credit.

**Crypto-specific**
- Insufficient confirmations → stays `VERIFYING`, no credit.
- Reorg before credit → no ledger impact.
- Reorg after credit → reversing entry; overdraft → receivable + freeze.
- Wrong network → `MANUAL_REVIEW`.

**Bank-specific**
- Reserved account credit → credited to the correct user.
- Sender-name mismatch → `MANUAL_REVIEW`, no auto-credit.
- Amount mismatch → credit actual / `MANUAL_REVIEW`.
- Reversal after credit → reversing entry; if spent → receivable + freeze.
- Card-funded transfer reversal → flagged, reserve respected, alert raised.
- Expired reserved account → `MANUAL_REVIEW`.

**Card-specific**
- Authorised but not captured → **no credit**.
- Captured → credited, `status = RESERVED`, `available_at` set.
- `available_at` elapsed, no dispute → promoted to `AVAILABLE`.
- Dispute before `available_at` → `REVERSED`, reversing entry.
- Withdrawal attempted on reserved principal → rejected.
- Friendly-fraud dispute after the client spent → `CUSTOMER_RECEIVABLE` + freeze.
- PAN/CVV never present in any stored field, log line or request body.

**Concurrency**
- Two simultaneous credits to the same account → both applied, no lost update.
- Concurrent withdrawal exceeding balance → exactly one succeeds.
- Concurrent reserve release and dispute → deterministic, one wins, no negative-availability bug.

**Security / authz**
- Unverified (KYC) user → deposit intent refused on every rail.
- Customer cannot credit their own balance via any endpoint.
- Customer cannot read another user's deposits.
- Non-admin cannot hit admin deposit endpoints.
- Webhook endpoint rejects a request without a valid signature.
- Audit row written for every transition, adjustment and admin action.

**Regression (must remain green, unchanged)**
- `veltrix_app/tests.py`, `tests_admin_api.py`, `tests_login.py`, `tests_profile_api.py` pass untouched.
- Demo top-up works when the flag is enabled; `403` when disabled.

**Frontend**
- Demo vs wallet labels render distinctly; instructions render per rail; card flow leaves for the PSP domain.

---

## 16. Decisions Requiring Human Approval

| # | Decision | Current position in this doc | Owner |
|---|---|---|---|
| 1 | **Crypto custody model** (managed vs self-hosted) | **Deliberately undecided.** The adapter seam is built; the choice is deferred | Founder |
| 2 | **Crypto provider + network + asset**, and written eligibility confirmation | None named | Founder + counsel |
| 3 | **Bank rail provider** (who issues reserved accounts) and whether the local NGN rail is rail #2 | Local NGN proposed as rail #2 | Founder |
| 4 | **Card PSP**, and whether cards settle in NGN or USD | NGN-first proposed (avoids FX at first) | Founder |
| 5 | **Reserve window length** and the `(b)` vs `(c)` policy in §2.3 | `(b)` — withdrawal blocked, trading allowed | Founder + risk |
| 6 | **KYC provider vs manual admin review** | Manual admin review proposed | Founder + counsel |
| 7 | **Fee policy**: who bears interchange, network fees, PSP fees; deposit fee charged to client? | Model built, values unset | Founder |
| 8 | **FX spread** and how it is booked in the ledger | Paired `FX_POSITION:{asset}` accounts proposed (§3.5, §5.1); spread booking not specified | Founder |
| 9 | **Whether card-funded NGN transfers are accepted at all** | Accepted but reserved + capped | Founder + risk |
| 10 | **International wire** in or out of scope | Out of scope for v1 | Founder |
| 11 | **Amend the crypto design's `LedgerEntry` uniqueness** to `unique(reference_type, reference_id, asset)` so cross-asset deposits can post one entry per asset (§3.5) | Required amendment — apply in the Phase 0 ledger migration | Founder |
| 12 | **Amend the crypto design's chart of accounts** to add `1100 FX_POSITION:{asset}` (§3.5) | Required amendment — apply in the Phase 0 ledger migration | Founder |
| 13 | **Ledger adoption**: approve the double-entry ledger as the real-funds source of truth | Recommended (§0.3) | Founder |
| 14 | **Worker approach**: DB-backed job table vs Celery | DB-backed table proposed | Founder |
| 15 | **Compliance/legal sign-off** for the Nigeria operating model, KYC/AML, Travel Rule | Hard prerequisite (§10.3) | Counsel |
| 16 | **Monero exclusion** | Excluded (carried from crypto design §5.4) | Founder |
| 17 | **Existing `Transaction` model fate** | Stays the simulated log; real money lives in `Deposit` + ledger | Founder |
| 18 | **`TopUpAPIView` fate** | Already gated off in production; no change | Founder |

---

## 17. Rollout & Rollback

### 17.1 Rollout

1. Phases 0–1 merged behind flags; full regression green.
2. Crypto testnet ingestion enabled in staging; synthetic deposits exercised.
3. Reconciliation + alerting in dry-run for a fixed period.
4. Provider eligibility, custody decision and compliance sign-off obtained.
5. Bank rail on PSP sandbox; card rail on PSP sandbox.
6. Mainnet/main-production: enable **one** rail and **one** asset, small caps, hot-wallet float limit, full monitoring.

### 17.2 Rollback

- Flags stop new activity **without** touching the ledger.
- The ledger is append-only: rollout bugs are corrected with reversing entries, never edits.
- DB migrations are additive; rollback = disable flags + revert the deploy; data remains consistent.
- Reserve release can be frozen independently (`FUNDING_RESERVE_RELEASE_ENABLED`) if disputes spike — clients keep their balances but withdrawals stay blocked while the dispute is investigated **[P]**.

---

## Appendix A — Verified vs Proposed vs Assumption Index

**Verified:** `DemoAccount.cash_balance` is simulated and ungated by any payment verification; `Transaction` is a flat activity log; `TopUpAPIView` is the only funding endpoint and is gated by `FUNDING_DEMO_TOPUP_ENABLED` (forced off in production); no wallet/ledger/deposit/payment/custody/webhook model, endpoint, dependency or worker exists; `User.is_verified` means email-verified, not KYC-verified; the frontend already routes `wallet`/`deposits`/`withdrawals`/`transactions` but renders placeholders; `TopUpModal.jsx` promises no payment details are collected; `select_for_update()` is a no-op on SQLite; Postgres is the production database; CI asserts a Postgres `DATABASE_URL`; migration `0002` was destructive.

**Proposed:** the entire new subsystem — `KycProfile`, `Asset`, `Network`, `Deposit` + rail detail models, `DepositProvider`, `ProviderEvent`, `Job`, `ReconciliationRun`, the double-entry ledger and `record_entry()`/`record_multi_asset_entry()`, the `1100 FX_POSITION:{asset}` account, `AssetBalance` (available/reserved/pending), the unified state machine, the three adapters, the reserve window, fee/limit/FX models, reconciliation, API contracts, settings flags, the application integration plan and the phase plan.

**Assumption:** provider availability and eligibility in Nigeria; specific interchange/PSP/network fee levels; the ~120-day card dispute window; CBN/FX/SEC/AML obligations and exact Travel Rule data; the treatment of card-funded NGN transfers; KYC provider availability; the legal status of a shared collection account and reserved accounts. **All must be confirmed by a human before real funds move.**

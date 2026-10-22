# NOWPayments USDT TRC20 deposit setup

## Settlement model verified

NOWPayments' official payment API describes a merchant-created payment as returning a NOWPayments-generated pay-in address. It reports `finished` when funds have reached the merchant's personal payout address. Its official funds explanation says the non-custodial Payments product automatically sends processed funds to the configured payout wallet; the Custody product is separate. Veltrix uses the Payments API, not NOWPayments Custody. The code explicitly submits the project's payout address and `payout_currency=usdttrc20` for each payment and only credits after a fresh authenticated GET status returns `finished` with matching payment ID, order reference, pay-in address, USDTTRC20 and exact amount.

Sources: [NOWPayments Payments API](https://documenter.getpostman.com/view/7907941/S1a32n38), [Where are my funds stored?](https://nowpayments.io/help/about-nowpayments/about/where-are-my-funds-stored), [USDT TRC20 payments](https://nowpayments.io/supported-coins/usdttrc20-payments).

NOWPayments' current fee article lists 1% for payments without exchange, and 1.5% for fixed-rate or fee-paid-by-user payments, plus network fees. This integration requests both fixed rate and `is_fee_paid_by_user`, so budget for the 1.5% tier and network costs; confirm the account's actual quote and fee behavior before launch. The UI displays the exact provider-returned pay-in amount. The webhook requires a `finished` outcome amount equal to the requested customer credit; any settlement shortfall/difference is held for review instead of credited. [NOWPayments pricing](https://nowpayments.io/pricing) explains the current service and network fees.

NOWPayments requires an account, payout wallet, API key and IPN secret. Its API docs provide a Sandbox for simulated payment cases; the backend selects `https://api-sandbox.nowpayments.io` only when `NOWPAYMENTS_SANDBOX=1`. Sandbox events are deliberately blocked from real-ledger credit, and mocked tests do not establish live settlement or on-chain behavior.

## Account and dashboard setup

1. Create and complete the NOWPayments merchant account and any requested business/KYC review.
2. Add the project owner's controlled TRON wallet as the USDT TRC20 outcome wallet under Store Settings → Payments. Confirm the wallet address independently; the environment value used below must be the same address.
3. Generate the API key and IPN Secret in Store Settings.
4. Set the public HTTPS IPN callback URL to `https://<backend-host>/api/webhooks/nowpayments/`. This implementation also submits it per payment.
5. For initial validation, create/use a separate NOWPayments Sandbox account, add a test outcome wallet, then generate that account's sandbox API key and IPN secret. Use its test-case payment simulation; sandbox does not transfer real funds. The integration requests the sandbox `success` case only when `NOWPAYMENTS_SANDBOX=1`.
6. Create database configuration rows through the Django admin or a controlled management operation:
   - `Asset`: code `USDT`, crypto, 6 decimals, active.
   - `Network`: asset USDT, chain `TRON`, token standard `TRC20`, mainnet (`is_testnet=false`), active, an operator-approved minimum amount, and `required_confirmations=1`. Veltrix waits for NOWPayments' `finished` state, which its API defines as funds reaching the merchant wallet after its chain processing; it does not claim to independently count TRON blocks.
   - `DepositProvider`: code `nowpayments`, crypto rail, active; optionally associate the USDT asset.
7. Complete a small live payment and independently confirm the settlement in NOWPayments and the owner's wallet before setting the live verification switch.

The customer sees NOWPayments' per-payment pay-in address. The provider documents `finished` as funds reaching the merchant's personal payout address, after the payment passes through its processing and fee stages; validate that address and settlement independently during any later live readiness review. Veltrix never invents or reuses a receiving address. Payments that are unmatched, late, incomplete, wrong-token/network/address or amount-mismatched go to review and are not automatically credited.

## Backend environment

Set only in the backend secret manager/environment; never in React:

- `NOWPAYMENTS_API_KEY`
- `NOWPAYMENTS_IPN_SECRET`
- `NOWPAYMENTS_PAYOUT_WALLET` — project-controlled Tron address (Base58Check validated; match the dashboard outcome wallet)
- `NOWPAYMENTS_IPN_CALLBACK_URL` — public HTTPS webhook URL above
- `NOWPAYMENTS_SANDBOX=1` only for sandbox credentials, otherwise `0`
- `NOWPAYMENTS_ENABLED=1` and `FUNDING_DEPOSITS_ENABLED=1` to enable the provider path outside production
- `FUNDING_LIVE_VERIFIED=1` only after the live account, payout destination, callbacks, verification, and wallet settlement have been operationally checked; production forces both funding flags off unless this is explicitly set
- `FUNDING_REQUIRE_KYC=1` (default)

All funding flags default off. No live credentials or wallet were supplied in this repository task, so live funding remains disabled. The deployment owner must perform the dashboard and live settlement steps above. ## Sandbox verification procedure

Use a local, non-production SQLite database (`DEBUG=1`, no `DATABASE_URL`) and a publicly reachable HTTPS tunnel/test host that forwards to this local backend. The callback must be exactly `https://<host>/api/webhooks/nowpayments/`. Do not set `FUNDING_LIVE_VERIFIED=1`.

1. Apply migrations, create a local staff user, and use Django admin to create/activate the three records above. Keep the provider marked test-only; associate USDT as a supported asset.
2. Put the sandbox API key, sandbox IPN secret, callback URL and a valid owner-controlled TRON address in the backend-only environment. Set `NOWPAYMENTS_SANDBOX=1`, `NOWPAYMENTS_ENABLED=1`, `FUNDING_DEPOSITS_ENABLED=1`, `FUNDING_REQUIRE_KYC=1`, and `FUNDING_LIVE_VERIFIED=0`. Approve the local test customer's KYC profile in admin.
3. Run `python manage.py runserver 0.0.0.0:8000`; ensure the public HTTPS callback forwards to port 8000. Start the frontend and create a small sandbox deposit. NOWPayments should simulate the requested success case and POST its signed IPN to the callback.
4. In admin, inspect the `Deposit` and `ProviderEvent`: expected state is `manual_review` with the sandbox-only reason, a valid signature, stored provider response and transaction reference when supplied. Verify the local ledger balance is unchanged. This is intentional: sandbox success must never create a real-money credit.
5. Re-deliver the identical IPN from the NOWPayments dashboard (or replay its exact body and signature from a protected local shell) and confirm the event is still a single row and the deposit/ledger are unchanged. A signed sandbox callback verifies the sandbox callback path only; it does not prove an on-chain transfer or live ledger credit.
6. Run `python manage.py test veltrix_app.tests_nowpayments` to exercise the mocked finished-payment ledger credit, duplicate/replay protection, bad signatures, mismatches, retry and rollback behavior. These tests use synthetic provider responses and do not call NOWPayments.

Example local commands (set secrets through an ignored local `.env` file or secret manager, never paste them into chat or commit them):

```sh
cd Veltrix-Backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

Before any live enablement, separately run a small real payment and verify NOWPayments reports `finished`, the exact owner outcome wallet and USDTTRC20 amount, the on-chain transaction in the owner's wallet, and the corresponding Veltrix ledger entry. Only then may the operator consider setting `FUNDING_LIVE_VERIFIED=1`; no live setting is enabled by this guide.

The mocked test suite has passed. No NOWPayments sandbox payment or real on-chain transaction has been run in this repository session.

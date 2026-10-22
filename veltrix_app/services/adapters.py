"""Funding rail adapters.

An adapter is the only place that knows how a particular rail actually
moves money. The pipeline above it (``services.deposits``) is written against
this interface, so a new rail is a new adapter and nothing else.

An adapter has to answer three questions:

``create_intent``
    What does the client need to do, and what reference will identify the
    money when it arrives?

``detect``
    Has the money arrived, and how much was it?

``verify``
    Is it genuinely ours, beyond doubt, and how many confirmations deep is
    it?

The rule that matters: an adapter never decides on its own that a client
should be credited. It reports a *fact* - "this address received 0.5 BTC and
it has 12 confirmations" - and the pipeline turns facts into ledger entries.
A webhook payload is a claim by the provider, so it is checked against the
provider's API before it is believed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests

from veltrix_app.models import Deposit, DepositProvider, User


class AdapterError(Exception):
    """Raised when an adapter cannot complete an operation."""


class ProviderUnavailable(AdapterError):
    """Raised when the provider is not configured or not reachable."""


@dataclass
class DetectionResult:
    """What the adapter learned by looking at the rail."""

    detected: bool
    amount: Decimal | None = None
    provider_ref: str | None = None
    confirmations: int = 0
    raw: dict | None = None
    reason: str = ""


@dataclass
class VerificationResult:
    """Whether the deposit may be credited."""

    verified: bool
    confirmations: int = 0
    required_confirmations: int = 0
    reason: str = ""
    raw: dict | None = None


class BaseAdapter:
    """The interface every rail implements."""

    code = ""
    rail = ""

    def __init__(self, provider: DepositProvider):
        self.provider = provider

    def create_intent(self, deposit: Deposit) -> dict:
        """Return the instructions shown to the client."""
        raise NotImplementedError

    def detect(self, deposit: Deposit) -> DetectionResult:
        """Look at the rail and report whether the money has arrived."""
        raise NotImplementedError

    def verify(self, deposit: Deposit) -> VerificationResult:
        """Confirm the money is ours beyond doubt."""
        raise NotImplementedError

    def parse_webhook(self, payload: dict, headers: dict | None = None) -> dict:
        """Normalise an inbound provider event into a plain dict."""
        raise NotImplementedError

    def verify_webhook_signature(self, payload: bytes, headers: dict | None = None) -> bool:
        """Check the provider's signature over the raw request body."""
        return False


class ManualBankAdapter(BaseAdapter):
    """Bank transfer into Veltrix's own collection account.

    This is the rail that works today, with no third-party signup: the
    client transfers to a fixed collection account and quotes the unique
    reference we issued, and an operator confirms the receipt in the admin
    console. The reference is the identity of the deposit, which is why a
    dedicated virtual account per deposit (once a bank PSP provides one) is a
    drop-in replacement later on.

    Until a bank statement feed is wired up, "detection" is a human
    confirming that the money is in the account, and "verification" is that
    human also confirming the reference and the amount.
    """

    code = "bank_manual"
    rail = Deposit.Rail.BANK

    def create_intent(self, deposit: Deposit) -> dict:
        return dict(deposit.instructions)

    def detect(self, deposit: Deposit) -> DetectionResult:
        return DetectionResult(
            detected=False,
            reason="Bank receipts are confirmed by an operator until a statement feed is connected.",
        )

    def verify(self, deposit: Deposit) -> VerificationResult:
        if deposit.received_amount is None:
            return VerificationResult(
                verified=False, reason="No confirmed amount on this deposit yet."
            )
        return VerificationResult(verified=True, reason="Confirmed by an operator.")


def is_tron_address(value: str) -> bool:
    """Validate a mainnet Base58Check TRON address, including checksum."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not isinstance(value, str) or not value or any(char not in alphabet for char in value):
        return False
    number = 0
    for char in value:
        number = number * 58 + alphabet.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    body = (b"\x00" * (len(value) - len(value.lstrip("1")))) + body
    if len(body) != 25 or body[0] != 0x41:
        return False
    payload, checksum = body[:-4], body[-4:]
    first = hashlib.sha256(payload).digest()
    expected = hashlib.sha256(first).digest()[:4]
    return hmac.compare_digest(checksum, expected)


class CryptoAdapter(BaseAdapter):
    """NOWPayments USDT TRC20 pay-ins with direct owner-wallet settlement.

    IPNs are authenticated, then the payment status is fetched from the
    provider API. A webhook body alone can never authorize a ledger credit.
    """

    code = "crypto_custody"
    rail = Deposit.Rail.CRYPTO

    @staticmethod
    def api_root():
        sandbox = os.environ.get("NOWPAYMENTS_SANDBOX", "0") == "1"
        return "https://api-sandbox.nowpayments.io" if sandbox else "https://api.nowpayments.io"

    def create_intent(self, deposit: Deposit) -> dict:
        if self.provider.code != "nowpayments":
            raise ProviderUnavailable("No crypto provider is configured.")
        api_key = os.environ.get("NOWPAYMENTS_API_KEY", "").strip()
        secret = os.environ.get("NOWPAYMENTS_IPN_SECRET", "").strip()
        callback_url = os.environ.get("NOWPAYMENTS_IPN_CALLBACK_URL", "").strip()
        payout_wallet = os.environ.get("NOWPAYMENTS_PAYOUT_WALLET", "").strip()
        callback = urlsplit(callback_url)
        if (not all((api_key, secret, callback_url)) or callback.scheme != "https"
                or not callback.netloc or callback.username or callback.password
                or not is_tron_address(payout_wallet)):
            raise ProviderUnavailable("NOWPayments configuration requires API credentials, an HTTPS callback URL, and a valid TRON payout wallet.")
        request_data = {"price_amount": str(deposit.expected_amount), "price_currency": "usd",
            "pay_currency": "usdttrc20", "payout_address": payout_wallet,
            "payout_currency": "usdttrc20", "is_fixed_rate": True,
            "is_fee_paid_by_user": True, "order_id": deposit.reference,
            "order_description": f"Veltrix deposit {deposit.reference}",
            "ipn_callback_url": callback_url}
        # NOWPayments' sandbox accepts a test case selector on payment
        # creation. Never send this provider-specific field to production.
        if os.environ.get("NOWPAYMENTS_SANDBOX", "0") == "1":
            request_data["case"] = "success"
        try:
            response = requests.post(f"{self.api_root()}/v1/payment",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json=request_data, timeout=(5, 20))
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
        except (requests.RequestException, ValueError) as exc:
            raise ProviderUnavailable("NOWPayments could not create the payment. Retry safely.") from exc
        payment_id = str(data.get("payment_id") or "")
        address = str(data.get("pay_address") or "")
        if not payment_id or not is_tron_address(address) or str(data.get("pay_currency", "")).lower() != "usdttrc20":
            raise ProviderUnavailable("NOWPayments returned incomplete or unexpected USDTTRC20 instructions.")
        try:
            amount = Decimal(str(data.get("pay_amount")))
            if not amount.is_finite() or amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError):
            raise ProviderUnavailable("NOWPayments returned an invalid payment amount.")
        deposit.provider_ref = payment_id
        deposit.instructions = {"provider": "NOWPayments", "network": "TRON (TRC20)", "asset": "USDT",
            "address": address, "amount": str(amount), "currency": "USDT",
            "expires_at": data.get("expiration_estimate_date"),
            "note": "The displayed amount includes the provider fee when charged to the payer. Send exactly this USDT amount on TRON (TRC20); wallet network fees may be extra. Other networks are not supported."}
        deposit.save(update_fields=["provider_ref", "instructions", "updated_at"])
        return dict(deposit.instructions)

    def detect(self, deposit: Deposit) -> DetectionResult:
        data = self._get_payment(deposit)
        payment_status = str(data.get("payment_status", "")).lower()
        try:
            amount = Decimal(str(data.get("actually_paid"))) if data.get("actually_paid") is not None else None
        except InvalidOperation:
            amount = None
        return DetectionResult(payment_status not in ("", "waiting"), amount,
            str(data.get("payment_id") or deposit.provider_ref), raw=data, reason=payment_status)

    def verify(self, deposit: Deposit) -> VerificationResult:
        data = self._get_payment(deposit)
        payment_status = str(data.get("payment_status", "")).lower()
        if (str(data.get("payment_id")) != deposit.provider_ref
                or str(data.get("order_id", "")) != deposit.reference
                or str(data.get("pay_currency", "")).lower() != "usdttrc20"
                or str(data.get("pay_address", "")) != str((deposit.instructions or {}).get("address", ""))):
            return VerificationResult(False, reason="Provider payment identity, network, token, or recipient mismatch.", raw=data)
        try:
            amount = Decimal(str(data.get("actually_paid") or "0"))
            provider_pay_amount = Decimal(str(data.get("pay_amount"))) if data.get("pay_amount") is not None else None
            outcome_amount = Decimal(str(data.get("outcome_amount"))) if data.get("outcome_amount") is not None else None
            requested_pay_amount = Decimal(str((deposit.instructions or {}).get("amount", deposit.expected_amount)))
        except InvalidOperation:
            amount = Decimal("0")
            provider_pay_amount = None
            outcome_amount = None
            requested_pay_amount = deposit.expected_amount
        outcome_currency = str(data.get("outcome_currency") or "").lower()
        if provider_pay_amount is not None and provider_pay_amount != requested_pay_amount:
            return VerificationResult(False, reason="Provider pay-in amount changed after payment creation.", raw=data)
        if payment_status == "finished" and (outcome_amount != deposit.expected_amount or outcome_currency != "usdttrc20"):
            return VerificationResult(False, reason="Settled owner-wallet amount or currency does not match the deposit.", raw=data)
        if payment_status == "partially_paid" or (payment_status == "finished" and amount < requested_pay_amount):
            return VerificationResult(False, reason="Underpaid provider payment.", raw=data)
        if payment_status == "finished" and amount > requested_pay_amount:
            return VerificationResult(False, reason="Overpaid provider payment.", raw=data)
        return VerificationResult(payment_status == "finished" and amount == requested_pay_amount,
            confirmations=1 if payment_status == "finished" else 0, required_confirmations=1,
            reason=payment_status, raw=data)

    def _get_payment(self, deposit: Deposit) -> dict:
        api_key = os.environ.get("NOWPAYMENTS_API_KEY", "").strip()
        if not api_key:
            raise ProviderUnavailable("NOWPayments API credentials are not configured.")
        try:
            response = requests.get(f"{self.api_root()}/v1/payment/{deposit.provider_ref}",
                headers={"x-api-key": api_key}, timeout=(5, 20))
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            return data
        except (requests.RequestException, ValueError) as exc:
            raise ProviderUnavailable("NOWPayments status could not be verified. Retry safely.") from exc

    def parse_webhook(self, payload: dict, headers: dict | None = None) -> dict:
        return {"event_id": str(payload.get("payment_id") or ""),
            "event_type": str(payload.get("payment_status") or ""),
            "payment_id": str(payload.get("payment_id") or ""), "order_id": str(payload.get("order_id") or "")}

    def verify_webhook_signature(self, payload: bytes, headers: dict | None = None) -> bool:
        secret = os.environ.get("NOWPAYMENTS_IPN_SECRET", "").strip()
        signature = ""
        if headers:
            signature = headers.get("x-nowpayments-sig", headers.get("X-Nowpayments-Sig", ""))
        if not secret or not signature:
            return False
        try:
            canonical = json.dumps(json.loads(payload.decode("utf-8")), sort_keys=True,
                separators=(",", ":"), ensure_ascii=False).encode()
        except (ValueError, UnicodeDecodeError):
            return False
        expected = hmac.new(secret.encode(), canonical, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, str(signature).lower())


class CardAdapter(BaseAdapter):
    """Card deposits through a payment service provider.

    Veltrix never handles the card number: the client completes the payment
    on the provider's hosted page and we only ever see a token, the brand and
    the last four digits. The client is credited on *capture*, not on
    authorisation, and the funds sit in a reserve window because a card
    payment can still be charged back days later.
    """

    code = "card_psp"
    rail = Deposit.Rail.CARD

    def create_intent(self, deposit: Deposit) -> dict:
        return dict(deposit.instructions)

    def detect(self, deposit: Deposit) -> DetectionResult:
        raise ProviderUnavailable(
            "No card provider is configured. Set FUNDING_RAIL_CARD_ENABLED and the "
            "provider credentials before accepting card deposits."
        )

    def verify(self, deposit: Deposit) -> VerificationResult:
        raise ProviderUnavailable("No card provider is configured for this deployment.")


ADAPTERS = {
    ManualBankAdapter.code: ManualBankAdapter,
    CryptoAdapter.code: CryptoAdapter,
    CardAdapter.code: CardAdapter,
}


def get_adapter(provider: DepositProvider) -> BaseAdapter:
    """Return the adapter that handles a provider."""
    adapter_class = ADAPTERS.get(provider.code)
    if adapter_class is None:
        # Fall back on the rail so a provider added without a bespoke adapter
        # still gets the correct generic behaviour.
        for candidate in (ManualBankAdapter, CryptoAdapter, CardAdapter):
            if candidate.rail == provider.rail:
                adapter_class = candidate
                break
    if adapter_class is None:
        raise AdapterError(f"No adapter for provider {provider.code}.")
    return adapter_class(provider)

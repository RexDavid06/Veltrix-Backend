"""Customer deposit API and NOWPayments IPN receiver."""
import base64
import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from veltrix_app.models import Asset, CryptoDeposit, Deposit, DepositProvider, Network, ProviderEvent
from veltrix_app.services.adapters import AdapterError, ProviderUnavailable, get_adapter
from veltrix_app.services.deposits import (
    DepositError, check_amount, create_deposit_intent, credit_deposit,
    reverse_deposit, send_to_manual_review, transition, validate_nowpayments_quote, verify_deposit,
)
from veltrix_app.services.tron import (
    TronConfigurationError,
    public_tron_configuration,
    record_txid_submission,
)




class IsClientAccount(permissions.BasePermission):
    """Real deposit APIs are for client accounts, with admin/staff read access for diagnostics."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_staff", False):
            return True
        role = getattr(user, "role", None)
        if role == "admin":
            return True
        return bool(role == getattr(user.Role, "CLIENT", "client") and not user.is_staff)


def _amount_text(value, places=None):
    if value is None:
        return None
    text = f"{value:.{places}f}" if places is not None else format(value, "f")
    return text if places is not None else (text.rstrip("0").rstrip(".") if "." in text else text)


def _qr_data_uri(value):
    if not value:
        return None
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    image = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=4)
    buffer = BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _txid_state(deposit, crypto):
    if not crypto:
        return ""
    transfer = deposit.chain_transfers.order_by("-pk").first()
    if transfer:
        return transfer.status
    if crypto.submitted_txid:
        return "submitted_pending_verification"
    return ""


def public_deposit(deposit):
    crypto = getattr(deposit, "crypto", None)
    bank = getattr(deposit, "bank", None)
    instructions = dict(deposit.instructions or {})
    # The client first reviews the USD and USDT quote, then explicitly confirms
    # before the API returns the provider address.
    if deposit.price_amount is not None and not deposit.quote_confirmed_at:
        instructions.pop("address", None)
    address = instructions.get("address")
    transfers = []
    try:
        for transfer in deposit.chain_transfers.order_by("-block_number", "-pk")[:10]:
            transfers.append({
                "id": transfer.pk,
                "txid": transfer.txid,
                "event_index": transfer.event_index,
                "block_number": transfer.block_number,
                "amount": _amount_text(transfer.amount),
                "sender_address": transfer.sender_address,
                "recipient_address": transfer.recipient_address,
                "confirmations": transfer.confirmations,
                "status": transfer.status,
                "review_reason": transfer.review_reason,
                "attribution_reason": transfer.attribution_reason,
                "observed_at": transfer.observed_at,
                "updated_at": transfer.updated_at,
                "credited_at": transfer.credited_at,
                "is_testnet": bool(getattr(transfer.network, "is_testnet", False)),
            })
    except Exception:
        transfers = []
    return {
        "id": deposit.pk, "reference": deposit.reference, "asset": deposit.asset.code,
        "rail": deposit.rail,
        "provider": deposit.provider.code,
        "network": (f"{crypto.network.chain} ({crypto.network.token_standard})" if crypto else deposit.quoted_network or None),
        "minimum_deposit": (_amount_text(crypto.network.min_deposit) if crypto and crypto.network.min_deposit > 0 else None),
        "confirmations": crypto.confirmations if crypto else 0,
        "required_confirmations": crypto.required_confirmations if crypto else None,
        "qr_code": _qr_data_uri(address),
        "expected_amount": None if deposit.provider_creation_uncertain else _amount_text(deposit.expected_amount),
        "requested_amount": _amount_text(crypto.requested_amount) if crypto and crypto.requested_amount is not None else _amount_text(deposit.expected_amount),
        "payable_amount": _amount_text(crypto.payable_amount) if crypto and crypto.payable_amount is not None else _amount_text(deposit.expected_amount),
        "expires_at": crypto.expires_at if crypto else None,
        "provider_creation_uncertain": deposit.provider_creation_uncertain,
        "price_amount": _amount_text(deposit.price_amount, 2),
        "price_currency": deposit.price_currency.upper() or None,
        "veltrix_fee_amount": _amount_text(deposit.fee_amount or Decimal("0"), 2),
        "veltrix_fee_currency": "USD" if deposit.price_amount is not None else None,
        "quoted_amount": _amount_text(deposit.quoted_amount),
        "quoted_currency": deposit.quoted_currency.upper() or None,
        "quoted_network": deposit.quoted_network or None,
        "network_precision": deposit.network_precision,
        "provider_fee_amount": _amount_text(deposit.provider_fee_amount),
        "provider_fee_currency": deposit.provider_fee_currency.upper() or None,
        "quote_expires_at": deposit.quote_expires_at,
        "quote_confirmed": bool(deposit.quote_confirmed_at),
        "received_amount": _amount_text(deposit.received_amount),
        "status": deposit.status, "instructions": instructions,
        "transaction_hash": crypto.txid if crypto else "",
        "submitted_txid": crypto.submitted_txid if crypto else "",
        "txid_state": _txid_state(deposit, crypto),
        "bank_verified": bool(bank and bank.verified_amount is not None) if bank else False,
        "verified_amount": _amount_text(bank.verified_amount) if bank and bank.verified_amount is not None else None,
        "created_at": deposit.created_at,
        "updated_at": deposit.updated_at,
        "chain_transfers": transfers,
    }


class DepositListCreateAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsClientAccount)

    def get_throttles(self):
        # Creating a remote payment is expensive and separately limited. The
        # frontend polls this endpoint for status, so do not apply that
        # creation quota to harmless history reads.
        if self.request.method == "POST":
            self.throttle_scope = "deposit_create"
            return [ScopedRateThrottle()]
        return [UserRateThrottle()]

    def get(self, request):
        qs = Deposit.objects.filter(
            user=request.user,
            rail__in=[Deposit.Rail.CRYPTO, Deposit.Rail.BANK],
        ).select_related("asset", "provider", "crypto__network", "bank").prefetch_related("chain_transfers__network")
        return Response([public_deposit(d) for d in qs[:100]])

    def post(self, request):
        direct = getattr(settings, "CRYPTO_DEPOSIT_MODE", "direct_tron") == "direct_tron"
        enabled = getattr(settings, "TRON_DEPOSITS_ENABLED", False) if direct else getattr(settings, "NOWPAYMENTS_ENABLED", False)
        if not getattr(settings, "FUNDING_DEPOSITS_ENABLED", False) or not enabled:
            return Response({"detail": "USDT deposits are not enabled on this deployment."}, status=503)
        if direct and (
            not getattr(settings, "FUNDING_LIVE_VERIFIED", False)
            or not getattr(settings, "TRON_LIVE_VERIFIED", False)
        ):
            return Response({"detail": "USDT deposits are not enabled on this deployment."}, status=503)
        amount_text = request.data.get("amount" if direct else "amount_usd")
        try:
            amount = Decimal(str(amount_text))
            precision = 6 if direct else 2
            if (not amount.is_finite() or amount <= 0
                    or amount.quantize(Decimal(1).scaleb(-precision)) != amount):
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Enter a valid positive USDT amount (up to 6 decimal places)." if direct else "Enter a valid positive USD amount (up to 2 decimal places)."}, status=400)
        idempotency_key = request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")
        if not idempotency_key or len(str(idempotency_key)) > 64:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)
        provider_code = "direct_tron" if direct else "nowpayments"
        provider = get_object_or_404(DepositProvider, code=provider_code, rail=DepositProvider.Rail.CRYPTO, is_active=True)
        asset = get_object_or_404(Asset, code="USDT", is_active=True)
        if direct:
            try:
                from veltrix_app.services.tron import get_configured_network
                network = get_configured_network()
            except TronConfigurationError as exc:
                return Response({"detail": str(exc)}, status=503)
        else:
            network = get_object_or_404(Network, asset=asset, chain__iexact="TRON", token_standard__iexact="TRC20", is_active=True,
                is_testnet=False)
        try:
            deposit = create_deposit_intent(request.user, provider, asset, amount,
                idempotency_key=str(idempotency_key), network=network)
        except DepositError as exc:
            return Response({"detail": str(exc)}, status=400)
        except AdapterError as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response(public_deposit(deposit), status=201)


class DepositDetailAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsClientAccount)

    def get(self, request, pk):
        deposit = get_object_or_404(
            Deposit.objects.select_related("asset", "provider", "crypto__network", "bank").prefetch_related("chain_transfers__network"),
            pk=pk, user=request.user,
            rail__in=[Deposit.Rail.CRYPTO, Deposit.Rail.BANK],
        )
        return Response(public_deposit(deposit))


class DepositConfigAPIView(APIView):
    """Non-secret funding configuration for the client deposit screen."""

    permission_classes = (permissions.IsAuthenticated, IsClientAccount)

    def get(self, request):
        deposits_enabled = bool(getattr(settings, "FUNDING_DEPOSITS_ENABLED", False))
        crypto_mode = getattr(settings, "CRYPTO_DEPOSIT_MODE", "direct_tron")
        bank_enabled = bool(
            getattr(settings, "BANK_TRANSFERS_ENABLED", False)
            and deposits_enabled
        )
        result = {
            "deposits_enabled": deposits_enabled,
            "crypto_mode": crypto_mode,
            "methods": [
                {
                    "type": "card",
                    "enabled": False,
                    "label": "Card payment",
                    "note": "Card payments coming soon.",
                },
                {
                    "type": "bank_transfer",
                    "enabled": bank_enabled,
                    "label": "Direct bank transfer",
                    "currency": getattr(settings, "BANK_TRANSFER_CURRENCY", "") or "",
                    "instructions": (
                        "To make a direct bank transfer, please reach out to the admin/support "
                        "team to get the verified bank account details. "
                        "Never send funds to details from unverified sources."
                    ),
                },
                {
                    "type": "crypto",
                    "enabled": False,
                    "label": "Crypto deposit",
                    "currency": "USDT",
                },
            ],
            "bank": {
                "enabled": bank_enabled,
                "currency": getattr(settings, "BANK_TRANSFER_CURRENCY", "") or "",
                "instructions": (
                    "Contact the admin/support team to request verified bank-transfer "
                    "details. Never send funds to details from another source."
                ),
            },
            "networks": [],
        }
        if crypto_mode == "direct_tron":
            tron_option_enabled = bool(getattr(settings, "TRON_DEPOSITS_ENABLED", False))
            live_funding_verified = bool(getattr(settings, "FUNDING_LIVE_VERIFIED", False))
            tron_live_verified = bool(getattr(settings, "TRON_LIVE_VERIFIED", False))
            requests_enabled = bool(deposits_enabled and tron_option_enabled and live_funding_verified and tron_live_verified)
            try:
                tron_cfg = public_tron_configuration()
            except TronConfigurationError as exc:
                result["tron"] = {
                    "configured": False,
                    "display_enabled": False,
                    "enabled": False,
                    "requests_enabled": False,
                    "live_enabled": False,
                    "error": str(exc),
                }
            else:
                if requests_enabled:
                    result["tron"] = {
                        **tron_cfg,
                        "configured": True,
                        "display_enabled": True,
                        "requests_enabled": True,
                        "live_enabled": True,
                    }
                else:
                    if tron_option_enabled and deposits_enabled and (not live_funding_verified or not tron_live_verified):
                        error_msg = "Direct TRON deposits are configured, but live verification is pending. New deposit requests are not open on this deployment."
                    elif tron_option_enabled and deposits_enabled:
                        error_msg = "Direct TRON deposits are configured, but new deposit requests are not open on this deployment."
                    elif tron_option_enabled:
                        error_msg = "Direct TRON deposits are configured, but new deposit requests are not open on this deployment."
                    else:
                        error_msg = "Direct TRON deposits are not enabled on this deployment."
                    result["tron"] = {
                        "configured": True,
                        "display_enabled": tron_option_enabled,
                        "enabled": False,
                        "requests_enabled": False,
                        "live_enabled": live_funding_verified and tron_live_verified,
                        "network": tron_cfg.get("network"),
                        "token_standard": tron_cfg.get("token_standard"),
                        "asset": tron_cfg.get("asset"),
                        "decimals": tron_cfg.get("decimals"),
                        "is_testnet": tron_cfg.get("is_testnet"),
                        "error": error_msg,
                    }
                networks = []
                if tron_option_enabled:
                    networks.append({
                        "id": "tron_trc20",
                        "key": "TRC20",
                        "label": "TRON (TRC20)",
                        "asset": tron_cfg.get("asset") or "USDT",
                        "chain": "TRON",
                        "token_standard": "TRC20",
                        "contract_address": (tron_cfg.get("contract_address") or "") if requests_enabled else "",
                        "address": (tron_cfg.get("address") or "") if requests_enabled else "",
                        "decimals": tron_cfg.get("decimals") or 6,
                        "confirmations": tron_cfg.get("required_confirmations") if tron_cfg.get("required_confirmations") is not None else getattr(settings, "TRON_REQUIRED_CONFIRMATIONS", 0),
                        "min_amount": tron_cfg.get("min_amount") or getattr(settings, "TRON_MIN_DEPOSIT", None),
                        "max_amount": tron_cfg.get("max_amount") or getattr(settings, "TRON_MAX_DEPOSIT", None),
                        "enabled": bool(requests_enabled),
                        "display_enabled": True,
                        "configured": True,
                        "is_testnet": bool(tron_cfg.get("is_testnet")),
                        "instructions": "Only send USDT on the TRON (TRC20) network to this address.",
                    })
                # Add ERC20/BEP20 if configured
                erc20_addr = getattr(settings, "ETHEREUM_USDT_RECEIVING_ADDRESS", "") or ""
                if erc20_addr and getattr(settings, "ETHEREUM_DEPOSITS_ENABLED", False):
                    networks.append({
                        "id": "eth_erc20",
                        "key": "ERC20",
                        "label": "Ethereum (ERC20)",
                        "asset": "USDT",
                        "chain": "ETHEREUM",
                        "token_standard": "ERC20",
                        "contract_address": getattr(settings, "ETHEREUM_USDT_CONTRACT", "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
                        "address": erc20_addr,
                        "decimals": 6,
                        "confirmations": getattr(settings, "ETHEREUM_REQUIRED_CONFIRMATIONS", 12),
                        "min_amount": getattr(settings, "TRON_MIN_DEPOSIT", "1"),
                        "max_amount": getattr(settings, "TRON_MAX_DEPOSIT", "100000"),
                        "enabled": bool(deposits_enabled),
                        "display_enabled": True,
                        "configured": True,
                        "is_testnet": False,
                        "instructions": "Only send USDT on the Ethereum (ERC20) network to this address.",
                    })
                bep20_addr = getattr(settings, "BSC_USDT_RECEIVING_ADDRESS", "") or ""
                if bep20_addr and getattr(settings, "BSC_DEPOSITS_ENABLED", False):
                    networks.append({
                        "id": "bsc_bep20",
                        "key": "BEP20",
                        "label": "BNB Smart Chain (BEP20)",
                        "asset": "USDT",
                        "chain": "BSC",
                        "token_standard": "BEP20",
                        "contract_address": getattr(settings, "BSC_USDT_CONTRACT", "0x55d398326f99059fF775485246999027B3197955"),
                        "address": bep20_addr,
                        "decimals": 18,
                        "confirmations": getattr(settings, "BSC_REQUIRED_CONFIRMATIONS", 15),
                        "min_amount": getattr(settings, "TRON_MIN_DEPOSIT", "1"),
                        "max_amount": getattr(settings, "TRON_MAX_DEPOSIT", "100000"),
                        "enabled": bool(deposits_enabled),
                        "display_enabled": True,
                        "configured": True,
                        "is_testnet": False,
                        "instructions": "Only send USDT on the BNB Smart Chain (BEP20) network to this address.",
                    })
                result["networks"] = networks
                for method in result["methods"]:
                    if method.get("type") == "crypto":
                        method["configured"] = bool(networks)
                        method["display_enabled"] = any(n.get("display_enabled") for n in networks)
                        method["enabled"] = any(n.get("enabled") for n in networks)
                        method["networks"] = networks
                        if networks and any(n.get("enabled") for n in networks):
                            active = next((n for n in networks if n.get("enabled")), networks[0])
                            method["address"] = active.get("address") or ""
                            method["contract"] = active.get("contract_address") or ""
                            method["network"] = active.get("label") or "TRON (TRC20)"
                            method["instructions"] = (
                                "Select a network below and send USDT to the displayed address. "
                                "Only send on the selected network."
                            )
                        break
        else:
            result["tron"] = {
                "configured": False,
                "display_enabled": False,
                "enabled": False,
                "requests_enabled": False,
                "live_enabled": False,
                "error": "Direct TRON deposits are not active.",
            }
            result["networks"] = []
        return Response(result)


class DepositTxidAPIView(APIView):
    """Record a client transaction-hash claim; never credit on this call."""

    permission_classes = (permissions.IsAuthenticated, IsClientAccount)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "deposit_txid"

    def post(self, request, pk):
        deposit = get_object_or_404(
            Deposit.objects.select_related("asset", "provider", "crypto__network"),
            pk=pk, user=request.user,
            rail=Deposit.Rail.CRYPTO, provider__code="direct_tron",
        )
        try:
            outcome = record_txid_submission(
                deposit, request.data.get("txid"), actor=request.user
            )
        except DepositError as exc:
            return Response({"detail": str(exc)}, status=400)
        deposit.refresh_from_db()
        return Response({"deposit": public_deposit(deposit), **outcome}, status=202)


class BankDepositCreateAPIView(APIView):
    """Create a manual bank-transfer request with no automated account details."""

    permission_classes = (permissions.IsAuthenticated, IsClientAccount)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "deposit_create"

    def post(self, request):
        if not getattr(settings, "FUNDING_DEPOSITS_ENABLED", False):
            return Response({"detail": "Deposits are not enabled on this deployment."}, status=503)
        if not getattr(settings, "BANK_TRANSFERS_ENABLED", False):
            return Response({"detail": "Bank transfers are not enabled on this deployment."}, status=503)
        currency = str(getattr(settings, "BANK_TRANSFER_CURRENCY", "") or "").upper()
        if not currency:
            return Response({"detail": "Bank transfers are unavailable until a currency is configured."}, status=503)
        try:
            amount = Decimal(str(request.data.get("amount")))
            if not amount.is_finite() or amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Enter a valid positive amount."}, status=400)
        idempotency_key = request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")
        if not idempotency_key or len(str(idempotency_key)) > 64:
            return Response({"detail": "A valid Idempotency-Key header is required."}, status=400)
        provider = DepositProvider.objects.filter(
            code="bank_manual", rail=DepositProvider.Rail.BANK, is_active=True
        ).first()
        asset = Asset.objects.filter(code=currency, is_active=True).first()
        if provider is None or asset is None:
            return Response({"detail": "Bank transfers are not configured for this currency."}, status=503)
        try:
            deposit = create_deposit_intent(
                request.user, provider, asset, amount,
                idempotency_key=str(idempotency_key),
            )
        except DepositError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(public_deposit(deposit), status=201)


class DepositQuoteConfirmAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsClientAccount)

    def post(self, request, pk):
        with transaction.atomic():
            deposit = get_object_or_404(
                Deposit.objects.select_for_update().select_related("asset", "provider", "crypto__network"),
                pk=pk, user=request.user, rail=Deposit.Rail.CRYPTO, provider__code="nowpayments",
            )
            if deposit.quote_expires_at is None or deposit.quote_expires_at <= timezone.now():
                if deposit.status == Deposit.Status.PENDING:
                    transition(deposit, Deposit.Status.EXPIRED, reason="NOWPayments quote expired before client confirmation.")
                return Response({"detail": "This payment quote expired. Request a new quote."}, status=410)
            problem = validate_nowpayments_quote(deposit)
            if problem:
                return Response({"detail": problem}, status=409)
            if not deposit.quote_confirmed_at:
                deposit.quote_confirmed_at = timezone.now()
                deposit.save(update_fields=["quote_confirmed_at", "updated_at"])
        return Response(public_deposit(deposit))


@method_decorator(csrf_exempt, name="dispatch")
class NowPaymentsWebhookAPIView(APIView):
    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "provider_webhook"

    def post(self, request):
        if not getattr(settings, "NOWPAYMENTS_ENABLED", False):
            return Response({"detail": "Webhook processing is disabled."}, status=503)
        if (os.environ.get("NOWPAYMENTS_SANDBOX", "0") != "1"
                and not getattr(settings, "NOWPAYMENTS_LIVE_AMOUNT_MODEL_APPROVED", False)):
            return Response({"detail": "Live payment processing is disabled pending amount-model approval."}, status=503)
        provider = DepositProvider.objects.filter(code="nowpayments", rail=DepositProvider.Rail.CRYPTO).first()
        if provider is None:
            return Response({"detail": "Provider is not configured."}, status=503)
        raw = request.body
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "Invalid JSON."}, status=400)
        adapter = get_adapter(provider)
        valid = adapter.verify_webhook_signature(raw, request.headers)
        event_id = hashlib.sha256(raw).hexdigest()
        parsed = adapter.parse_webhook(payload, request.headers)
        event, created = ProviderEvent.objects.get_or_create(
            provider=provider, event_id=event_id,
            defaults={"event_type": parsed["event_type"], "signature_valid": valid,
                "payload_sha256": event_id, "raw_payload": raw.decode("utf-8", errors="replace")[:20000],
                "status": ProviderEvent.Status.RECEIVED},
        )
        if not created and event.status in (ProviderEvent.Status.PROCESSED, ProviderEvent.Status.IGNORED):
            return Response({"received": True, "duplicate": True})
        if valid and not event.signature_valid:
            event.signature_valid = True
            event.error = ""
            event.status = ProviderEvent.Status.RECEIVED
            event.save(update_fields=["signature_valid", "error", "status"])
        if not valid:
            ProviderEvent.objects.filter(pk=event.pk).update(status=ProviderEvent.Status.FAILED,
                error="Invalid NOWPayments IPN signature.", processed_at=timezone.now())
            return Response({"detail": "Invalid signature."}, status=400)

        order_id = parsed.get("order_id")
        deposit = Deposit.objects.filter(provider=provider, reference=order_id, rail=Deposit.Rail.CRYPTO).first()
        if deposit is None:
            ProviderEvent.objects.filter(pk=event.pk).update(status=ProviderEvent.Status.IGNORED,
                error="Unmatched payment order reference.", processed_at=timezone.now())
            return Response({"received": True, "matched": False})
        try:
            with transaction.atomic():
                locked = Deposit.objects.select_for_update().select_related("asset", "provider").get(pk=deposit.pk)
                if parsed.get("payment_id") != locked.provider_ref:
                    send_to_manual_review(locked, reason="Callback payment ID does not match the stored provider payment.")
                    ProviderEvent.objects.filter(pk=event.pk).update(status=ProviderEvent.Status.FAILED,
                        error="Callback payment ID does not match the deposit.", processed_at=timezone.now())
                    return Response({"detail": "Provider payment identity mismatch."}, status=400)
                verification = adapter.verify(locked)
                provider_data = verification.raw or {}
                status_text = str(provider_data.get("payment_status") or "").lower()
                paid = provider_data.get("actually_paid")
                try:
                    amount = Decimal(str(paid)) if paid is not None else None
                except InvalidOperation:
                    amount = None
                crypto = CryptoDeposit.objects.select_for_update().get(deposit=locked)
                txid = provider_data.get("payin_hash") or provider_data.get("txid") or ""
                if txid and not crypto.txid:
                    crypto.txid = str(txid)[:128]
                    crypto.save(update_fields=["txid"])
                if amount is not None:
                    locked.source_amount = amount
                    locked.source_currency = "USDT"
                    locked.save(update_fields=["source_amount", "source_currency", "updated_at"])
                if status_text in ("partially_paid", "finished") and not verification.verified:
                    if locked.status in (Deposit.Status.PENDING, Deposit.Status.DETECTED, Deposit.Status.VERIFYING,
                            Deposit.Status.EXPIRED, Deposit.Status.REJECTED):
                        send_to_manual_review(locked, reason=verification.reason or f"Provider payment amount mismatch: received {amount}.")
                elif status_text == "finished" and verification.verified:
                    outcome_amount = Decimal(str(provider_data.get("outcome_amount")))
                    if os.environ.get("NOWPAYMENTS_SANDBOX", "0") == "1":
                        locked.received_amount = outcome_amount
                        locked.save(update_fields=["received_amount", "updated_at"])
                        send_to_manual_review(locked, reason="Sandbox payments are test-only and cannot credit the real ledger.")
                    elif locked.status in (Deposit.Status.EXPIRED, Deposit.Status.REJECTED, Deposit.Status.MANUAL_REVIEW):
                        send_to_manual_review(locked, reason="Late or previously flagged payment requires operator review.")
                    else:
                        if locked.status == Deposit.Status.PENDING:
                            from veltrix_app.services.deposits import mark_detected
                            mark_detected(locked, amount=outcome_amount, provider_ref=locked.provider_ref)
                        else:
                            locked.received_amount = outcome_amount
                            locked.save(update_fields=["received_amount", "updated_at"])
                        verify_deposit(locked, reason="NOWPayments API reports finished USDTTRC20 payment.")
                        credit_deposit(locked)
                elif status_text in ("confirming", "confirmed", "sending"):
                    if locked.status == Deposit.Status.PENDING:
                        from veltrix_app.services.deposits import mark_detected
                        mark_detected(locked, amount=amount, provider_ref=locked.provider_ref)
                    if status_text in ("confirmed", "sending") and locked.status == Deposit.Status.DETECTED:
                        transition(locked, Deposit.Status.VERIFYING)
                elif status_text in ("expired", "failed", "refunded"):
                    if locked.is_credited:
                        reverse_deposit(locked, reason=f"NOWPayments reports terminal status: {status_text}.")
                    elif locked.status in (Deposit.Status.PENDING, Deposit.Status.DETECTED, Deposit.Status.VERIFYING):
                        transition(locked, Deposit.Status.EXPIRED if status_text == "expired" else Deposit.Status.REJECTED,
                            reason=f"NOWPayments status: {status_text}")
                    elif locked.status in (Deposit.Status.EXPIRED, Deposit.Status.REJECTED):
                        pass
                    else:
                        send_to_manual_review(locked, reason=f"Late or terminal provider status: {status_text}.")
                elif not verification.verified and status_text not in ("", "waiting", "confirming", "confirmed", "sending"):
                    send_to_manual_review(locked, reason=verification.reason or f"Unrecognized provider status: {status_text}.")
                event.raw_payload = json.dumps({"ipn": payload, "verified_status": provider_data}, separators=(",", ":"), default=str)[:20000]
                event.status = ProviderEvent.Status.PROCESSED
                event.processed_at = timezone.now()
                event.error = ""
                event.save(update_fields=["raw_payload", "status", "processed_at", "error"])
        except ProviderUnavailable as exc:
            ProviderEvent.objects.filter(pk=event.pk).update(status=ProviderEvent.Status.FAILED,
                error=str(exc)[:255], processed_at=timezone.now())
            return Response({"detail": "Provider verification temporarily unavailable."}, status=503)
        except Exception as exc:
            ProviderEvent.objects.filter(pk=event.pk).update(status=ProviderEvent.Status.FAILED,
                error=str(exc)[:255], processed_at=timezone.now())
            return Response({"detail": "Payment processing failed and can be retried."}, status=503)
        return Response({"received": True})

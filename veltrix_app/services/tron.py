"""Fail-closed TRON USDT TRC-20 event ingestion.

Veltrix uses ONE public, business-controlled receiving address. This module
never derives, accepts, stores or signs with private key material. Because a
shared address cannot cryptographically attribute an inbound transfer to a
client, matching is conservative:

* a client-submitted transaction hash is only a claim;
* unique payable amounts are an optional matching aid, not proof;
* automatic linking and automatic crediting are separate opt-ins;
* a transfer is credited only after independent canonical-chain verification,
  exact amount matching, sufficient solidity confirmations, and live-operation
  verification (or an explicit administrator approval through the same checks).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from veltrix_app.models import (
    AuditLog,
    ChainScanCursor,
    ChainTransfer,
    Deposit,
    Network,
)
from veltrix_app.services.adapters import is_tron_address

MAINNET_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6
CHAIN_PAGE_LIMIT = 200
TRONGRID_MAINNET_HOST = "api.trongrid.io"
TRONGRID_TESTNET_HOSTS = frozenset({"nile.trongrid.io", "api.shasta.trongrid.io"})
AUTO_MATCH_REASON = "auto: unique payable amount matched one eligible request"
ELIGIBLE_STATUSES = (
    Deposit.Status.PENDING,
    Deposit.Status.DETECTED,
    Deposit.Status.VERIFYING,
    Deposit.Status.VERIFIED,
)


class TronConfigurationError(Exception):
    pass


class TronProviderError(Exception):
    pass


def _is_admin_actor(actor) -> bool:
    return bool(
        actor
        and getattr(actor, "is_authenticated", False)
        and getattr(actor, "is_active", False)
        and getattr(actor, "is_admin", lambda: False)()
    )


def _secure_url(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _endpoint_host(value: str) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def validate_tron_endpoint():
    """Fail closed when the indexer endpoint and selected network disagree.

    TronGrid serves mainnet and testnets from different hosts. A testnet
    rehearsal must never query the mainnet host (and vice versa), otherwise
    network, contract and confirmation checks would run against the wrong
    chain.
    """
    if not _secure_url(settings.TRON_API_BASE_URL):
        raise TronConfigurationError("TRON_API_BASE_URL must be an HTTPS node or indexer URL.")
    network = settings.TRON_NETWORK
    if network not in ("mainnet", "testnet"):
        raise TronConfigurationError("TRON_NETWORK must be mainnet or testnet.")
    host = _endpoint_host(settings.TRON_API_BASE_URL)
    if not host:
        raise TronConfigurationError("TRON_API_BASE_URL must be an HTTPS node or indexer URL.")
    if host == TRONGRID_MAINNET_HOST:
        if network != "mainnet":
            raise TronConfigurationError(
                "TRON_API_BASE_URL is the TronGrid mainnet endpoint, which cannot be used "
                "when TRON_NETWORK=testnet."
            )
        return
    if host in TRONGRID_TESTNET_HOSTS:
        if network != "testnet":
            raise TronConfigurationError(
                "TRON_API_BASE_URL is a TronGrid testnet endpoint, which cannot be used "
                "when TRON_NETWORK=mainnet."
            )
        return
    if host == "trongrid.io" or host.endswith(".trongrid.io"):
        raise TronConfigurationError(
            "TronGrid endpoints must be https://api.trongrid.io (mainnet), "
            "https://nile.trongrid.io, or https://api.shasta.trongrid.io (testnet)."
        )


def configured_contract() -> str:
    """Return the pinned mainnet contract, or a required explicit testnet one."""
    if settings.TRON_NETWORK == "mainnet":
        return settings.TRON_USDT_CONTRACT or MAINNET_USDT
    if not settings.TRON_USDT_CONTRACT:
        raise TronConfigurationError("TRON_USDT_CONTRACT must be configured explicitly for testnet.")
    if settings.TRON_USDT_CONTRACT == MAINNET_USDT:
        raise TronConfigurationError(
            "Testnet must use a test USDT contract, not the official mainnet USDT contract."
        )
    return settings.TRON_USDT_CONTRACT


def receiving_address() -> str:
    address = str(getattr(settings, "TRON_USDT_RECEIVING_ADDRESS", "") or "").strip()
    if not address or not is_tron_address(address):
        raise TronConfigurationError(
            "VELTRIX_USDT_TRC20_RECEIVING_ADDRESS must be a valid public TRON address."
        )
    return address


def require_tron_chain_configuration(*, live=False):
    """Validate public chain/indexer and shared receiving-address config."""
    if settings.CRYPTO_DEPOSIT_MODE != "direct_tron":
        raise TronConfigurationError("Direct TRON monitoring is not selected for this deployment.")
    if settings.TRON_NETWORK not in ("mainnet", "testnet"):
        raise TronConfigurationError("TRON_NETWORK must be mainnet or testnet.")
    validate_tron_endpoint()
    contract = configured_contract()
    if not is_tron_address(contract):
        raise TronConfigurationError("TRON_USDT_CONTRACT must be a valid TRON Base58Check address.")
    if settings.TRON_NETWORK == "mainnet" and contract != MAINNET_USDT:
        raise TronConfigurationError("Mainnet USDT contract does not match the trusted USDT TRC-20 contract.")
    receiving_address()
    if settings.TRON_NETWORK == "testnet" and settings.TRON_LIVE_VERIFIED:
        raise TronConfigurationError("Testnet must never be marked as verified for live customer deposits.")
    if settings.TRON_NETWORK == "testnet" and getattr(
        settings, "FUNDING_DEPLOYMENT_IS_PRODUCTION", False
    ):
        raise TronConfigurationError(
            "Testnet receiving addresses are not exposed by production deployments."
        )
    if settings.TRON_REQUIRED_CONFIRMATIONS < 1:
        raise TronConfigurationError("TRON_REQUIRED_CONFIRMATIONS must be at least 1.")
    if live and (settings.TRON_NETWORK != "mainnet" or not settings.TRON_LIVE_VERIFIED):
        raise TronConfigurationError("Live TRON deposits are disabled until mainnet operations are verified.")


def require_tron_configuration(*, live=False):
    """Backwards-compatible alias for the full direct-TRON configuration."""
    require_tron_chain_configuration(live=live)


def validate_tron_network(network: Network, *, live=None):
    """Reject inconsistent database rows before attribution or crediting."""
    if network.asset.code.strip().upper() != "USDT":
        raise TronConfigurationError("The configured TRON network asset must be USDT.")
    if network.chain.strip().upper() not in ("TRON", "TRON MAINNET"):
        raise TronConfigurationError("The configured network chain must be TRON.")
    if network.token_standard.strip().upper() != "TRC20":
        raise TronConfigurationError("The configured network token standard must be TRC20.")
    if network.decimals != USDT_DECIMALS or network.asset.decimals != USDT_DECIMALS:
        raise TronConfigurationError("TRON USDT asset and network decimals must both be 6.")
    if network.contract_address != configured_contract():
        raise TronConfigurationError("Database network does not match the configured USDT contract.")
    if network.is_testnet != (settings.TRON_NETWORK == "testnet"):
        raise TronConfigurationError("Database network testnet flag does not match the selected chain.")
    if not network.is_active:
        raise TronConfigurationError("The configured TRON deposit network is inactive.")
    if live is None:
        live = not network.is_testnet
    require_tron_chain_configuration(live=live)


def get_configured_network(*, live=None) -> Network:
    """Resolve exactly one USDT network row; refuse ambiguous configuration."""
    if live is None:
        live = settings.TRON_NETWORK == "mainnet"
    require_tron_chain_configuration(live=live)
    queryset = Network.objects.select_related("asset").filter(
        Q(asset__code__iexact="USDT"),
        Q(chain__iexact="TRON") | Q(chain__iexact="TRON MAINNET"),
        token_standard__iexact="TRC20",
        contract_address=configured_contract(),
        is_testnet=(settings.TRON_NETWORK == "testnet"),
        is_active=True,
    )
    try:
        network = queryset.get()
    except Network.DoesNotExist as exc:
        raise TronConfigurationError("No active USDT network row matches the configured TRON chain.") from exc
    except Network.MultipleObjectsReturned as exc:
        raise TronConfigurationError("Multiple active USDT network rows match the configured TRON chain.") from exc
    validate_tron_network(network, live=live)
    return network


def public_tron_configuration() -> dict:
    """Return non-secret configuration that every client may see."""
    require_tron_chain_configuration(live=False)
    return {
        "address": receiving_address(),
        "network": "TRON",
        "token_standard": "TRC20",
        "asset": "USDT",
        "decimals": USDT_DECIMALS,
        "contract_address": configured_contract(),
        "enabled": bool(
            settings.TRON_DEPOSITS_ENABLED and settings.FUNDING_DEPOSITS_ENABLED
        ),
        "live_enabled": bool(
            settings.TRON_NETWORK == "mainnet"
            and settings.TRON_LIVE_VERIFIED
            and settings.TRON_DEPOSITS_ENABLED
            and settings.FUNDING_DEPOSITS_ENABLED
        ),
        "is_testnet": bool(settings.TRON_NETWORK == "testnet"),
        "unique_amount_enabled": bool(settings.TRON_UNIQUE_AMOUNT_ENABLED),
        "expires_after_minutes": int(settings.TRON_REQUEST_TTL_MINUTES or 0),
        "min_amount": settings.TRON_MIN_DEPOSIT,
        "max_amount": settings.TRON_MAX_DEPOSIT,
    }


def _extract_transfer(item, *, expected_contract: str, decimals: int, require_timestamp: bool):
    def integer(value):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("Expected integer text.")
        text = str(value)
        if not text.isdigit():
            raise ValueError("Expected unsigned integer text.")
        return int(text)

    try:
        contract = item["contract_address"]
        txid = item["transaction_id"]
        index = integer(item["event_index"])
        block = integer(item["block_number"])
        result = item["result"]
        recipient = result["to"]
        sender = result["from"]
        base = integer(result["value"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Malformed TRC-20 Transfer event.") from exc
    if item.get("event_name") != "Transfer":
        raise ValueError("Unexpected contract event type.")
    if contract != expected_contract or not is_tron_address(contract):
        raise ValueError("Unexpected token contract.")
    if not is_tron_address(recipient) or not is_tron_address(sender):
        raise ValueError("Invalid transfer address.")
    if not isinstance(txid, str) or len(txid) != 64 or any(c not in "0123456789abcdefABCDEF" for c in txid):
        raise ValueError("Invalid transaction hash.")
    if index < 0 or block <= 0 or base <= 0 or decimals < 0 or decimals > 18:
        raise ValueError("Invalid event index, block, amount, or token precision.")
    timestamp = item.get("block_timestamp")
    if require_timestamp:
        try:
            timestamp = integer(timestamp)
        except ValueError as exc:
            raise ValueError("Malformed TRC-20 Transfer event timestamp.") from exc
        if timestamp <= 0:
            raise ValueError("Malformed TRC-20 Transfer event timestamp.")
    amount = Decimal(base).scaleb(-decimals)
    return {
        "txid": txid.lower(), "event_index": index, "block_number": block,
        "block_hash": str(item.get("block_hash", "")), "contract_address": contract,
        "sender_address": sender, "recipient_address": recipient,
        "amount_base_units": Decimal(base), "amount": amount,
    }


def parse_transfer_event(item, *, expected_contract: str, decimals: int):
    """Validate and normalize one indexed Transfer log without float math."""
    return _extract_transfer(item, expected_contract=expected_contract, decimals=decimals, require_timestamp=True)


def _retry_delay(response, attempt: int) -> float:
    header = getattr(response, "headers", {}) or {}
    retry_after = header.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except (TypeError, ValueError):
            pass
    backoff = max(0.0, float(getattr(settings, "TRON_HTTP_BACKOFF_SECONDS", 0.5)))
    return min(backoff * (2 ** attempt), 30.0)


def _request(path, params=None):
    # Defense in depth: lower-level callers must not be able to reach a node on
    # the wrong network even if they bypassed the public configuration gate.
    validate_tron_endpoint()
    headers = {"TRON-PRO-API-KEY": settings.TRON_API_KEY} if settings.TRON_API_KEY else {}
    retries = max(0, min(int(getattr(settings, "TRON_HTTP_RETRIES", 3)), 10))
    for attempt in range(retries + 1):
        try:
            if path.startswith("/walletsolidity/") or path.startswith("/wallet/"):
                response = requests.post(
                    settings.TRON_API_BASE_URL + path, json=params or {}, headers=headers, timeout=(5, 20)
                )
            else:
                response = requests.get(
                    settings.TRON_API_BASE_URL + path, params=params, headers=headers, timeout=(5, 20)
                )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt >= retries:
                raise TronProviderError("TRON indexer request failed; cursor was not advanced.") from exc
            import time
            time.sleep(_retry_delay(None, attempt))
            continue
        except requests.RequestException as exc:
            raise TronProviderError("TRON indexer request failed; cursor was not advanced.") from exc
        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt < retries:
                import time
                time.sleep(_retry_delay(response, attempt))
                continue
            raise TronProviderError("TRON indexer remained unavailable; cursor was not advanced.")
        try:
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise TronProviderError("TRON indexer request failed; cursor was not advanced.") from exc
        except ValueError as exc:
            if attempt < retries:
                import time
                time.sleep(_retry_delay(None, attempt))
                continue
            raise TronProviderError("TRON indexer returned invalid JSON; cursor was not advanced.") from exc
        if not isinstance(payload, dict):
            raise TronProviderError("TRON indexer returned an invalid response.")
        return payload
    raise TronProviderError("TRON indexer request failed; cursor was not advanced.")


def _node_head(*, solidity: bool):
    path = "/walletsolidity/getnowblock" if solidity else "/wallet/getnowblock"
    payload = _request(path)
    try:
        raw = payload["block_header"]["raw_data"]
        number = int(raw["number"])
        timestamp = int(raw["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TronProviderError("TRON node returned an invalid block head.") from exc
    if number < 0 or timestamp <= 0:
        raise TronProviderError("TRON node returned an invalid block head.")
    return number, timestamp


def _solidity_block_hash(block_number: int) -> str:
    payload = _request("/walletsolidity/getblockbynum", {"num": int(block_number)})
    block_id = payload.get("blockID") if isinstance(payload, dict) else None
    if not isinstance(block_id, str) or not block_id:
        raise TronProviderError("TRON solidity node did not return the expected block.")
    return block_id


def _transaction_info(txid: str):
    payload = _request("/walletsolidity/gettransactioninfobyid", {"value": txid})
    if not isinstance(payload, dict):
        raise TronProviderError("TRON solidity node returned invalid transaction data.")
    if not payload or payload.get("id") is None:
        return None
    return payload


def _transaction_events(txid: str):
    payload = _request(f"/v1/transactions/{txid}/events", {"only_confirmed": "true"})
    if payload.get("success") is False:
        raise TronProviderError("TRON indexer could not verify the transaction events.")
    data = payload.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise TronProviderError("TRON indexer returned invalid transaction events.")
    return data


def _canonical_transfer(transfer, network: Network):
    """Return (is_canonical, reason, canonical_block_hash) for a saved event."""
    info = _transaction_info(transfer.txid)
    if info is None:
        return False, "Transaction is no longer present on the solidified chain.", ""
    if str(info.get("id", "")).lower() != transfer.txid.lower():
        return False, "Solidified transaction ID does not match the detected transfer.", ""
    try:
        canonical_block = int(info.get("blockNumber"))
    except (TypeError, ValueError):
        return False, "Solidified transaction block number is missing.", ""
    if canonical_block != transfer.block_number:
        return False, "Transaction moved to a different block after detection.", ""
    receipt = info.get("receipt")
    if not isinstance(receipt, dict) or str(receipt.get("result", "")).upper() != "SUCCESS":
        return False, "Solidified transaction receipt is not successful.", ""
    canonical_hash = _solidity_block_hash(canonical_block)
    if transfer.block_hash and transfer.block_hash.lower() != canonical_hash.lower():
        return False, "Block hash changed after detection; possible chain reorganization.", ""
    match = None
    for item in _transaction_events(transfer.txid):
        try:
            parsed = _extract_transfer(
                item,
                expected_contract=configured_contract(),
                decimals=network.decimals,
                require_timestamp=False,
            )
        except ValueError:
            continue
        if parsed["event_index"] == transfer.event_index and parsed["txid"] == transfer.txid.lower():
            match = parsed
            break
    if match is None:
        raise TronProviderError("Canonical transaction event is not indexed yet; retry later.")
    if match["block_number"] != transfer.block_number:
        return False, "Event moved to a different block after detection.", ""
    if match["contract_address"] != transfer.contract_address:
        return False, "Event contract changed after detection.", ""
    if match["recipient_address"] != transfer.recipient_address:
        return False, "Event recipient changed after detection.", ""
    if transfer.sender_address and match["sender_address"] != transfer.sender_address:
        return False, "Event sender changed after detection.", ""
    if match["amount_base_units"] != transfer.amount_base_units:
        return False, "Event amount changed after detection.", ""
    return True, "", canonical_hash


def _flag_transfer_review(transfer_id: int, reason: str):
    from veltrix_app.services.deposits import send_to_manual_review

    with transaction.atomic():
        transfer = ChainTransfer.objects.select_for_update().select_related("deposit").get(pk=transfer_id)
        if transfer.status == ChainTransfer.Status.MANUAL_REVIEW:
            return
        transfer.status = ChainTransfer.Status.MANUAL_REVIEW
        transfer.review_reason = reason[:255]
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "review_reason", "updated_at"])
        if not transfer.deposit_id:
            return
        deposit = Deposit.objects.select_for_update().get(pk=transfer.deposit_id)
        if deposit.is_credited or deposit.status == Deposit.Status.CREDITED:
            AuditLog.objects.create(
                user=deposit.user,
                action="deposit.chain_reorg",
                details=f"deposit={deposit.id} transfer={transfer.id} reason={reason} ledger=unchanged",
            )
        elif deposit.status != Deposit.Status.REVERSED:
            send_to_manual_review(deposit, reason=reason)


def _eligible_deposits(network: Network, *, lock=True):
    queryset = Deposit.objects.filter(
        rail=Deposit.Rail.CRYPTO,
        provider__code="direct_tron",
        crypto__network=network,
        crypto__address=receiving_address(),
        status__in=ELIGIBLE_STATUSES,
    )
    return queryset.select_for_update() if lock else queryset


def _link_transfer(transfer: ChainTransfer, deposit: Deposit, reason: str):
    from veltrix_app.services.deposits import mark_detected

    if transfer.deposit_id and transfer.deposit_id != deposit.pk:
        raise ValueError("Transfer is already linked to a different deposit request.")
    existing = (
        ChainTransfer.objects.filter(deposit=deposit)
        .exclude(pk=transfer.pk)
        .exists()
    )
    if existing:
        raise ValueError("Deposit is already linked to another chain transfer.")
    transfer.deposit = deposit
    transfer.attribution_reason = reason[:255]
    transfer.updated_at = timezone.now()
    transfer.save(update_fields=["deposit", "attribution_reason", "updated_at"])
    crypto = deposit.crypto
    update_fields = []
    if crypto.txid != transfer.txid:
        crypto.txid = transfer.txid
        update_fields.append("txid")
    if crypto.vout != str(transfer.event_index):
        crypto.vout = str(transfer.event_index)
        update_fields.append("vout")
    if update_fields:
        crypto.save(update_fields=update_fields)
    if deposit.status == Deposit.Status.PENDING:
        mark_detected(deposit, amount=transfer.amount, provider_ref=f"{transfer.txid}:{transfer.event_index}")


def attribute_transfer(transfer: ChainTransfer) -> Deposit | None:
    """Conservatively attach an unallocated transfer to at most one request.

    Nothing is linked by default. Automatic linking requires the unique-amount
    feature to be enabled and exactly one unexpired request with that exact
    amount, no competing client claims, and no competing amount candidate.
    """
    if transfer.deposit_id:
        return transfer.deposit
    if not settings.TRON_UNIQUE_AMOUNT_ENABLED or not settings.TRON_AUTO_MATCH_UNIQUE_AMOUNT:
        return None
    network = transfer.network
    now = timezone.now()
    candidates = (
        _eligible_deposits(network)
        .filter(expected_amount=transfer.amount, crypto__unique_amount_suffix__gt=0)
        .filter(chain_transfers__isnull=True)
        .filter(Q(crypto__expires_at__isnull=True) | Q(crypto__expires_at__gt=now))
    )
    candidate_list = list(candidates[:2])
    claims = list(
        _eligible_deposits(network)
        .filter(crypto__submitted_txid__iexact=transfer.txid)
        .filter(Q(crypto__expires_at__isnull=True) | Q(crypto__expires_at__gt=now))[:2]
    )
    if len(claims) > 1:
        transfer.review_reason = "Multiple clients claim this transaction; administrator review required."
        transfer.save(update_fields=["review_reason", "updated_at"])
        return None
    if len(candidate_list) != 1:
        return None
    candidate = candidate_list[0]
    if claims and claims[0].pk != candidate.pk:
        transfer.review_reason = "Transaction amount matches one request but another client submitted the hash; review required."
        transfer.save(update_fields=["review_reason", "updated_at"])
        return None
    _link_transfer(transfer, candidate, AUTO_MATCH_REASON)
    return candidate


def scan_next_page(network: Network, *, limit=CHAIN_PAGE_LIMIT) -> int:
    """Persist one stable timestamp/fingerprint page before advancing the cursor."""
    # Detection is deliberately independent from the live-credit switch.
    # Scanning and canonical re-checks are read-only; crediting is gated below.
    require_tron_chain_configuration(live=False)
    validate_tron_network(network, live=False)
    shared_address = receiving_address()
    start_timestamp = int(getattr(settings, "TRON_SCAN_START_TIMESTAMP", 0))
    if start_timestamp < 0:
        raise TronConfigurationError("TRON_SCAN_START_TIMESTAMP must be a non-negative millisecond timestamp.")
    ChainScanCursor.objects.get_or_create(network=network, defaults={"timestamp": start_timestamp})
    window_ms = max(1, int(getattr(settings, "TRON_SCAN_WINDOW_SECONDS", 3600))) * 1000
    overlap_ms = max(0, int(getattr(settings, "TRON_SCAN_OVERLAP_SECONDS", 120))) * 1000
    lag_ms = max(0, int(getattr(settings, "TRON_SCAN_LAG_SECONDS", 60))) * 1000
    with transaction.atomic():
        cursor = ChainScanCursor.objects.select_for_update().get(network=network)
        if not cursor.timestamp:
            if start_timestamp <= 0:
                raise TronConfigurationError(
                    "TRON_SCAN_START_TIMESTAMP must be configured before the first direct TRON scan."
                )
            cursor.timestamp = start_timestamp
        if cursor.fingerprint and (
            cursor.window_start_timestamp is None or cursor.window_end_timestamp is None
        ):
            cursor.fingerprint = ""
        if cursor.fingerprint:
            window_start = cursor.window_start_timestamp
            window_end = cursor.window_end_timestamp
        else:
            _, head_timestamp = _node_head(solidity=False)
            upper_bound = head_timestamp - lag_ms
            lower_bound = max(0, cursor.timestamp - overlap_ms) if cursor.timestamp else 0
            if upper_bound < lower_bound:
                cursor.updated_at = timezone.now()
                cursor.save(update_fields=["timestamp", "fingerprint", "updated_at"])
                return 0
            window_start = lower_bound
            window_end = min(upper_bound, lower_bound + window_ms - 1)
        params = {
            "event_name": "Transfer",
            "limit": min(max(int(limit), 1), CHAIN_PAGE_LIMIT),
            "order_by": "block_timestamp,asc",
            "only_confirmed": "false",
            "min_timestamp": int(window_start),
            "max_timestamp": int(window_end),
        }
        if cursor.fingerprint:
            params["fingerprint"] = cursor.fingerprint
        payload = _request(f"/v1/contracts/{configured_contract()}/events", params)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise TronProviderError("TRON indexer returned an invalid event page.")
        if payload.get("success") is False:
            raise TronProviderError("TRON indexer reported an unsuccessful event page; cursor remains unchanged.")
        observed = 0
        for item in payload["data"]:
            try:
                event = parse_transfer_event(
                    item,
                    expected_contract=configured_contract(),
                    decimals=network.decimals,
                )
            except ValueError as exc:
                raise TronProviderError("Malformed TRC-20 event; scan cursor remains unchanged.") from exc
            if event["recipient_address"] != shared_address:
                continue
            try:
                transfer, created = ChainTransfer.objects.get_or_create(
                    network=network,
                    txid=event["txid"],
                    event_index=event["event_index"],
                    defaults={**event, "raw_event": item},
                )
                if created:
                    transfer = ChainTransfer.objects.select_for_update().select_related("network", "deposit").get(pk=transfer.pk)
                    attribute_transfer(transfer)
                    observed += 1
            except IntegrityError:
                continue
        meta = payload.get("meta") or {}
        next_fingerprint = meta.get("fingerprint") or ""
        if next_fingerprint:
            if next_fingerprint == cursor.fingerprint:
                raise TronProviderError("TRON indexer pagination did not advance; cursor remains unchanged.")
            cursor.fingerprint = next_fingerprint
            cursor.window_start_timestamp = int(window_start)
            cursor.window_end_timestamp = int(window_end)
        else:
            cursor.timestamp = max(cursor.timestamp, int(window_end) + 1)
            cursor.fingerprint = ""
            cursor.window_start_timestamp = None
            cursor.window_end_timestamp = None
        heights = []
        for event in payload["data"]:
            try:
                heights.append(int(event["block_number"]))
            except (KeyError, TypeError, ValueError):
                continue
        if heights:
            cursor.block_number = max(cursor.block_number, max(heights))
        cursor.updated_at = timezone.now()
        cursor.save(update_fields=[
            "timestamp", "fingerprint", "window_start_timestamp",
            "window_end_timestamp", "block_number", "updated_at",
        ])
        return observed


def refresh_confirmations(network: Network) -> int:
    """Reload canonical state, conservatively attribute, and verify matured transfers."""
    from veltrix_app.services.deposits import send_to_manual_review, verify_deposit

    # Read-only detection/reorg checks do not require the live-credit attestation.
    require_tron_chain_configuration(live=False)
    validate_tron_network(network, live=False)
    solid_height, _ = _node_head(solidity=True)
    required = max(network.required_confirmations, settings.TRON_REQUIRED_CONFIRMATIONS)
    stale_minutes = int(getattr(settings, "TRON_UNCONFIRMED_REVIEW_MINUTES", 0) or 0)
    stale_before = timezone.now() - timedelta(minutes=stale_minutes) if stale_minutes else None
    transfers = list(
        ChainTransfer.objects.filter(network=network)
        .exclude(status=ChainTransfer.Status.MANUAL_REVIEW)
        .select_related("network")
        .order_by("block_number", "pk")
    )
    block_hash_cache = {}
    changed = 0
    for transfer in transfers:
        confirmations = max(0, solid_height - transfer.block_number + 1)
        if (
            stale_before is not None
            and confirmations < required
            and transfer.status != ChainTransfer.Status.CREDITED
            and transfer.observed_at <= stale_before
        ):
            _flag_transfer_review(
                transfer.id,
                f"Transfer did not reach {required} solidity confirmations within {stale_minutes} minutes.",
            )
            changed += 1
            continue
        canonical_hash = ""
        if solid_height >= transfer.block_number:
            if transfer.block_number not in block_hash_cache:
                block_hash_cache[transfer.block_number] = _solidity_block_hash(transfer.block_number)
            canonical_hash = block_hash_cache[transfer.block_number]
        if canonical_hash and transfer.block_hash and transfer.block_hash.lower() != canonical_hash.lower():
            _flag_transfer_review(
                transfer.id,
                "TRON block hash changed after detection; investigate possible chain reorganization.",
            )
            changed += 1
            continue
        verified_hash = ""
        if confirmations >= required and transfer.status != ChainTransfer.Status.CREDITED:
            canonical, reason, verified_hash = _canonical_transfer(transfer, network)
            if not canonical:
                _flag_transfer_review(transfer.id, reason)
                changed += 1
                continue
        with transaction.atomic():
            locked = ChainTransfer.objects.select_for_update().select_related("network", "deposit").get(pk=transfer.id)
            if locked.status == ChainTransfer.Status.MANUAL_REVIEW:
                continue
            if not locked.deposit_id and confirmations >= required:
                attribute_transfer(locked)
                locked.refresh_from_db()
            locked.confirmations = confirmations
            if verified_hash:
                locked.block_hash = verified_hash
            elif canonical_hash and not locked.block_hash:
                locked.block_hash = canonical_hash
            if locked.status == ChainTransfer.Status.CREDITED:
                locked.updated_at = timezone.now()
                locked.save(update_fields=["confirmations", "block_hash", "updated_at"])
                changed += 1
                continue
            if confirmations >= required:
                locked.status = ChainTransfer.Status.CONFIRMED
                if not locked.deposit_id:
                    locked.status = ChainTransfer.Status.MANUAL_REVIEW
                    locked.review_reason = "Confirmed transfer could not be unambiguously attributed to a deposit request."
                else:
                    deposit = Deposit.objects.select_for_update().get(pk=locked.deposit_id)
                    crypto = deposit.crypto
                    crypto.confirmations = confirmations
                    crypto.save(update_fields=["confirmations"])
                    if locked.amount != deposit.expected_amount:
                        send_to_manual_review(
                            deposit,
                            reason=(
                                f"Direct TRON amount mismatch: expected {deposit.expected_amount}, "
                                f"received {locked.amount}."
                            ),
                        )
                        locked.status = ChainTransfer.Status.MANUAL_REVIEW
                        locked.review_reason = "Direct TRON amount mismatch; over/underpayment requires review."
                    elif not settings.TRON_LIVE_VERIFIED and not network.is_testnet:
                        locked.review_reason = "Confirmed, but live crediting is disabled pending operational verification."
                        if deposit.status not in (Deposit.Status.VERIFIED, Deposit.Status.CREDITED):
                            verify_deposit(
                                deposit,
                                reason="TRON transfer reached the configured solidity confirmation threshold.",
                            )
                    elif not deposit.is_credited and deposit.status != Deposit.Status.VERIFIED:
                        verify_deposit(
                            deposit,
                            reason="TRON transfer reached the configured solidity confirmation threshold.",
                        )
            locked.updated_at = timezone.now()
            locked.save(update_fields=[
                "confirmations", "block_hash", "status", "review_reason", "updated_at",
            ])
        changed += 1
    return changed


@transaction.atomic
def credit_confirmed_transfer(
    transfer_id: int, *, actor=None, manual_approval=False,
    allow_amount_mismatch=False, reason="",
):
    """Reverify then credit one final transfer through the shared deposit ledger.

    Automatic callers are only allowed when unique-amount matching and
    automatic crediting are both explicitly enabled. An administrator approval
    uses the same canonical/live checks and is recorded in the audit log.
    """
    from veltrix_app.services.deposits import (
        check_amount, credit_deposit, mark_detected, send_to_manual_review,
        transition, verify_deposit,
    )

    transfer = ChainTransfer.objects.select_for_update().select_related("network", "deposit").get(pk=transfer_id)
    if transfer.status == ChainTransfer.Status.CREDITED:
        return transfer
    if not manual_approval:
        if transfer.status != ChainTransfer.Status.CONFIRMED:
            return transfer
        if not (
            settings.TRON_AUTO_CREDIT_UNIQUE_AMOUNT
            and transfer.attribution_reason == AUTO_MATCH_REASON
        ):
            return transfer
    network = transfer.network
    live = not network.is_testnet
    if not settings.FUNDING_DEPOSITS_ENABLED:
        raise TronConfigurationError("Real deposits are disabled by the global funding switch.")
    require_tron_chain_configuration(live=live)
    validate_tron_network(network, live=live)
    if transfer.recipient_address != receiving_address():
        raise TronConfigurationError("Transfer recipient is not the configured receiving address.")
    if not transfer.deposit_id:
        transfer.status = ChainTransfer.Status.MANUAL_REVIEW
        transfer.review_reason = "Confirmed transfer has no linked deposit request."
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "review_reason", "updated_at"])
        return transfer
    deposit = Deposit.objects.select_for_update().select_related("crypto").get(pk=transfer.deposit_id)
    if deposit.rail != Deposit.Rail.CRYPTO or deposit.provider.code != "direct_tron":
        raise TronConfigurationError("Only direct TRON crypto deposits can be credited from a chain transfer.")
    if deposit.is_credited or deposit.status == Deposit.Status.CREDITED:
        return transfer
    eligible_statuses = ELIGIBLE_STATUSES
    if manual_approval:
        eligible_statuses = eligible_statuses + (Deposit.Status.MANUAL_REVIEW,)
    if deposit.status not in eligible_statuses:
        send_to_manual_review(
            deposit,
            reason="A TRON transfer was approved outside the deposit's active payment window.",
        )
        transfer.status = ChainTransfer.Status.MANUAL_REVIEW
        transfer.review_reason = "Deposit request is no longer eligible."
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "review_reason", "updated_at"])
        return transfer
    solid_height, _ = _node_head(solidity=True)
    confirmations = max(0, solid_height - transfer.block_number + 1)
    required = max(network.required_confirmations, settings.TRON_REQUIRED_CONFIRMATIONS)
    if confirmations < required:
        raise TronConfigurationError("Transfer does not yet have the required solidity confirmations.")
    canonical, canonical_reason, canonical_hash = _canonical_transfer(transfer, network)
    if not canonical:
        send_to_manual_review(deposit, reason=canonical_reason)
        transfer.status = ChainTransfer.Status.MANUAL_REVIEW
        transfer.review_reason = canonical_reason[:255]
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "review_reason", "updated_at"])
        return transfer
    transfer.confirmations = confirmations
    transfer.block_hash = canonical_hash or transfer.block_hash
    crypto = deposit.crypto
    crypto.txid = transfer.txid
    crypto.vout = str(transfer.event_index)
    crypto.confirmations = confirmations
    crypto.save(update_fields=["txid", "vout", "confirmations"])
    mismatch = transfer.amount != deposit.expected_amount
    mismatch_approved = (
        mismatch and allow_amount_mismatch and manual_approval
        and deposit.status == Deposit.Status.MANUAL_REVIEW
        and str(reason or "").strip()
    )
    if mismatch and not mismatch_approved:
        send_to_manual_review(
            deposit,
            reason=(
                f"Direct TRON amount mismatch: expected {deposit.expected_amount}, "
                f"received {transfer.amount}; over/underpayment requires review."
            ),
        )
        transfer.status = ChainTransfer.Status.MANUAL_REVIEW
        transfer.review_reason = "Direct TRON amount mismatch."
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "review_reason", "updated_at"])
        return transfer
    if deposit.status == Deposit.Status.PENDING:
        mark_detected(deposit, amount=transfer.amount, provider_ref=f"{transfer.txid}:{transfer.event_index}")
    if mismatch_approved:
        deposit.received_amount = transfer.amount
        deposit.updated_at = timezone.now()
        deposit.save(update_fields=["received_amount", "updated_at"])
        if deposit.status == Deposit.Status.MANUAL_REVIEW:
            transition(deposit, Deposit.Status.VERIFIED, reason=reason[:255])
    elif check_amount(deposit):
        send_to_manual_review(deposit, reason="Direct TRON transfer failed the configured amount check.")
        transfer.status = ChainTransfer.Status.MANUAL_REVIEW
        transfer.review_reason = "Direct TRON amount check failed."
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "review_reason", "updated_at"])
        return transfer
    else:
        verify_deposit(deposit, reason=reason or "Transfer contract, recipient, event, amount and solidity confirmations verified.")
    credit_deposit(deposit, allow_amount_mismatch=mismatch_approved)
    transfer.status = ChainTransfer.Status.CREDITED
    transfer.credited_at = timezone.now()
    transfer.updated_at = timezone.now()
    transfer.save(update_fields=["status", "credited_at", "updated_at"])
    if manual_approval:
        AuditLog.objects.create(
            user=deposit.user,
            actor=actor,
            action="deposit.crypto_admin_credit",
            details=(
                f"deposit={deposit.id} transfer={transfer.id} txid={transfer.txid} "
                f"amount={transfer.amount} reason={reason[:500]}"
            ),
        )
    return transfer


def approve_transfer(
    transfer_id: int, *, actor, reason: str, allow_amount_mismatch: bool = False
):
    """Administrator approval: re-check finality, then credit or flag."""
    if not _is_admin_actor(actor):
        raise TronConfigurationError("An administrator actor is required.")
    if not str(reason or "").strip():
        raise ValueError("A review reason is required.")
    with transaction.atomic():
        transfer = ChainTransfer.objects.select_for_update().select_related("network").get(pk=transfer_id)
        if transfer.status == ChainTransfer.Status.CREDITED:
            return transfer
        if not transfer.deposit_id:
            raise ValueError("Link the transfer to a deposit request before approval.")
        network = transfer.network
        live = not network.is_testnet
        require_tron_chain_configuration(live=live)
        validate_tron_network(network, live=live)
        solid_height, _ = _node_head(solidity=True)
        confirmations = max(0, solid_height - transfer.block_number + 1)
        required = max(network.required_confirmations, settings.TRON_REQUIRED_CONFIRMATIONS)
        if confirmations < required:
            raise TronConfigurationError("Transfer does not yet have the required solidity confirmations.")
        canonical, canonical_reason, canonical_hash = _canonical_transfer(transfer, network)
        if not canonical:
            raise TronConfigurationError(canonical_reason)
        transfer.status = ChainTransfer.Status.CONFIRMED
        transfer.confirmations = confirmations
        transfer.block_hash = canonical_hash or transfer.block_hash
        transfer.updated_at = timezone.now()
        transfer.save(update_fields=["status", "confirmations", "block_hash", "updated_at"])
        return credit_confirmed_transfer(
            transfer_id,
            actor=actor,
            manual_approval=True,
            allow_amount_mismatch=allow_amount_mismatch,
            reason=reason,
        )


def assign_transfer(transfer_id: int, deposit_id: int, *, actor, reason: str):
    """Explicitly attach an unallocated transfer to one deposit for review."""
    if not _is_admin_actor(actor):
        raise TronConfigurationError("An administrator actor is required.")
    if not str(reason or "").strip():
        raise ValueError("A review reason is required.")
    with transaction.atomic():
        transfer = ChainTransfer.objects.select_for_update().select_related("network", "deposit").get(pk=transfer_id)
        if transfer.deposit_id and transfer.deposit_id != int(deposit_id):
            raise ValueError("Transfer is already linked to a different deposit request.")
        deposit = Deposit.objects.select_for_update().select_related("crypto", "provider").get(pk=deposit_id)
        if deposit.is_credited or deposit.status == Deposit.Status.CREDITED:
            raise ValueError("Target deposit is already credited.")
        if ChainTransfer.objects.filter(deposit=deposit).exclude(pk=transfer.pk).exists():
            raise ValueError("Target deposit is already linked to another chain transfer.")
        if transfer.recipient_address != receiving_address():
            raise ValueError("Transfer did not reach the configured shared receiving address.")
        if deposit.rail != Deposit.Rail.CRYPTO or deposit.provider.code != "direct_tron":
            raise ValueError("Target deposit is not a direct TRON deposit.")
        if deposit.crypto.network_id != transfer.network_id:
            raise ValueError("Target deposit uses a different network.")
        if deposit.crypto.address != receiving_address():
            raise ValueError("Target deposit was not created against the configured shared address.")
        if transfer.deposit_id is None:
            _link_transfer(transfer, deposit, f"admin: {reason}"[:255])
            if transfer.amount != deposit.expected_amount:
                from veltrix_app.services.deposits import send_to_manual_review
                send_to_manual_review(
                    deposit,
                    reason=(
                        f"Direct TRON amount mismatch: expected {deposit.expected_amount}, "
                        f"received {transfer.amount}; over/underpayment requires review."
                    ),
                    actor=actor,
                )
                transfer.status = ChainTransfer.Status.MANUAL_REVIEW
                transfer.review_reason = "Direct TRON amount mismatch; over/underpayment requires review."
                transfer.updated_at = timezone.now()
                transfer.save(update_fields=["status", "review_reason", "updated_at"])
            AuditLog.objects.create(
                user=deposit.user, actor=actor, action="deposit.crypto_transfer_linked",
                details=f"deposit={deposit.id} transfer={transfer.id} txid={transfer.txid} reason={reason[:500]}",
            )
        transfer.refresh_from_db()
        return transfer


def record_txid_submission(deposit: Deposit, txid: str, *, actor=None) -> dict:
    """Record a client transaction-hash claim without trusting it as proof."""
    from veltrix_app.services.deposits import DepositError

    if deposit.rail != Deposit.Rail.CRYPTO or deposit.provider.code != "direct_tron":
        raise DepositError("Transaction hashes are only accepted for direct TRON deposits.")
    if deposit.status not in ELIGIBLE_STATUSES:
        raise DepositError("This deposit request is no longer accepting transaction hashes.")
    normalized = str(txid or "").strip().lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise DepositError("Enter a valid 64-character TRON transaction hash.")
    crypto = deposit.crypto
    if crypto.submitted_txid and crypto.submitted_txid.lower() != normalized:
        raise DepositError("A different transaction hash was already submitted for this deposit.")
    transfer = ChainTransfer.objects.filter(network=crypto.network, txid=normalized).first()
    if transfer and transfer.recipient_address != receiving_address():
        raise DepositError("That transaction did not pay the configured receiving address.")
    if transfer and transfer.deposit_id and transfer.deposit_id != deposit.pk:
        raise DepositError("That transaction is already attributed to another deposit request.")
    if not crypto.submitted_txid:
        crypto.submitted_txid = normalized
        crypto.submitted_at = timezone.now()
        crypto.save(update_fields=["submitted_txid", "submitted_at"])
        AuditLog.objects.create(
            user=deposit.user, actor=actor, action="deposit.txid_submitted",
            details=f"deposit={deposit.id} txid={normalized} status=claim_pending_verification",
        )
    if transfer and transfer.deposit_id == deposit.pk:
        message = "Transaction hash recorded. It is already linked and awaiting confirmation review."
    elif transfer:
        message = "Transaction hash recorded. An administrator must attribute and review the transfer."
    else:
        message = "Transaction hash recorded. It will be checked against the chain; this is not proof of payment."
    return {"txid": normalized, "status": "recorded", "message": message}

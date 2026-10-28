"""Platform-admin deposit review, bank approval and reconciliation APIs."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from veltrix_app.api.permissions import IsPlatformAdmin
from veltrix_app.models import (
    AuditLog,
    BankDeposit,
    ChainTransfer,
    Deposit,
    ReconciliationDiscrepancy,
    ReconciliationRun,
)
from veltrix_app.services.deposits import (
    DepositError, approve_bank_transfer, reject_bank_transfer, send_to_manual_review,
)
from veltrix_app.services.reconciliation import resolve_discrepancy, run_reconciliation
from veltrix_app.services.tron import (
    TronConfigurationError, TronProviderError, approve_transfer, assign_transfer,
)


def _money(value):
    return None if value is None else format(value, "f")


def _deposit_data(deposit):
    crypto = getattr(deposit, "crypto", None)
    bank = getattr(deposit, "bank", None)
    transfers = deposit.chain_transfers.order_by("-block_number", "-pk")[:10]
    return {
        "id": deposit.pk,
        "reference": deposit.reference,
        "user": {
            "id": deposit.user_id,
            "email": deposit.user.email,
            "username": deposit.user.username,
        },
        "rail": deposit.rail,
        "provider": deposit.provider.code,
        "asset": deposit.asset.code,
        "status": deposit.status,
        "review_reason": deposit.review_reason,
        "expected_amount": _money(deposit.expected_amount),
        "received_amount": _money(deposit.received_amount),
        "requested_amount": _money(crypto.requested_amount) if crypto else _money(deposit.expected_amount),
        "payable_amount": _money(crypto.payable_amount) if crypto else None,
        "is_credited": deposit.is_credited,
        "ledger_entry": deposit.ledger_entry_id,
        "credited_at": deposit.credited_at,
        "created_at": deposit.created_at,
        "updated_at": deposit.updated_at,
        "crypto": {
            "network": crypto.network.chain if crypto else None,
            "token_standard": crypto.network.token_standard if crypto else None,
            "address": crypto.address if crypto else None,
            "txid": crypto.txid if crypto else "",
            "submitted_txid": crypto.submitted_txid if crypto else "",
            "submitted_at": crypto.submitted_at if crypto else None,
            "confirmations": crypto.confirmations if crypto else 0,
            "required_confirmations": crypto.required_confirmations if crypto else 0,
            "expires_at": crypto.expires_at if crypto else None,
        } if crypto else None,
        "bank": {
            "provider_reference": bank.provider_reference,
            "verified_amount": _money(bank.verified_amount),
            "verified_currency": bank.verified_currency,
            "verified_reference": bank.verified_reference,
            "verification_source": bank.verification_source,
            "decision_reason": bank.decision_reason,
            "approved_by": bank.approved_by_id,
            "approved_at": bank.approved_at,
            "rejected_by": bank.rejected_by_id,
            "rejected_at": bank.rejected_at,
        } if bank else None,
        "chain_transfers": [_transfer_data(t) for t in transfers],
    }


def _transfer_data(transfer):
    return {
        "id": transfer.pk,
        "txid": transfer.txid,
        "event_index": transfer.event_index,
        "block_number": transfer.block_number,
        "amount": _money(transfer.amount),
        "sender_address": transfer.sender_address,
        "recipient_address": transfer.recipient_address,
        "is_testnet": bool(transfer.network.is_testnet),
        "confirmations": transfer.confirmations,
        "status": transfer.status,
        "review_reason": transfer.review_reason,
        "attribution_reason": transfer.attribution_reason,
        "deposit_id": transfer.deposit_id,
        "observed_at": transfer.observed_at,
        "updated_at": transfer.updated_at,
        "credited_at": transfer.credited_at,
    }


def _paginated(request, queryset, serializer):
    try:
        page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)
        page_number = max(int(request.query_params.get("page", 1)), 1)
    except (TypeError, ValueError):
        page_size, page_number = 20, 1
    page = Paginator(queryset, page_size).get_page(page_number)
    return Response({
        "count": page.paginator.count,
        "page": page.number,
        "pages": page.paginator.num_pages,
        "results": [serializer(item) for item in page.object_list],
    })


def _parse_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Enter a valid verified amount.") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Enter a valid verified amount.")
    return amount


class AdminDepositListView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def get(self, request):
        qs = Deposit.objects.select_related(
            "user", "asset", "provider", "crypto__network", "bank"
        ).filter(rail__in=[Deposit.Rail.CRYPTO, Deposit.Rail.BANK]).order_by("-created_at")
        rail = (request.query_params.get("rail") or "").strip()
        if rail in (Deposit.Rail.CRYPTO, Deposit.Rail.BANK):
            qs = qs.filter(rail=rail)
        status_value = (request.query_params.get("status") or "").strip()
        if status_value in dict(Deposit.Status.choices):
            qs = qs.filter(status=status_value)
        if request.query_params.get("review") in ("1", "true"):
            qs = qs.filter(status=Deposit.Status.MANUAL_REVIEW)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(reference__icontains=search)
                | Q(user__email__icontains=search)
                | Q(crypto__txid__icontains=search)
                | Q(crypto__submitted_txid__icontains=search)
                | Q(bank__provider_reference__icontains=search)
            )
        return _paginated(request, qs, _deposit_data)


class AdminDepositDetailView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def get(self, request, pk):
        deposit = get_object_or_404(
            Deposit.objects.select_related("user", "asset", "provider", "crypto__network", "bank"),
            pk=pk,
        )
        data = _deposit_data(deposit)
        data["audit"] = [
            {"action": row.action, "details": row.details, "created_at": row.created_at}
            for row in AuditLog.objects.filter(details__icontains=f"deposit={deposit.pk}").order_by("-created_at")[:50]
        ]
        return Response(data)


class AdminChainTransferListView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def get(self, request):
        qs = ChainTransfer.objects.select_related("network", "deposit").order_by("-block_number", "-pk")
        if request.query_params.get("unallocated") in ("1", "true"):
            qs = qs.filter(deposit__isnull=True)
        status_value = (request.query_params.get("status") or "").strip()
        if status_value in dict(ChainTransfer.Status.choices):
            qs = qs.filter(status=status_value)
        txid = (request.query_params.get("txid") or "").strip().lower()
        if txid:
            qs = qs.filter(txid__icontains=txid)
        return _paginated(request, qs, _transfer_data)


class AdminChainTransferAssignView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def post(self, request, pk):
        try:
            deposit_id = int(request.data.get("deposit_id"))
            transfer = assign_transfer(
                pk, deposit_id, actor=request.user, reason=request.data.get("reason", "")
            )
        except (TypeError, ValueError, DepositError) as exc:
            return Response({"detail": str(exc)}, status=400)
        except TronConfigurationError as exc:
            return Response({"detail": str(exc)}, status=409)
        except TronProviderError as exc:
            return Response({"detail": str(exc)}, status=503)
        return Response(_transfer_data(transfer))


class AdminChainTransferApproveView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def post(self, request, pk):
        try:
            allow_mismatch = request.data.get("allow_amount_mismatch") is True or str(
                request.data.get("allow_amount_mismatch", "")
            ).strip().lower() in {"1", "true", "yes", "on"}
            transfer = approve_transfer(
                pk,
                actor=request.user,
                reason=request.data.get("reason", ""),
                allow_amount_mismatch=allow_mismatch,
            )
        except (TypeError, ValueError, DepositError) as exc:
            return Response({"detail": str(exc)}, status=400)
        except TronConfigurationError as exc:
            return Response({"detail": str(exc)}, status=409)
        except TronProviderError as exc:
            return Response({"detail": str(exc)}, status=503)
        data = _transfer_data(transfer)
        data["credited"] = transfer.status == ChainTransfer.Status.CREDITED
        return Response(data)


class AdminChainTransferFlagView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def post(self, request, pk):
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "A review reason is required."}, status=400)
        try:
            with transaction.atomic():
                transfer = get_object_or_404(
                    ChainTransfer.objects.select_for_update().select_related("deposit"),
                    pk=pk,
                )
                if (
                    transfer.status == ChainTransfer.Status.CREDITED
                    or (transfer.deposit_id and transfer.deposit.is_credited)
                ):
                    raise ValueError("Credited transfers cannot be moved back to manual review.")
                transfer.status = ChainTransfer.Status.MANUAL_REVIEW
                transfer.review_reason = reason[:255]
                transfer.updated_at = timezone.now()
                transfer.save(update_fields=["status", "review_reason", "updated_at"])
                if transfer.deposit_id:
                    send_to_manual_review(transfer.deposit, reason=reason, actor=request.user)
                AuditLog.objects.create(
                    user=transfer.deposit.user if transfer.deposit_id else request.user,
                    actor=request.user,
                    action="deposit.crypto_transfer_flagged",
                    details=f"transfer={transfer.id} reason={reason[:500]}",
                )
        except (ValueError, DepositError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_transfer_data(transfer))


class AdminBankApproveView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def post(self, request, pk):
        try:
            amount = _parse_amount(request.data.get("amount"))
            deposit = approve_bank_transfer(
                pk,
                actor=request.user,
                amount=amount,
                currency=request.data.get("currency", ""),
                reference=request.data.get("reference", ""),
                verification_source=request.data.get("verification_source", ""),
                reason=request.data.get("reason", ""),
            )
        except (ValueError, DepositError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_deposit_data(deposit))


class AdminBankRejectView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def post(self, request, pk):
        try:
            deposit = reject_bank_transfer(
                pk, actor=request.user, reason=request.data.get("reason", "")
            )
        except (ValueError, DepositError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_deposit_data(deposit))


class AdminReconciliationView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def get(self, request):
        runs = ReconciliationRun.objects.order_by("-ran_at")[:50]
        discrepancies = ReconciliationDiscrepancy.objects.select_related(
            "deposit", "chain_transfer"
        ).order_by("-created_at")[:100]
        return Response({
            "runs": [
                {
                    "id": run.id, "rail": run.rail, "status": run.status,
                    "provider_total": _money(run.provider_total),
                    "ledger_total": _money(run.ledger_total),
                    "difference": _money(run.difference),
                    "discrepancy_count": run.discrepancy_count,
                    "period_start": run.period_start, "period_end": run.period_end,
                    "notes": run.notes, "ran_at": run.ran_at,
                }
                for run in runs
            ],
            "discrepancies": [
                {
                    "id": item.id, "kind": item.kind, "severity": item.severity,
                    "status": item.status, "details": item.details,
                    "deposit_id": item.deposit_id, "chain_transfer_id": item.chain_transfer_id,
                    "created_at": item.created_at, "resolved_at": item.resolved_at,
                    "resolution": item.resolution,
                }
                for item in discrepancies
            ],
        })

    def post(self, request):
        runs = run_reconciliation(actor=request.user)
        return Response({
            "runs": [
                {
                    "id": run.id, "rail": run.rail, "status": run.status,
                    "difference": _money(run.difference),
                    "discrepancy_count": run.discrepancy_count,
                }
                for run in runs
            ]
        })


class AdminDiscrepancyResolveView(APIView):
    permission_classes = (IsPlatformAdmin,)

    def post(self, request, pk):
        try:
            discrepancy = resolve_discrepancy(
                pk,
                actor=request.user,
                resolution=request.data.get("resolution", ""),
                ignore=request.data.get("ignore") is True,
            )
        except (ValueError, ReconciliationDiscrepancy.DoesNotExist) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({
            "id": discrepancy.id, "status": discrepancy.status,
            "resolved_at": discrepancy.resolved_at,
        })

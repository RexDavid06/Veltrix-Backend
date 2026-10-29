from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from veltrix_app.api.deposits import AdminDepositActionView
from veltrix_app.models import DepositRequest, DemoAccount, Transaction


class CryptoAdminApprovalTests(SimpleTestCase):
    def test_crypto_request_cannot_be_approved_from_admin_flag_or_client_hash(self):
        deposit = SimpleNamespace(
            id=7,
            method=DepositRequest.Method.CRYPTO,
            status=DepositRequest.Status.PENDING,
            amount=Decimal('25'),
            received_amount=None,
            tx_hash='client-supplied-fake-hash',
            reference='VLX-CRYPTO-7',
        )
        locked_query = MagicMock()
        locked_query.get.return_value = deposit
        admin = SimpleNamespace(id=3, pk=3, is_authenticated=True, is_active=True, is_staff=True)
        factory = APIRequestFactory()
        responses = []

        with patch.object(DepositRequest.objects, 'select_for_update', return_value=locked_query), \
                patch('veltrix_app.api.deposits.transaction.atomic', return_value=nullcontext()), \
                patch.object(DemoAccount.objects, 'select_for_update') as lock_account, \
                patch.object(DemoAccount.objects, 'filter') as update_account, \
                patch.object(Transaction.objects, 'create') as create_transaction:
            for verified in (False, True):
                request = factory.post(
                    '/api/admin/deposits/7/action/',
                    {
                        'action': 'approve',
                        'verified': verified,
                        'received_amount': '25',
                        'tx_hash': 'another-client-supplied-hash',
                    },
                    format='json',
                )
                force_authenticate(request, user=admin)
                responses.append(AdminDepositActionView.as_view()(request, pk=7))

        self.assertEqual([response.status_code for response in responses], [503, 503])
        self.assertEqual(deposit.status, DepositRequest.Status.PENDING)
        self.assertIsNone(deposit.received_amount)
        lock_account.assert_not_called()
        update_account.assert_not_called()
        create_transaction.assert_not_called()

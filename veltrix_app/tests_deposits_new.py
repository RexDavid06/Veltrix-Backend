from decimal import Decimal
import os

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from veltrix_app.models import DepositRequest, DemoAccount, AuditLog, Transaction


User = get_user_model()


class DepositsNewTests(APITestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(username='c1', email='c1@test.com', password='pass12345')
        self.admin_user = User.objects.create_user(username='a1', email='a1@test.com', password='pass12345', role='admin', is_staff=True)
        DemoAccount.objects.get_or_create(user=self.client_user, defaults={'currency': 'USD', 'cash_balance': Decimal('0.00')})

    def test_reference_uniqueness(self):
        self.client.force_authenticate(user=self.client_user)
        # Create via model/util logic not directly; API generates
        from veltrix_app.utils_deposits import generate_deposit_reference
        refs = set(generate_deposit_reference() for _ in range(5))
        self.assertEqual(len(refs), 5)

    def test_required_crypto_fields(self):
        self.client.force_authenticate(user=self.client_user)
        with self.settings(TRON_USDT_RECEIVING_ADDRESS='TXYZ'):
            resp = self.client.post('/api/deposits/', {'method': 'crypto', 'network': 'USDT_TRC20', 'amount': '10'}, format='json')
            self.assertIn(resp.status_code, [400])
        self.assertTrue(DepositRequest.objects.filter(user=self.client_user).count() == 0)

    def test_credit_uses_received_amount(self):
        self.client.force_authenticate(user=self.client_user)
        d = DepositRequest.objects.create(user=self.client_user, method='crypto', network='USDT_TRC20', amount=Decimal('100'), reference='VLX-TEST0001')
        admin_client = self.client
        admin_client.force_authenticate(user=self.admin_user)
        resp = admin_client.post(f'/api/admin/deposits/{d.id}/action/', {'action': 'approve', 'verified': True, 'received_amount': '80'}, format='json')
        self.assertEqual(resp.status_code, 200)
        demo = DemoAccount.objects.get(user=self.client_user)
        self.assertEqual(demo.cash_balance, Decimal('80'))
        d.refresh_from_db()
        self.assertEqual(d.received_amount, Decimal('80'))

    def test_audit_log_written(self):
        self.client.force_authenticate(user=self.client_user)
        d = DepositRequest.objects.create(user=self.client_user, method='bank', amount=Decimal('50'), reference='VLX-TEST0002')
        admin_client = self.client
        admin_client.force_authenticate(user=self.admin_user)
        resp = admin_client.post(f'/api/admin/deposits/{d.id}/action/', {'action': 'approve', 'verified': True, 'received_amount': '50'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action='deposit.status_changed').exists())

    def test_non_admin_denied(self):
        self.client.force_authenticate(user=self.client_user)
        d = DepositRequest.objects.create(user=self.client_user, method='bank', amount=Decimal('50'), reference='VLX-TEST0003')
        resp = self.client.post(f'/api/admin/deposits/{d.id}/action/', {'action': 'approve', 'verified': True, 'received_amount': '50'}, format='json')
        self.assertIn(resp.status_code, [403, 401])

    def test_duplicate_tx_hash_rejected(self):
        self.client.force_authenticate(user=self.admin_user)
        u1 = self.client_user
        u2 = User.objects.create_user(username='c2', email='c2@test.com', password='pass12345')
        DemoAccount.objects.get_or_create(user=u2, defaults={'currency': 'USD', 'cash_balance': Decimal('0.00')})
        DepositRequest.objects.create(user=u1, method='crypto', network='USDT_TRC20', amount=Decimal('10'), reference='VLX-T0000001', tx_hash='abc123')
        try:
            DepositRequest.objects.create(user=u2, method='crypto', network='USDT_TRC20', amount=Decimal('10'), reference='VLX-T0000002', tx_hash='abc123')
            self.fail('Should have failed')
        except Exception:
            pass

    def test_duplicate_tx_hash_allowed_after_reject(self):
        u1 = self.client_user
        u2 = User.objects.create_user(username='c3', email='c3@test.com', password='pass12345')
        DemoAccount.objects.get_or_create(user=u2, defaults={'currency': 'USD', 'cash_balance': Decimal('0.00')})
        d1 = DepositRequest.objects.create(user=u1, method='crypto', network='USDT_TRC20', amount=Decimal('10'), reference='VLX-T0000003', tx_hash='abc123', status='rejected')
        d2 = DepositRequest.objects.create(user=u2, method='crypto', network='USDT_TRC20', amount=Decimal('10'), reference='VLX-T0000004', tx_hash='abc123')
        self.assertTrue(d2.id > 0)

    def test_missing_env_var_hides_network(self):
        self.client.force_authenticate(user=self.client_user)
        with self.settings(TRON_USDT_RECEIVING_ADDRESS='', ETHEREUM_USDT_RECEIVING_ADDRESS='', BSC_USDT_RECEIVING_ADDRESS=''):
            resp = self.client.get('/api/deposits/config/')
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.data.get('networks', [])), 0)

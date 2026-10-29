from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate


NETWORK_ADDRESSES = {
    'USDT_ERC20': '0x' + ('a' * 40),
    'USDT_TRC20': 'T' + ('1' * 33),
    'USDT_BEP20': '0x' + ('b' * 40),
}


class DepositNetworkAddressAPITests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(is_authenticated=True, pk=1)

    def request_address(self, network, authenticated=True):
        path = f'/api/deposits/networks/{network}/address/'
        request = self.factory.get(path)
        if authenticated:
            force_authenticate(request, user=self.user)
        return resolve(path).func(request, network=network)

    def test_returns_only_the_requested_network_address(self):
        for network, address in NETWORK_ADDRESSES.items():
            with self.subTest(network=network):
                with patch(
                    'veltrix_app.api.deposits.get_network_address',
                    return_value=address,
                ) as get_address:
                    response = self.request_address(network)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, {'address': address})
                get_address.assert_called_once_with(network)

    def test_rejects_unknown_network_key(self):
        with patch('veltrix_app.api.deposits.get_network_address') as get_address:
            response = self.request_address('USDT_POLYGON')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {'detail': 'Unknown deposit network.'})
        get_address.assert_not_called()

    def test_missing_or_invalid_address_is_unavailable(self):
        for address in (None, '', 'malformed-address'):
            with self.subTest(address_state='missing' if not address else 'invalid'):
                with patch(
                    'veltrix_app.api.deposits.get_network_address',
                    return_value=address,
                ):
                    response = self.request_address('USDT_ERC20')

                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.data,
                    {'detail': 'Network address is currently unavailable.'},
                )

    def test_requires_authentication(self):
        response = self.request_address('USDT_ERC20', authenticated=False)

        self.assertEqual(response.status_code, 401)

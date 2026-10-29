import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from veltrix_app.deposits_config import DEPOSIT_NETWORKS, get_available_networks, get_network_address


class DepositNetworkAddressConfigTests(SimpleTestCase):
    expected_env_vars = {
        'USDT_BEP20': 'BSC_USDT_RECEIVING_ADDRESS',
        'USDT_ERC20': 'ETHEREUM_USDT_RECEIVING_ADDRESS',
        'USDT_TRC20': 'TRON_USDT_RECEIVING_ADDRESS',
    }

    @override_settings(
        BSC_USDT_RECEIVING_ADDRESS='',
        ETHEREUM_USDT_RECEIVING_ADDRESS='',
        TRON_USDT_RECEIVING_ADDRESS='',
        VELTRIX_USDT_BEP20_RECEIVING_ADDRESS='',
        VELTRIX_USDT_ERC20_RECEIVING_ADDRESS='',
        VELTRIX_USDT_TRC20_RECEIVING_ADDRESS='',
    )
    def test_networks_use_render_environment_variable_names(self):
        self.assertEqual(
            {network['key']: network['env_var'] for network in DEPOSIT_NETWORKS},
            self.expected_env_vars,
        )
        env_addresses = {
            'BSC_USDT_RECEIVING_ADDRESS': 'bsc-placeholder',
            'ETHEREUM_USDT_RECEIVING_ADDRESS': 'ethereum-placeholder',
            'TRON_USDT_RECEIVING_ADDRESS': 'tron-placeholder',
        }
        with patch.dict(os.environ, env_addresses):
            for key, env_var in self.expected_env_vars.items():
                self.assertEqual(get_network_address(key), env_addresses[env_var])

    @override_settings(
        BSC_USDT_RECEIVING_ADDRESS='',
        ETHEREUM_USDT_RECEIVING_ADDRESS='',
        TRON_USDT_RECEIVING_ADDRESS='',
        VELTRIX_USDT_BEP20_RECEIVING_ADDRESS='',
        VELTRIX_USDT_ERC20_RECEIVING_ADDRESS='',
        VELTRIX_USDT_TRC20_RECEIVING_ADDRESS='',
    )
    def test_missing_environment_variables_hide_networks(self):
        with patch.dict(os.environ, {}, clear=False):
            for env_var in self.expected_env_vars.values():
                os.environ.pop(env_var, None)
            self.assertTrue(all(get_network_address(key) is None for key in self.expected_env_vars))
            self.assertEqual(get_available_networks(), [])

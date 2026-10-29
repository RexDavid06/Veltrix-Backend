import logging
import os

logger = logging.getLogger(__name__)

DEPOSIT_NETWORKS = [
    {
        'key': 'USDT_TRC20',
        'label': 'USDT (TRC20)',
        'env_var': 'TRON_USDT_RECEIVING_ADDRESS',
    },
    {
        'key': 'USDT_ERC20',
        'label': 'USDT (ERC20)',
        'env_var': 'ETHEREUM_USDT_RECEIVING_ADDRESS',
    },
    {
        'key': 'USDT_BEP20',
        'label': 'USDT (BEP20)',
        'env_var': 'BSC_USDT_RECEIVING_ADDRESS',
    },
]


def get_available_networks():
    networks = []
    for cfg in DEPOSIT_NETWORKS:
        # Resolve the same aliases and Django settings as the deposit API.
        # Render may use the documented VELTRIX_* names rather than the
        # legacy names in env_var.
        addr = get_network_address(cfg['key']) or ''
        if addr.strip():
            networks.append({
                'key': cfg['key'],
                'label': cfg['label'],
                'address': addr.strip(),
            })
        else:
            logger.warning('Missing deposit wallet for %s (%s)', cfg['key'], cfg['env_var'])
    return networks


def get_network_address(key):
    for cfg in DEPOSIT_NETWORKS:
        if cfg['key'] == key:
            from django.conf import settings
            setting_names = {
                'USDT_TRC20': ('TRON_USDT_RECEIVING_ADDRESS', 'VELTRIX_USDT_TRC20_RECEIVING_ADDRESS'),
                'USDT_ERC20': ('ETHEREUM_USDT_RECEIVING_ADDRESS', 'VELTRIX_USDT_ERC20_RECEIVING_ADDRESS'),
                'USDT_BEP20': ('BSC_USDT_RECEIVING_ADDRESS', 'VELTRIX_USDT_BEP20_RECEIVING_ADDRESS'),
            }
            addr = os.environ.get(cfg['env_var'], '')
            for name in setting_names.get(key, ()):
                addr = addr or getattr(settings, name, '')
            return addr.strip() if addr.strip() else None
    return None

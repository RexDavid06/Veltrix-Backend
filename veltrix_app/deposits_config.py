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
        addr = os.environ.get(cfg['env_var'], '') or ''
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
            addr = os.environ.get(cfg['env_var'], '') or getattr(__import__('django.conf').conf.settings, cfg['env_var'], '') or ''
            return addr.strip() if addr.strip() else None
    return None

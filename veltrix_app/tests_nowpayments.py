import hashlib
import hmac
import json
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle

from veltrix_app.models import Asset, CryptoDeposit, Deposit, DepositProvider, KycProfile, Network, ProviderEvent
from veltrix_app.services.kyc import approve_profile, ensure_profile
from veltrix_app.services import ledger


User = get_user_model()

import random
import string

from veltrix_app.models import DepositRequest


def generate_deposit_reference():
    for _ in range(50):
        ref = 'VLX-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if not DepositRequest.objects.filter(reference=ref).exists():
            return ref
    raise RuntimeError('Could not generate unique deposit reference')

from decimal import Decimal

from django.db import migrations


def fresh_start(apps, schema_editor):
    """Reset every pre-existing account so it matches a brand-new user.

    Balances go to 0.00 and all demo transaction/position history is removed.
    This is intentionally destructive for existing demo data; reverse is a noop.
    """
    DemoAccount = apps.get_model("veltrix_app", "DemoAccount")
    Position = apps.get_model("veltrix_app", "Position")
    Transaction = apps.get_model("veltrix_app", "Transaction")

    DemoAccount.objects.update(cash_balance=Decimal("0.00"))
    Position.objects.all().delete()
    Transaction.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("veltrix_app", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(fresh_start, migrations.RunPython.noop),
    ]

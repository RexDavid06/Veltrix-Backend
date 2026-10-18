from rest_framework import serializers

from veltrix_app.models import AuditLog, Instrument, Transaction, User


class AdminUserSerializer(serializers.ModelSerializer):
    """Read-only projection of a user for the admin dashboard.

    Only whitelisted fields are ever serialized: password hashes, tokens and
    other credentials can never appear in an admin response.
    """

    cash_balance = serializers.SerializerMethodField()
    transaction_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "first_name",
            "last_name",
            "role",
            "is_verified",
            "is_active",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
            "cash_balance",
            "transaction_count",
        )
        read_only_fields = fields

    def get_cash_balance(self, obj):
        account = getattr(obj, "demo_account", None)
        return str(account.cash_balance) if account else "0.00"


class AdminUserDetailSerializer(AdminUserSerializer):
    positions = serializers.SerializerMethodField()
    recent_transactions = serializers.SerializerMethodField()

    class Meta(AdminUserSerializer.Meta):
        fields = AdminUserSerializer.Meta.fields + ("positions", "recent_transactions")
        read_only_fields = fields

    def get_positions(self, obj):
        return [
            {
                "symbol": p.instrument.symbol,
                "name": p.instrument.name,
                "quantity": str(p.quantity),
                "avg_price": str(p.avg_price),
                "market_value": str(p.market_value),
            }
            for p in obj.positions.select_related("instrument")
        ]

    def get_recent_transactions(self, obj):
        return AdminTransactionSerializer(
            obj.transactions.order_by("-created_at")[:20], many=True
        ).data


class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """Writable projection: only safe account-state flags may be changed.

    role, staff/superuser flags, email and username are deliberately absent so
    an admin API call can never escalate or rewrite identities.
    """

    class Meta:
        model = User
        fields = ("is_active", "is_verified")


class AdminTransactionSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = (
            "id",
            "user_id",
            "user_email",
            "user_name",
            "symbol",
            "side",
            "quantity",
            "execution_price",
            "amount",
            "status",
            "note",
            "created_at",
        )
        read_only_fields = fields

    def get_user_name(self, obj):
        return obj.user.get_full_name() or obj.user.username


class AdminInstrumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instrument
        fields = (
            "id",
            "symbol",
            "name",
            "asset_class",
            "currency",
            "price",
            "change_percent",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "symbol", "currency", "price", "change_percent", "created_at")


class AdminInstrumentUpdateSerializer(serializers.ModelSerializer):
    """Safe management surface: only listing visibility can be changed.

    Prices are excluded because they drive order execution; name and asset
    class are display-only but also excluded to keep the surface minimal.
    """

    class Meta:
        model = Instrument
        fields = ("is_active",)


class AdminAuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()
    target_email = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ("id", "actor", "actor_email", "actor_name", "user", "target_email", "action", "details", "created_at")
        read_only_fields = fields

    def get_actor_email(self, obj):
        return obj.actor.email if obj.actor else None

    def get_actor_name(self, obj):
        if not obj.actor:
            return None
        return obj.actor.get_full_name() or obj.actor.username

    def get_target_email(self, obj):
        return obj.user.email if obj.user else None

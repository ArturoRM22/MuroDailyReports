from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import Branch, User, BranchSettings, DailyReport

def settings_for(branch, report_date):
    """Latest settings row effective on report_date (None if no row exists)."""
    return BranchSettings.objects.filter(
        branch=branch, effective_from__lte=report_date,
    ).order_by("-effective_from").first()

class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "name", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class UserSerializer(serializers.ModelSerializer):
    # write_only -> accepted as input, never serialized into a response.
    # required=False so PATCH can omit it; create() is where it's enforced.
    password = serializers.CharField(
        write_only=True,
        required=False,
        style={"input_type": "password"},
    )

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name", "email",
            "role", "branch", "is_active", "password",
            "date_joined", "last_login",
        ]
        read_only_fields = ["id", "date_joined", "last_login"]

    def validate_password(self, value):
        # DRF calls this because the field is named `password`. Inside the
        # method `validate_password` resolves to the django import, not to
        # this method -- method names live on the class, not in module scope.
        validate_password(value, user=self.instance)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            raise serializers.ValidationError({"password": "This field is required."})
        user = User(**validated_data)
        # At field-validation time self.instance was None, so the "too
        # similar to username" check was skipped. The username exists now.
        validate_password(password, user=user)
        user.set_password(password)   # hashes. make_password() only RETURNS one.
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        if password:
            instance.set_password(password)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance


class SelfSerializer(UserSerializer):
    """What an employee may see and change about their own record."""

    class Meta(UserSerializer.Meta):
        fields = [
            "id", "username", "first_name", "last_name", "email",
            "role", "branch", "password",
        ]
        # role/branch/username stay READABLE -- React needs to know if you're
        # an owner to render the right UI -- but they cannot be written.
        read_only_fields = ["id", "username", "role", "branch"]

class BranchSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = BranchSettings
        fields = [
            "id", "branch", "effective_from",
            "nixtamal_can_kg", "corn_sack_kg", "corn_yield",
            "maseca_sack_kg", "maseca_yield",
            "harina_sack_kg", "harina_yield",
            "default_tortilla_price", "default_taco_rate",
            "default_restaurant_rate", "default_masa_price",
            "aluminio_rate", "kilo25_rate", "masa_out_rate"
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        """
        Instantiates an uncommitted model object and runs model-level .clean() 
        so Django's validation errors are caught and returned as DRF 400 errors.
        """
        # Create a temporary instance with the incoming data
        instance = BranchSettings(**attrs)
        try:
            instance.clean()
        except DjangoValidationError as e:
            # Convert Django validation error into a DRF ValidationError
            if hasattr(e, 'message_dict'):
                raise serializers.ValidationError(e.message_dict)
            raise serializers.ValidationError(e.messages)
        
        return attrs

class DailyReportSerializer(serializers.ModelSerializer):
    PRICE_DEFAULTS = {
        "tortilla_price": "default_tortilla_price",
        "taco_rate": "default_taco_rate",
        "restaurant_rate": "default_restaurant_rate",
        "masa_price": "default_masa_price",
    }

    class Meta:
        model = DailyReport
        fields = "__all__"

        read_only_fields = [
            "id",
            "branch_settings",
            "created_by",
            "created_at",
            "updated_at",
            "closed_at",

            # Calculated fields
            "production_kg",
            "expected_sales",
            "actual_sales",
            "difference",
        ]

    def create(self, validated_data):
        branch = validated_data["branch"]
        report_date = validated_data["report_date"]
    
        # The settings row is never chosen by the client: it is the row
        # in effect on that date for that branch.
        row = settings_for(branch, report_date)
        if row is None:
            raise serializers.ValidationError(
                {"report_date": "No existe una configuración vigente para esta fecha."}
            )
    
        validated_data["branch_settings"] = row
        for price_field, default_field in self.PRICE_DEFAULTS.items():
            if price_field not in validated_data:          # client sent it -> leave as-is
                validated_data[price_field] = getattr(row, default_field)
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        # Branch is immutable: settings resolution is per-branch, so a branch
        # change would silently mix settings rows from two branches.
        if validated_data.get("branch", instance.branch) != instance.branch:
            raise serializers.ValidationError(
                {"branch": "La sucursal no se puede cambiar."}
            )

        # Re-resolve settings only if the day moved.
        new_date = validated_data.get("report_date", instance.report_date)
        if new_date != instance.report_date:
            row = settings_for(instance.branch, new_date)
            if row is None:
                raise serializers.ValidationError(
                    {"report_date": "No existe una configuración vigente para esta fecha."}
                )
            instance.branch_settings = row

        return super().update(instance, validated_data)


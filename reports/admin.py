from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Branch, BranchSettings, DailyReport, Expense, Order, User


def is_branch_employee(user):
    """Employees (non-superuser, role=employee) only see their own branch."""
    return (not user.is_superuser) and getattr(user, "role", None) == User.Role.EMPLOYEE


# --------------------------------------------------------------------------
# Users and branches
# --------------------------------------------------------------------------
@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Tortillería", {"fields": ("role", "branch")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Tortillería", {"fields": ("role", "branch")}),)
    list_display = ("username", "role", "branch", "is_active")
    list_filter = ("role", "branch", "is_active")


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@admin.register(BranchSettings)
class BranchSettingsAdmin(admin.ModelAdmin):
    list_display = ("branch", "effective_from", "corn_yield", "maseca_yield", "harina_yield")
    list_filter = ("branch",)

    def get_readonly_fields(self, request, obj=None):
        """Locked rows render as text, so the guard is visible before it fires."""
        readonly = list(super().get_readonly_fields(request, obj))
        if obj and obj.reports.exists():
            readonly += ["branch", "effective_from", *BranchSettings.PROTECTED_FIELDS]
        return readonly

    fieldsets = (
        (None, {"fields": ("branch", "effective_from")}),
        ("Maíz / nixtamal", {"fields": ("nixtamal_can_kg", "corn_sack_kg", "corn_yield")}),
        ("Maseca", {"fields": ("maseca_sack_kg", "maseca_yield")}),
        ("Harina", {"fields": ("harina_sack_kg", "harina_yield")}),
        ("Precios por defecto", {"fields": (
            "default_tortilla_price", "default_taco_rate",
            "default_restaurant_rate", "default_masa_price",
        )}),
        ("Otras tarifas (por confirmar)", {"fields": ("aluminio_rate", "kilo25_rate", "masa_out_rate")}),
    )


# --------------------------------------------------------------------------
# Daily report
# --------------------------------------------------------------------------
class ExpenseInline(admin.TabularInline):
    model = Expense
    extra = 3


class OrderInline(admin.TabularInline):
    model = Order
    extra = 0


@admin.register(DailyReport)
class DailyReportAdmin(admin.ModelAdmin):
    list_display = ("branch", "report_date", "status", "production_kg", "difference")
    list_filter = ("branch", "status")
    date_hierarchy = "report_date"
    inlines = [ExpenseInline, OrderInline]
    readonly_fields = (
        "production_kg", "expected_sales", "actual_sales", "difference",
        "created_by", "created_at", "updated_at", "closed_at",
    )

    # Sections follow the layout of the Cruz Verde sheet
    fieldsets = (
        (None, {"fields": ("branch", "report_date", "branch_settings", "status")}),
        ("Precios del día", {"fields": ("tortilla_price", "taco_rate", "restaurant_rate", "masa_price")}),
        ("Nixtamal", {"fields": (
            "nix_cans_yesterday", "corn_sacks_yesterday", "nix_cans_leftover",
            "nix_cans_out", "nix_cans_in", "nix_cans_hot", "corn_sacks_tomorrow",
        )}),
        ("Inventario de maíz", {"fields": (
            "corn_inv_yesterday", "corn_inv_in", "corn_inv_cruz", "corn_inv_muro",
        )}),
        ("Maseca (bultos enteros + kg incompletos)", {"fields": (
            ("maseca_yesterday_sacks", "maseca_yesterday_loose_kg"),
            ("maseca_in_sacks", "maseca_in_loose_kg"),
            ("maseca_out_sacks", "maseca_out_loose_kg"),
            ("maseca_left_sacks", "maseca_left_loose_kg"),
        )}),
        ("Harina (bultos enteros + kg incompletos)", {"fields": (
            ("harina_yesterday_sacks", "harina_yesterday_loose_kg"),
            ("harina_left_sacks", "harina_left_loose_kg"),
        )}),
        ("Entradas", {"fields": (
            "in_aluminio_kg", "in_tortilla_kg", "in_masa_kg", "masa_sold_kg",
            "customer_payments", "kilo25_kg",
        )}),
        ("Salidas", {"fields": (
            "waste_masa_kg", "waste_tortilla_kg", "customers_owe", "out_masa_kg",
            "out_tortilla_kg", "unsold_kg", "other_kg", "taco_kg", "restaurant_kg",
        )}),
        ("Cierre", {"fields": ("routes_kg", "taco_sales", "counter_sales")}),
        ("Resultados (calculados)", {"fields": (
            "production_kg", "expected_sales", "actual_sales", "difference",
        )}),
        ("Otros", {"classes": ("collapse",), "fields": (
            "extra", "created_by", "created_at", "updated_at", "closed_at",
        )}),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if is_branch_employee(request.user):
            return qs.filter(branch=request.user.branch)
        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if is_branch_employee(request.user):
            if db_field.name == "branch":
                kwargs["queryset"] = Branch.objects.filter(pk=request.user.branch_id)
            elif db_field.name == "branch_settings":
                kwargs["queryset"] = BranchSettings.objects.filter(branch=request.user.branch)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
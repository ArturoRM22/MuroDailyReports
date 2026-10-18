"""
Tortillería app - database models (Cruz Verde format only).

Tables: Branch, User, BranchSettings, DailyReport, Expense, Order.
(Customer ledger is intentionally left for later.)

Every DailyReport field notes the Excel label it comes from.

Conventions
- Quantities (kg, sacks, cans)  -> Decimal with 3 decimals
- Money (pesos)                 -> Decimal with 2 decimals
- Blank cell in Excel           -> 0 in the database
- "sacks + loose kg" pairs      -> Excel's "Bultos enteros" + "Bultos incompletos (kg)"
"""
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


def qty(**kwargs):
    """Quantity: kg, sacks, cans."""
    return models.DecimalField(max_digits=10, decimal_places=3, default=0, **kwargs)


def money(**kwargs):
    """Pesos."""
    return models.DecimalField(max_digits=12, decimal_places=2, default=0, **kwargs)


# --------------------------------------------------------------------------
# Branches and users
# --------------------------------------------------------------------------
class Branch(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class User(AbstractUser):
    """
    Custom user. Must exist BEFORE the first migration
    (settings.AUTH_USER_MODEL = "reports.User").
    """

    class Role(models.TextChoices):
        EMPLOYEE = "employee", "Empleado"
        OWNER = "owner", "Dueño"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.EMPLOYEE)
    # Employees belong to one branch. NULL = owner who can see every branch.
    branch = models.ForeignKey(
        Branch, null=True, blank=True, on_delete=models.PROTECT, related_name="users"
    )


# --------------------------------------------------------------------------
# Settings: the numbers that were buried inside the Excel formulas
# --------------------------------------------------------------------------
class BranchSettings(models.Model):
    """
    Versioned by date. Never edit a row that has reports attached:
    create a new row with a later `effective_from` instead, so old days keep
    the yields and rates they were calculated with.
    """

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="settings_history")
    effective_from = models.DateField()

    # Nixtamal / corn
    nixtamal_can_kg = models.DecimalField(max_digits=8, decimal_places=3, default=10.5)  # C8: B8*10.5
    corn_sack_kg = models.DecimalField(max_digits=8, decimal_places=3, default=50)        # C9: B9*50
    corn_yield = models.DecimalField(max_digits=8, decimal_places=3, default=1.53)        # C15: kg tortilla per kg corn

    # Maseca
    maseca_sack_kg = models.DecimalField(max_digits=8, decimal_places=3, default=20)      # H5: G5/20
    maseca_yield = models.DecimalField(max_digits=8, decimal_places=3, default=39)        # I10: sacks * 39

    # Harina
    harina_sack_kg = models.DecimalField(max_digits=8, decimal_places=3, default=44)      # H13: G13/44
    harina_yield = models.DecimalField(max_digits=8, decimal_places=3, default=88)        # I16: sacks * 88

    # Default prices (copied into each new daily report, where they can be changed)
    default_tortilla_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    default_taco_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    default_restaurant_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    default_masa_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # Other rates that were hardcoded in formulas. Rename once their meaning is confirmed.
    aluminio_rate = models.DecimalField(max_digits=8, decimal_places=2, default=2)        # C21: "Kilos en aluminio" *2
    kilo25_rate = models.DecimalField(max_digits=8, decimal_places=2, default=5)          # C26: "Kilo a 25" *5
    masa_out_rate = models.DecimalField(max_digits=8, decimal_places=2, default=21)       # C36: "Salida kilos de masa" *21

    # Fields whose whole purpose is to reproduce what past days were calculated
    # with. Frozen once reports point at this row; fix a mistake by adding a new
    # row with a later `effective_from` instead.
    PROTECTED_FIELDS = (
        "nixtamal_can_kg", "corn_sack_kg", "corn_yield",
        "maseca_sack_kg", "maseca_yield",
        "harina_sack_kg", "harina_yield",
        "default_tortilla_price", "default_taco_rate",
        "default_restaurant_rate", "default_masa_price",
        "aluminio_rate", "kilo25_rate", "masa_out_rate",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "effective_from"], name="uniq_settings_branch_date"),
        ]
        ordering = ["branch", "-effective_from"]
        verbose_name_plural = "branch settings"

    def __str__(self):
        return f"{self.branch} (desde {self.effective_from})"

    # ---- Freeze-once-used --------------------------------------------------
    def _locked_changes(self):
        """
        Protected fields that differ from what is currently stored in the
        database. Comparing against the DB (not the in-memory object) is what
        lets an unchanged row be re-saved without tripping the guard.
        """
        if self._state.adding or self.pk is None:
            return []
        stored = type(self).objects.filter(pk=self.pk).first()
        if stored is None or not stored.reports.exists():
            return []
        changed = []
        for field in self._meta.fields:
            if field.name not in self.PROTECTED_FIELDS:
                continue
            # Coerce both sides to the field's Python type first. A plain
            # `obj.corn_yield = 1.7` leaves a float while the DB hands back a
            # Decimal, and Decimal('1.700') != 1.7 -- to_python() normalises
            # them so only real edits register.
            was = field.to_python(getattr(stored, field.name))
            now = field.to_python(getattr(self, field.name))
            if was != now:
                changed.append(field.name)
        return changed

    def _assert_not_locked(self):
        changed = self._locked_changes()
        if changed:
            raise ValidationError(
                "No se pueden modificar %s: esta configuración ya tiene "
                "reportes asociados. Cree una nueva fila con una fecha "
                "posterior." % ", ".join(changed)
            )

    def clean(self):
        """Caught by ModelForm, so admin shows a form error instead of a 500."""
        self._assert_not_locked()
        return super().clean()

    def save(self, *args, **kwargs):
        """The actual guarantee — covers admin, shell, and any future API."""
        self._assert_not_locked()
        super().save(*args, **kwargs)


# --------------------------------------------------------------------------
# Daily report: one row per branch per day
# --------------------------------------------------------------------------
class DailyReport(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Abierto"
        CLOSED = "closed", "Cerrado"

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="reports")
    report_date = models.DateField()                         # Fecha
    branch_settings = models.ForeignKey(                     # factors used for this day
        BranchSettings, on_delete=models.PROTECT, related_name="reports"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="reports",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # ---- Prices of the day (they change from day to day) ------------------
    tortilla_price = money()      # Precio kilo de tortillas (B1)
    taco_rate = money()           # Taco                     (B2)
    restaurant_rate = money()     # Restaurante              (B3)
    masa_price = money()          # Masa                     (B4)

    # ---- Nixtamal / corn --------------------------------------------------
    nix_cans_yesterday = qty()        # Botes que sobraron ayer
    corn_sacks_yesterday = qty()      # Bultos de maíz de ayer
    nix_cans_leftover = qty()         # Nixtamal sobró
    nix_cans_out = qty()              # Salida nixtamal
    nix_cans_in = qty()               # Entrada nixtamal
    nix_cans_hot = qty()              # Nixtamal caliente
    corn_sacks_tomorrow = qty()       # Bultos de maíz para mañana

    # ---- Corn inventory (sacks) -------------------------------------------
    corn_inv_yesterday = qty()        # Inventario de maíz: Maíz de ayer
    corn_inv_in = qty()               # Entrada de maíz
    corn_inv_cruz = qty()             # Cruz   (meaning to confirm)
    corn_inv_muro = qty()             # Muro   (meaning to confirm)

    # ---- Maseca (each: whole sacks + loose kg) ----------------------------
    maseca_yesterday_sacks = qty()    # Sobró ayer
    maseca_yesterday_loose_kg = qty()
    maseca_in_sacks = qty()           # Entrada de bultos maseca
    maseca_in_loose_kg = qty()
    maseca_out_sacks = qty()          # Salida de maseca de hoy
    maseca_out_loose_kg = qty()
    maseca_left_sacks = qty()         # Bultos que sobraron
    maseca_left_loose_kg = qty()

    # ---- Harina (whole sacks + loose kg) ----------------------------------
    harina_yesterday_sacks = qty()    # Sobró ayer
    harina_yesterday_loose_kg = qty()
    harina_left_sacks = qty()         # Sobró hoy
    harina_left_loose_kg = qty()

    # ---- Entradas ----------------------------------------------------------
    in_aluminio_kg = qty()            # Kilos en aluminio
    in_tortilla_kg = qty()            # Entrada kilos de tortillas
    in_masa_kg = qty()                # Entrada kilos de masa
    masa_sold_kg = qty()              # Kilos de masa vendida
    customer_payments = money()       # Pagos pendientes de clientes (pesos received)
    kilo25_kg = qty()                 # Kilo a 25

    # ---- Salidas -----------------------------------------------------------
    waste_masa_kg = qty()             # Desperdicio de masa
    waste_tortilla_kg = qty()         # Desperdicio de tortillas
    customers_owe = money()           # Clientes que deben (pesos)
    out_masa_kg = qty()               # Salida kilos de masa
    out_tortilla_kg = qty()           # Salida kilos de tortillas
    unsold_kg = qty()                 # Kilos que no se vendieron
    other_kg = qty()                  # Otros kilos
    taco_kg = qty()                   # Taco a 18
    restaurant_kg = qty()             # Kilos a restaurantes

    # ---- Closing figures typed by the employee -----------------------------
    routes_kg = qty()                 # Rutas
    taco_sales = money()              # Taco (pesos)
    counter_sales = money()           # Venta del mostrador (actual money counted)

    # ---- Results (filled by the calculation module, not typed) -------------
    production_kg = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    expected_sales = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_sales = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    difference = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Escape hatch for new or branch-specific fields, before they deserve a column
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["branch", "report_date"], name="uniq_report_branch_date"),
        ]
        ordering = ["-report_date"]

    def __str__(self):
        return f"{self.branch} - {self.report_date}"


# --------------------------------------------------------------------------
# Expenses and orders: lists inside a day
# --------------------------------------------------------------------------
class Expense(models.Model):
    """One row per expense. 'Otros' can appear as many times as needed."""

    class Category(models.TextChoices):
        LIMPIEZA = "limpieza", "Limpieza"
        GASOLINA = "gasolina", "Gasolina"
        ACEITE = "aceite", "Aceite"
        AGUA = "agua", "Agua"
        BOLSA = "bolsa", "Bolsa"
        HARINA = "harina", "Harina"
        OTROS = "otros", "Otros"

    report = models.ForeignKey(DailyReport, on_delete=models.CASCADE, related_name="expenses")
    category = models.CharField(max_length=20, choices=Category.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.category}: {self.amount}"


class Order(models.Model):                                   # "Pedidos"
    report = models.ForeignKey(DailyReport, on_delete=models.CASCADE, related_name="orders")
    customer_name = models.CharField(max_length=150)         # Nombre
    delivery_date = models.DateField(null=True, blank=True)  # Fecha
    kg = models.DecimalField(max_digits=10, decimal_places=3, default=0)  # Kilos
    address = models.CharField(max_length=250, blank=True)   # Domicilio
    delivery_time = models.CharField(max_length=50, blank=True)  # Horario
    cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # Costo

    def __str__(self):
        return f"{self.customer_name} ({self.kg} kg)"
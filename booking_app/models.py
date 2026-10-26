
import datetime
import random
import string

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


# ---------------------------------------------------------------------------
# Accounts / roles
# Research alignment: role-based access control (proposal §3.4.2 iv).
# ---------------------------------------------------------------------------
class Role(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    STAFF = "staff", "Staff / Booking Officer"
    MANAGER = "manager", "Manager / Administrator"


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER)
    phone_number = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_customer(self) -> bool:
        return self.role == Role.CUSTOMER

    def is_booking_staff(self) -> bool:
        return self.role == Role.STAFF

    def is_manager(self) -> bool:
        return self.role == Role.MANAGER

    def __str__(self) -> str:
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


class CustomerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="customer_profile")
    id_number = models.CharField(max_length=30, blank=True)
    preferences = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"CustomerProfile<{self.user.username}>"


class StaffProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="staff_profile")
    department = models.CharField(max_length=100, blank=True)
    permission_scope = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"StaffProfile<{self.user.username}>"


# ---------------------------------------------------------------------------
# Listings: Destination -> Property -> Resource
# Kept as a 3-level chain to avoid duplicating location data (§1.2/§2.2.1).
# ---------------------------------------------------------------------------
class Destination(models.Model):
    name = models.CharField(max_length=150)
    country = models.CharField(max_length=100, default="Kenya")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Property(models.Model):
    destination = models.ForeignKey(Destination, on_delete=models.PROTECT, related_name="properties")
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Properties"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.destination.name})"


class ResourceType(models.TextChoices):
    ROOM = "room", "Room"
    TOUR = "tour", "Tour"
    SERVICE = "service", "Service"


class Resource(models.Model):
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="resources")
    resource_type = models.CharField(max_length=20, choices=ResourceType.choices)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    capacity = models.PositiveIntegerField(default=1)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(
        default=True,
        help_text="An inactive resource cannot accept new bookings — checked by the Booking Integrity Engine.",
    )
    is_featured = models.BooleanField(
        default=False,
        help_text="Shown in the homepage's featured listings section (brief §17).",
    )
    image = models.ImageField(
        upload_to="resources/%Y/%m/",
        blank=True,
        null=True,
        help_text="Main photo shown on listings and staff dashboard.",
    )

    class Meta:
        ordering = ["property", "name"]

    def __str__(self) -> str:
        return f"{self.name} — {self.property.name}"


# ---------------------------------------------------------------------------
# Scheduling
# Research alignment: §12 — schedule changes must never leave outdated
# data silently active.
# ---------------------------------------------------------------------------
class Schedule(models.Model):
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="schedules")
    start_date = models.DateField()
    end_date = models.DateField()
    opens_at = models.TimeField(null=True, blank=True)
    closes_at = models.TimeField(null=True, blank=True)
    capacity_override = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="schedules_created")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["resource", "start_date"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(start_date__lte=models.F("end_date")),
                name="schedule_start_before_end",
            )
        ]

    def __str__(self) -> str:
        return f"{self.resource.name}: {self.start_date} → {self.end_date}"


# ---------------------------------------------------------------------------
# Payments (M-Pesa) — mandatory: a Booking only reaches CONFIRMED through
# a matching confirmed Payment (project decision).
# ---------------------------------------------------------------------------
class PaymentStatus(models.TextChoices):
    INITIATED = "initiated", "Initiated"
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"
    REFUNDED = "refunded", "Refunded"


class PaymentMethod(models.TextChoices):
    MPESA = "mpesa", "M-Pesa"


class Payment(models.Model):
    customer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="KES")
    method = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.MPESA)
    status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.INITIATED)

    mpesa_phone_number = models.CharField(max_length=15, blank=True)
    mpesa_checkout_request_id = models.CharField(max_length=100, blank=True, unique=True, null=True)
    mpesa_merchant_request_id = models.CharField(max_length=100, blank=True)
    mpesa_receipt_number = models.CharField(max_length=50, blank=True)
    raw_callback_payload = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Payment#{self.pk} — {self.amount} {self.currency} ({self.get_status_display()})"


# ---------------------------------------------------------------------------
# Bookings — the centerpiece. UNIQUE constraint is the DB-level
# duplicate/conflict defense (§4/§5); never bypass BookingIntegrityEngine
# (see engine.py) to create/modify a Booking directly.
# ---------------------------------------------------------------------------
class BookingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAYMENT_PENDING = "payment_pending", "Payment Pending"
    CONFIRMED = "confirmed", "Confirmed"
    CANCELLED = "cancelled", "Cancelled"
    COMPLETED = "completed", "Completed"
    CONFLICT = "conflict", "Conflict"
    EXPIRED = "expired", "Expired"


def generate_reference() -> str:
    """TRV-<year>-<5 char alphanumeric>, e.g. TRV-2026-8F42K."""
    year = datetime.date.today().year
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"TRV-{year}-{suffix}"


class Booking(models.Model):
    reference = models.CharField(max_length=20, unique=True, db_index=True, editable=False)
    customer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="bookings")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="bookings")
    schedule = models.ForeignKey(Schedule, on_delete=models.PROTECT, related_name="bookings", null=True, blank=True)

    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    guests = models.PositiveIntegerField(default=1)

    status = models.CharField(max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING)

    payment = models.OneToOneField(
        Payment, on_delete=models.PROTECT, related_name="booking", null=True, blank=True
    )

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="bookings_created",
        help_text="The customer themself, or the staff member who booked on their behalf.",
    )
    payment_hold_expires_at = models.DateTimeField(null=True, blank=True)

    version = models.PositiveIntegerField(default=0, help_text="Optimistic-locking counter.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "start_datetime", "end_datetime"],
                condition=models.Q(status__in=["pending", "payment_pending", "confirmed"]),
                name="unique_active_booking_per_resource_slot",
            ),
            models.CheckConstraint(
                condition=models.Q(start_datetime__lt=models.F("end_datetime")),
                name="booking_start_before_end",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "start_datetime"]),
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = generate_reference()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.reference} — {self.resource.name} ({self.get_status_display()})"


class BookingStatusHistory(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="status_history")
    previous_status = models.CharField(max_length=20, choices=BookingStatus.choices, blank=True)
    new_status = models.CharField(max_length=20, choices=BookingStatus.choices)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    note = models.CharField(max_length=255, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp"]
        verbose_name_plural = "Booking status history"

    def __str__(self) -> str:
        return f"{self.booking.reference}: {self.previous_status} → {self.new_status}"


# ---------------------------------------------------------------------------
# Audit trail — append-only, tamper-resistant through admin.py permissions
# (§8). Every state change in engine.py ends with a call to record().
# ---------------------------------------------------------------------------
class AuditLog(models.Model):
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="audit_entries")
    action = models.CharField(max_length=100)
    target_model = models.CharField(max_length=100)
    target_id = models.CharField(max_length=50)
    previous_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["target_model", "target_id"]),
            models.Index(fields=["actor", "timestamp"]),
        ]

    def __str__(self) -> str:
        return f"[{self.timestamp}] {self.actor}: {self.action} on {self.target_model}#{self.target_id}"


def record(actor, action: str, target, previous_value=None, new_value=None, ip_address=None) -> "AuditLog":
    """Single write path for audit entries — call this, never AuditLog.objects.create() directly."""
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        target_model=target.__class__.__name__,
        target_id=str(target.pk),
        previous_value=previous_value,
        new_value=new_value,
        ip_address=ip_address,
    )


# ---------------------------------------------------------------------------
# Notifications / system alerts
# ---------------------------------------------------------------------------
class NotificationType(models.TextChoices):
    BOOKING_CREATED = "booking_created", "Booking Created"
    BOOKING_CONFIRMED = "booking_confirmed", "Booking Confirmed"
    BOOKING_MODIFIED = "booking_modified", "Booking Modified"
    BOOKING_CANCELLED = "booking_cancelled", "Booking Cancelled"
    BOOKING_CONFLICT = "booking_conflict", "Booking Conflict"
    SCHEDULE_CHANGE = "schedule_change", "Schedule Change"
    SYSTEM_ALERT = "system_alert", "System Alert"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    type = models.CharField(max_length=30, choices=NotificationType.choices)
    payload = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_type_display()} → {self.user}"


class SystemAlert(models.Model):
    message = models.CharField(max_length=255)
    severity = models.CharField(
        max_length=10,
        choices=[("info", "Info"), ("warning", "Warning"), ("critical", "Critical")],
        default="warning",
    )
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.message}"
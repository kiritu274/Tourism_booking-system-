from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import (
    User, CustomerProfile, StaffProfile,
    Destination, Property, Resource,
    Schedule,
    Payment,
    Booking, BookingStatusHistory,
    AuditLog,
    Notification, SystemAlert,
)


class UserAdmin(BaseUserAdmin):
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Role", {"fields": ("role", "phone_number")}),
    )
    list_display = ("username", "email", "role", "is_staff", "is_active")
    list_filter = BaseUserAdmin.list_filter + ("role",)
    # Role changes made here are still allowed (a manager legitimately
    # needs to promote/demote staff) but are now automatically
    # audit-logged by signals.audit_user_role_changes (Phase 9).


admin.site.register(User, UserAdmin)
admin.site.register(CustomerProfile)
admin.site.register(StaffProfile)

admin.site.register(Destination)
admin.site.register(Property)


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("name", "property", "resource_type", "base_price", "capacity", "is_active", "is_featured")
    list_filter = ("resource_type", "is_active", "is_featured")
    list_editable = ("is_active", "is_featured")
    search_fields = ("name", "property__name")


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("resource", "start_date", "end_date", "is_active")
    list_filter = ("is_active",)
    # Schedules ARE meant to be edited directly here (brief §12 — there's
    # no dedicated "schedule engine" the way there is for bookings).
    # Every save is audit-logged automatically by
    # signals.audit_schedule_changes.


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """
    Payment records are read-only in admin — a Payment's status only
    ever changes through the M-Pesa callback (mpesa_callback_view) or
    the engine's confirm_payment/fail_payment, both of which keep the
    linked Booking in sync. Editing a Payment directly here would
    desync it from its Booking's status, silently reintroducing the
    "manual reconciliation" problem the research proposal identifies as
    a failure mode, not fix it.
    """
    list_display = ("id", "customer", "amount", "status", "mpesa_receipt_number", "created_at")
    list_filter = ("status", "method")
    search_fields = ("mpesa_checkout_request_id", "mpesa_receipt_number", "customer__username")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


class BookingStatusHistoryInline(admin.TabularInline):
    model = BookingStatusHistory
    extra = 0
    readonly_fields = ("previous_status", "new_status", "changed_by", "note", "timestamp")
    can_delete = False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    """
    Read-only by design (Phase 9 lockdown, brief §8/§22): a Booking may
    only be created or change status through BookingIntegrityEngine,
    which does conflict detection, transactions, and detailed audit
    logging that a bare admin save cannot replicate. This applies to
    every admin user including superusers — there is no "trusted user"
    exception, because the point is that the *path* (not the person)
    determines whether a change is validated. Managers still use this
    page to inspect a booking and its full status history (the inline
    below) — inspection, not mutation.
    """
    list_display = ("reference", "customer", "resource", "status", "start_datetime", "end_datetime")
    list_filter = ("status",)
    search_fields = ("reference", "customer__username")
    inlines = [BookingStatusHistoryInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting a booking would erase its status history and audit
        # trail along with it — cancellation (through the engine) is
        # the correct way to end a booking's lifecycle, never deletion.
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor", "action", "target_model", "target_id")
    list_filter = ("target_model", "action")
    search_fields = ("target_id", "actor__username")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Tamper-resistance per research §8.
        return False


admin.site.register(Notification)
admin.site.register(SystemAlert)
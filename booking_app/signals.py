
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from .middleware import get_current_user, get_current_ip
from .models import Booking, Schedule, User, AuditLog

BOOKING_TRACKED_FIELDS = ["status", "resource_id", "start_datetime", "end_datetime", "customer_id", "payment_id"]


def _actor():
    user = get_current_user()
    return user if user and user.is_authenticated else None


@receiver(pre_save, sender=Booking)
def audit_booking_changes(sender, instance, **kwargs):

    if getattr(instance, "_via_engine", False):
        return
    if not instance.pk:
        return

    try:
        previous = Booking.objects.get(pk=instance.pk)
    except Booking.DoesNotExist:
        return

    changes = {}
    for field in BOOKING_TRACKED_FIELDS:
        old_value = getattr(previous, field)
        new_value = getattr(instance, field)
        if old_value != new_value:
            changes[field] = {"from": str(old_value), "to": str(new_value)}

    if changes:
        AuditLog.objects.create(
            actor=_actor(),
            action="booking.modified_outside_engine",
            target_model="Booking",
            target_id=str(instance.pk),
            previous_value={k: v["from"] for k, v in changes.items()},
            new_value={k: v["to"] for k, v in changes.items()},
            ip_address=get_current_ip(),
        )


@receiver(post_save, sender=Schedule)
def audit_schedule_changes(sender, instance, created, **kwargs):
    AuditLog.objects.create(
        actor=_actor(),
        action="schedule.created" if created else "schedule.modified",
        target_model="Schedule",
        target_id=str(instance.pk),
        new_value={
            "resource_id": instance.resource_id,
            "start_date": str(instance.start_date),
            "end_date": str(instance.end_date),
            "is_active": instance.is_active,
        },
        ip_address=get_current_ip(),
    )


@receiver(pre_save, sender=User)
def audit_user_role_changes(sender, instance, **kwargs):
    if not instance.pk:
        return
    try:
        previous = User.objects.get(pk=instance.pk)
    except User.DoesNotExist:
        return
    if previous.role != instance.role:
        AuditLog.objects.create(
            actor=_actor(),
            action="user.role_changed",
            target_model="User",
            target_id=str(instance.pk),
            previous_value={"role": previous.role},
            new_value={"role": instance.role},
            ip_address=get_current_ip(),
        )
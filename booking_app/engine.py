
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction, IntegrityError
from django.utils import timezone

from .models import (
    Booking, BookingStatus, BookingStatusHistory, Resource, record as audit_record,
)


class BookingConflictError(Exception):
    """Raised when a requested slot is already taken (brief §4/§25)."""


class BookingValidationError(Exception):
    """Raised for anything else that fails the pre-flight checks (§4)."""


@dataclass
class BookingRequest:
    customer: object
    resource: Resource
    start_datetime: datetime
    end_datetime: datetime
    guests: int
    created_by: Optional[object] = None


class BookingIntegrityEngine:
    @staticmethod
    def _run_preflight_checks(req: "BookingRequest") -> None:
        if req.customer is None or not req.customer.is_authenticated:
            raise BookingValidationError("You must be signed in to make a reservation.")
        if not req.resource.is_active:
            raise BookingValidationError("This listing is not currently available for booking.")
        if req.start_datetime >= req.end_datetime:
            raise BookingValidationError("The reservation end time must be after the start time.")
        if req.start_datetime < timezone.now():
            raise BookingValidationError("You can't book a date in the past.")
        if req.guests > req.resource.capacity:
            raise BookingValidationError(f"This listing accommodates up to {req.resource.capacity} guest(s).")
        duplicate = Booking.objects.filter(
            customer=req.customer,
            resource=req.resource,
            start_datetime=req.start_datetime,
            end_datetime=req.end_datetime,
            status__in=[BookingStatus.PENDING, BookingStatus.PAYMENT_PENDING, BookingStatus.CONFIRMED],
        ).exists()
        if duplicate:
            raise BookingValidationError("You already have a reservation for this exact slot.")

    @classmethod
    def create_booking(cls, req: "BookingRequest", ip_address: Optional[str] = None) -> Booking:
        cls._run_preflight_checks(req)

        try:
            with transaction.atomic():
                Resource.objects.select_for_update().get(pk=req.resource.pk)

                conflict_exists = Booking.objects.select_for_update().filter(
                    resource=req.resource,
                    start_datetime=req.start_datetime,
                    end_datetime=req.end_datetime,
                    status__in=[BookingStatus.PENDING, BookingStatus.PAYMENT_PENDING, BookingStatus.CONFIRMED],
                ).exists()
                if conflict_exists:
                    raise BookingConflictError(
                        "Sorry, this reservation cannot be completed because this resource has "
                        "just been reserved by another customer."
                    )

                booking = Booking.objects.create(
                    customer=req.customer,
                    resource=req.resource,
                    start_datetime=req.start_datetime,
                    end_datetime=req.end_datetime,
                    guests=req.guests,
                    status=BookingStatus.PAYMENT_PENDING,
                    created_by=req.created_by or req.customer,
                    payment_hold_expires_at=timezone.now() + timedelta(
                        minutes=settings.BOOKING_PAYMENT_HOLD_MINUTES
                    ),
                )
                BookingStatusHistory.objects.create(
                    booking=booking,
                    previous_status="",
                    new_status=booking.status,
                    changed_by=req.created_by or req.customer,
                    note="Booking created; awaiting M-Pesa payment.",
                )
                audit_record(
                    actor=req.created_by or req.customer,
                    action="booking.created",
                    target=booking,
                    new_value={
                        "reference": booking.reference,
                        "resource_id": booking.resource_id,
                        "start": str(booking.start_datetime),
                        "end": str(booking.end_datetime),
                    },
                    ip_address=ip_address,
                )
        except IntegrityError as exc:
            raise BookingConflictError(
                "Sorry, this reservation cannot be completed because this resource has "
                "just been reserved by another customer."
            ) from exc

        # TODO (Phase 10): broadcast a channel_layer group_send here so
        # staff dashboards update without a manual refresh.
        return booking

    @classmethod
    def confirm_payment(cls, booking: Booking, payment, actor=None) -> Booking:
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
            previous_status = booking.status
            booking.status = BookingStatus.CONFIRMED
            booking.payment = payment
            booking.version += 1
            booking._via_engine = True  # tells the audit signal this save is already logged below
            booking.save(update_fields=["status", "payment", "version", "updated_at"])
            BookingStatusHistory.objects.create(
                booking=booking,
                previous_status=previous_status,
                new_status=booking.status,
                changed_by=actor,
                note=f"Payment confirmed (M-Pesa receipt {payment.mpesa_receipt_number}).",
            )
            audit_record(
                actor=actor,
                action="booking.confirmed",
                target=booking,
                previous_value={"status": previous_status},
                new_value={"status": booking.status, "payment_id": payment.pk},
            )
        return booking

    @classmethod
    def fail_payment(cls, booking: Booking, reason: str, actor=None) -> Booking:
        """
        Called when an M-Pesa payment fails or is cancelled by the
        customer on their phone. Releases the resource hold (moves the
        booking to CANCELLED) so someone else can book the slot —
        payment being mandatory means a failed payment must never leave
        a resource stuck unavailable with no path back to bookable.
        """
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
            if booking.status != BookingStatus.PAYMENT_PENDING:
                return booking
            previous_status = booking.status
            booking.status = BookingStatus.CANCELLED
            booking.version += 1
            booking._via_engine = True  # tells the audit signal this save is already logged below
            booking.save(update_fields=["status", "version", "updated_at"])
            BookingStatusHistory.objects.create(
                booking=booking,
                previous_status=previous_status,
                new_status=booking.status,
                changed_by=actor,
                note=f"Payment failed: {reason}",
            )
            audit_record(
                actor=actor,
                action="booking.payment_failed",
                target=booking,
                previous_value={"status": previous_status},
                new_value={"status": booking.status, "reason": reason},
            )
        return booking

    @classmethod
    def release_expired_hold(cls, booking: Booking) -> Booking:
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=booking.pk)
            if booking.status != BookingStatus.PAYMENT_PENDING:
                return booking
            previous_status = booking.status
            booking.status = BookingStatus.EXPIRED
            booking.version += 1
            booking._via_engine = True  # tells the audit signal this save is already logged below
            booking.save(update_fields=["status", "version", "updated_at"])
            BookingStatusHistory.objects.create(
                booking=booking,
                previous_status=previous_status,
                new_status=booking.status,
                changed_by=None,
                note="Payment hold expired without confirmation.",
            )
            audit_record(
                actor=None,
                action="booking.expired",
                target=booking,
                previous_value={"status": previous_status},
                new_value={"status": booking.status},
            )
        return booking
import json
import logging
from datetime import timedelta
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import redirect, render, get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from .engine import BookingIntegrityEngine, BookingConflictError, BookingValidationError, BookingRequest
from .forms import StaffCreationForm, CustomerRegistrationForm
from .models import (
    User, Role, Destination, Resource, Booking, BookingStatus, Payment, PaymentStatus,
    record as audit_record,
)
from .mpesa_client import MpesaClient, verify_callback_token, parse_stk_callback
from .permissions import ManagerRequiredMixin, StaffOrManagerRequiredMixin

logger = logging.getLogger(__name__)

from django.contrib.auth.decorators import login_required
from django.views.generic import CreateView
from django.urls import reverse_lazy
from .forms import ResourceCreateForm
from .models import Resource, record as audit_record
from .permissions import StaffOrManagerRequiredMixin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import TemplateView, UpdateView
from django.urls import reverse_lazy
from django.contrib import messages

from .models import Booking, BookingStatus, Payment, User, CustomerProfile
from .forms import CustomerProfileForm   # we'll create this below


class CustomerDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "booking_app/customer_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        bookings = (
            Booking.objects
            .filter(customer=user)
            .select_related("resource", "resource__property", "payment")
            .order_by("-created_at")
        )
        context["bookings"] = bookings
        context["upcoming"] = bookings.filter(
            status__in=[
                BookingStatus.PENDING,
                BookingStatus.PAYMENT_PENDING,
                BookingStatus.CONFIRMED,
            ]
        )
        context["past"] = bookings.filter(
            status__in=[
                BookingStatus.COMPLETED,
                BookingStatus.CANCELLED,
                BookingStatus.EXPIRED,
            ]
        )
        context["payments"] = (
            Payment.objects
            .filter(customer=user)
            .order_by("-created_at")[:20]
        )
        # Ensure profile exists
        profile, _ = CustomerProfile.objects.get_or_create(user=user)
        context["profile"] = profile
        return context


class CustomerProfileUpdateView(LoginRequiredMixin, UpdateView):
    model = User
    form_class = CustomerProfileForm
    template_name = "booking_app/customer_profile.html"
    success_url = reverse_lazy("booking_app:customer_dashboard")

    def get_object(self):
        return self.request.user

    def form_valid(self, form):
        messages.success(self.request, "Profile updated successfully.")
        return super().form_valid(form)


@login_required
def booking_receipt_view(request, reference):
    """
    Printable receipt for a confirmed booking belonging to the current user.
    """
    booking = get_object_or_404(
        Booking.objects.select_related(
            "resource", "resource__property", "resource__property__destination",
            "customer", "payment"
        ),
        reference=reference,
        customer=request.user,
    )
    if booking.status not in (BookingStatus.CONFIRMED, BookingStatus.COMPLETED):
        messages.warning(request, "A receipt is only available for confirmed or completed bookings.")
        return redirect("booking_app:customer_dashboard")

    return render(request, "booking_app/booking_receipt.html", {"booking": booking})

class ResourceCreateView(StaffOrManagerRequiredMixin, CreateView):
    model = Resource
    form_class = ResourceCreateForm
    template_name = "booking_app/resource_create.html"
    success_url = reverse_lazy("booking_app:staff_dashboard")

    def form_valid(self, form):
        response = super().form_valid(form)
        audit_record(
            actor=self.request.user,
            action="resource.created",
            target=self.object,
            new_value={
                "name": self.object.name,
                "type": self.object.resource_type,
                "price": str(self.object.base_price),
            },
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        messages.success(
            self.request,
            f"{self.object.get_resource_type_display()} “{self.object.name}” added successfully."
        )
        return response
class HomeView(TemplateView):
    template_name = "booking_app/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["destinations"] = Destination.objects.filter(is_active=True)[:6]

        featured = list(
            Resource.objects.filter(is_active=True, is_featured=True)
            .select_related("property", "property__destination")[:6]
        )
        if not featured:
            featured = list(
                Resource.objects.filter(is_active=True)
                .select_related("property", "property__destination")[:6]
            )
        context["featured_resources"] = featured
        return context


class RegisterView(CreateView):
    model = User
    form_class = CustomerRegistrationForm
    template_name = "booking_app/register.html"
    success_url = reverse_lazy("booking_app:login")



class StaffDashboardView(StaffOrManagerRequiredMixin, TemplateView):
    template_name = "booking_app/staff_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        today = now.date()
        thirty_days_ago = now - timedelta(days=30)
        seven_days_ago = now - timedelta(days=7)
        prev_thirty = thirty_days_ago - timedelta(days=30)

        qs = Booking.objects.all()

        # ── Core counts ──────────────────────────────────────────────
        total = qs.count()
        active_statuses = [
            BookingStatus.PENDING,
            BookingStatus.PAYMENT_PENDING,
            BookingStatus.CONFIRMED,
        ]
        active = qs.filter(status__in=active_statuses).count()
        confirmed = qs.filter(status=BookingStatus.CONFIRMED).count()
        pending = qs.filter(
            status__in=[BookingStatus.PENDING, BookingStatus.PAYMENT_PENDING]
        ).count()

        confirmation_rate = round((confirmed / total * 100) if total else 0, 1)

        revenue = (
            qs.filter(status=BookingStatus.CONFIRMED, payment__isnull=False)
            .aggregate(total=Sum("payment__amount"))["total"]
            or 0
        )

        # ── Deltas ───────────────────────────────────────────────────
        active_recent = qs.filter(
            status__in=active_statuses, created_at__gte=seven_days_ago
        ).count()
        active_prev = qs.filter(
            status__in=active_statuses,
            created_at__gte=seven_days_ago - timedelta(days=7),
            created_at__lt=seven_days_ago,
        ).count()
        active_delta = (
            round(((active_recent - active_prev) / active_prev * 100), 1)
            if active_prev else (100.0 if active_recent else 0.0)
        )

        conf_this = qs.filter(
            status=BookingStatus.CONFIRMED, created_at__gte=thirty_days_ago
        ).count()
        conf_prev = qs.filter(
            status=BookingStatus.CONFIRMED,
            created_at__gte=prev_thirty,
            created_at__lt=thirty_days_ago,
        ).count()
        rate_delta = (
            round(((conf_this - conf_prev) / conf_prev * 100), 1)
            if conf_prev else (100.0 if conf_this else 0.0)
        )

        rev_this = (
            qs.filter(
                status=BookingStatus.CONFIRMED,
                payment__isnull=False,
                created_at__gte=thirty_days_ago,
            ).aggregate(t=Sum("payment__amount"))["t"] or 0
        )
        rev_prev = (
            qs.filter(
                status=BookingStatus.CONFIRMED,
                payment__isnull=False,
                created_at__gte=prev_thirty,
                created_at__lt=thirty_days_ago,
            ).aggregate(t=Sum("payment__amount"))["t"] or 0
        )
        revenue_delta = (
            round(((float(rev_this) - float(rev_prev)) / float(rev_prev) * 100), 1)
            if rev_prev else (100.0 if rev_this else 0.0)
        )

        volume_this = qs.filter(created_at__gte=thirty_days_ago).count()
        volume_prev = qs.filter(
            created_at__gte=prev_thirty, created_at__lt=thirty_days_ago
        ).count()
        volume_delta = (
            round(((volume_this - volume_prev) / volume_prev * 100), 1)
            if volume_prev else (100.0 if volume_this else 0.0)
        )

        # ── Chart data: bookings per day (last 30 days) ─────────────
        daily = (
            qs.filter(created_at__gte=thirty_days_ago)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(c=Count("id"))
            .order_by("day")
        )
        day_map = {row["day"]: row["c"] for row in daily}
        chart_labels = []
        chart_data = []
        for i in range(29, -1, -1):
            d = today - timedelta(days=i)
            chart_labels.append(d.strftime("%b %d"))
            chart_data.append(day_map.get(d, 0))

        spark_active = chart_data[-7:] if len(chart_data) >= 7 else chart_data
        spark_rate = [max(0, v - 1) for v in spark_active]
        spark_revenue = [v * 1200 for v in spark_active]

        # ── Status distribution ──────────────────────────────────────
        status_agg = (
            qs.values("status")
            .annotate(c=Count("id"))
            .order_by("-c")
        )
        status_labels = []
        status_counts = []
        label_map = dict(BookingStatus.choices)
        for row in status_agg:
            status_labels.append(label_map.get(row["status"], row["status"]))
            status_counts.append(row["c"])

        context["recent_bookings"] = (
            qs.select_related("resource", "customer").order_by("-created_at")[:20]
        )
        context["metrics"] = {
            "active_bookings": active,
            "active_delta": active_delta,
            "confirmation_rate": confirmation_rate,
            "rate_delta": rate_delta,
            "revenue": revenue,
            "revenue_delta": revenue_delta,
            "total_bookings": total,
            "volume_delta": volume_delta,
            "confirmed_count": confirmed,
            "confirmed_pct": round((confirmed / total * 100) if total else 0, 1),
            "pending_count": pending,
            "chart_labels": json.dumps(chart_labels),
            "chart_data": json.dumps(chart_data),
            "spark_active": json.dumps(spark_active),
            "spark_rate": json.dumps(spark_rate),
            "spark_revenue": json.dumps(spark_revenue),
            "status_labels": json.dumps(status_labels),
            "status_counts": json.dumps(status_counts),
        }
        return context
class StaffUserListView(ManagerRequiredMixin, ListView):
    model = User
    template_name = "booking_app/manager_user_list.html"
    context_object_name = "users"

    def get_queryset(self):
        return User.objects.exclude(role=Role.CUSTOMER).order_by("role", "username")


class StaffUserCreateView(ManagerRequiredMixin, CreateView):
    model = User
    form_class = StaffCreationForm
    template_name = "booking_app/manager_user_create.html"
    success_url = reverse_lazy("booking_app:manager_user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        audit_record(
            actor=self.request.user,
            action="user.created",
            target=self.object,
            new_value={"username": self.object.username, "role": self.object.role},
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        messages.success(self.request, f"{self.object.username} created as {self.object.get_role_display()}.")
        return response


class ResourceListView(ListView):
    model = Resource
    template_name = "booking_app/listing_list.html"
    context_object_name = "resources"
    paginate_by = 12

    def get_queryset(self):
        qs = Resource.objects.filter(is_active=True).select_related("property", "property__destination")

        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(property__name__icontains=query)
                | Q(property__destination__name__icontains=query)
            )

        resource_type = self.request.GET.get("type", "").strip()
        if resource_type:
            qs = qs.filter(resource_type=resource_type)

        guests = self.request.GET.get("guests", "").strip()
        if guests.isdigit():
            qs = qs.filter(capacity__gte=int(guests))

        start_raw = self.request.GET.get("start_date", "").strip()
        end_raw = self.request.GET.get("end_date", "").strip()
        start_date = parse_date(start_raw) if start_raw else None
        end_date = parse_date(end_raw) if end_raw else None
        if start_date and end_date:
            overlapping_resource_ids = Booking.objects.filter(
                status__in=[BookingStatus.PENDING, BookingStatus.PAYMENT_PENDING, BookingStatus.CONFIRMED],
                start_datetime__date__lt=end_date,
                end_datetime__date__gt=start_date,
            ).values_list("resource_id", flat=True)
            qs = qs.exclude(pk__in=overlapping_resource_ids)

        return qs.order_by("property__destination__name", "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        context["guests"] = self.request.GET.get("guests", "")
        context["start_date"] = self.request.GET.get("start_date", "")
        context["end_date"] = self.request.GET.get("end_date", "")
        context["resource_type"] = self.request.GET.get("type", "")
        context["destinations"] = Destination.objects.filter(is_active=True)
        context["is_filtered"] = any([
            context["query"], context["guests"], context["start_date"], context["end_date"],
            context["resource_type"],
        ])
        return context


class ResourceDetailView(DetailView):
    model = Resource
    template_name = "booking_app/listing_detail.html"
    context_object_name = "resource"


@login_required
def create_booking_view(request, resource_id):
    resource = get_object_or_404(Resource, pk=resource_id)

    if request.method == "POST":
        start_dt = parse_datetime(request.POST.get("start_datetime", ""))
        end_dt = parse_datetime(request.POST.get("end_datetime", ""))

        if start_dt is None or end_dt is None:
            messages.error(request, "Please provide a valid start and end date/time.")
            return redirect("booking_app:listing_detail", pk=resource.pk)

        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)

        req = BookingRequest(
            customer=request.user,
            resource=resource,
            start_datetime=start_dt,
            end_datetime=end_dt,
            guests=int(request.POST.get("guests", 1)),
            created_by=request.user,
        )
        try:
            booking = BookingIntegrityEngine.create_booking(req, ip_address=request.META.get("REMOTE_ADDR"))
        except (BookingConflictError, BookingValidationError) as exc:
            messages.error(request, str(exc))
            return redirect("booking_app:listing_detail", pk=resource.pk)

        messages.success(request, f"Reservation {booking.reference} created — complete payment to confirm.")
        return redirect("booking_app:booking_detail", reference=booking.reference)

    return render(request, "booking_app/booking_create.html", {"resource": resource})


class BookingDetailView(DetailView):
    model = Booking
    slug_field = "reference"
    slug_url_kwarg = "reference"
    context_object_name = "booking"
    template_name = "booking_app/booking_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["timeline"] = self.object.status_history.all()
        return context


def verify_booking_view(request):
    reference = request.GET.get("reference", "").strip().upper()
    booking = None
    if reference:
        booking = Booking.objects.filter(reference=reference).first()
    return render(request, "booking_app/booking_verify.html", {"booking": booking, "reference": reference})


@login_required
def initiate_payment_view(request, reference):
    booking = get_object_or_404(Booking, reference=reference, customer=request.user)

    if booking.status != BookingStatus.PAYMENT_PENDING:
        messages.info(request, "This booking isn't awaiting payment.")
        return redirect("booking_app:booking_detail", reference=booking.reference)

    if request.method == "POST":
        phone_number = request.POST.get("phone_number", "").strip()
        payment = Payment.objects.create(
            customer=request.user,
            amount=booking.resource.base_price,
            mpesa_phone_number=phone_number,
        )
        booking.payment = payment
        booking.save(update_fields=["payment"])

        try:
            result = MpesaClient().stk_push(
                phone_number=phone_number,
                amount=int(booking.resource.base_price),
                account_reference=booking.reference,
                description=f"Booking {booking.reference}",
            )
        except Exception:
            logger.exception("M-Pesa STK push failed for booking %s", booking.reference)
            payment.status = PaymentStatus.FAILED
            payment.save(update_fields=["status"])
            messages.error(request, "We couldn't reach M-Pesa right now. Please try again in a moment.")
            return redirect("booking_app:booking_pay", reference=booking.reference)

        payment.mpesa_checkout_request_id = result.checkout_request_id
        payment.mpesa_merchant_request_id = result.merchant_request_id
        payment.status = PaymentStatus.PENDING
        payment.save(update_fields=["mpesa_checkout_request_id", "mpesa_merchant_request_id", "status"])

        messages.success(request, "Check your phone and enter your M-Pesa PIN to complete payment.")
        return redirect("booking_app:booking_detail", reference=booking.reference)

    return render(request, "booking_app/booking_pay.html", {"booking": booking})


@csrf_exempt
@require_POST
def mpesa_callback_view(request):
    if not verify_callback_token(request):
        logger.warning("Rejected M-Pesa callback with invalid/missing token")
        return HttpResponseForbidden("Invalid callback token.")

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Received unparseable M-Pesa callback body")
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    parsed = parse_stk_callback(payload)
    if parsed is None:
        logger.warning("Received malformed M-Pesa callback: %s", payload)
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(
            mpesa_checkout_request_id=parsed["checkout_request_id"]
        ).first()
        if payment is None:
            logger.warning("M-Pesa callback for unknown CheckoutRequestID %s", parsed["checkout_request_id"])
            return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

        if payment.status in (PaymentStatus.CONFIRMED, PaymentStatus.FAILED):
            return JsonResponse({"ResultCode": 0, "ResultDesc": "Already processed"})

        payment.raw_callback_payload = payload

        if parsed["result_code"] == 0:
            payment.status = PaymentStatus.CONFIRMED
            payment.mpesa_receipt_number = parsed["receipt_number"] or ""
            payment.confirmed_at = timezone.now()
            payment.save(update_fields=[
                "raw_callback_payload", "status", "mpesa_receipt_number", "confirmed_at",
            ])
            BookingIntegrityEngine.confirm_payment(payment.booking, payment, actor=None)
        else:
            payment.status = PaymentStatus.FAILED
            payment.save(update_fields=["raw_callback_payload", "status"])
            BookingIntegrityEngine.fail_payment(payment.booking, reason=parsed["result_desc"])

    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})
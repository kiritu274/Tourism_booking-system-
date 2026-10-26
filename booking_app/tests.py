
import sys
import asyncio
import platform
import unittest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import datetime
import json
from unittest.mock import patch

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .engine import BookingIntegrityEngine, BookingRequest, BookingConflictError, BookingValidationError
from .models import User, Destination, Property, Resource, Booking, BookingStatus, Payment, Schedule, AuditLog
from .mpesa_client import StkPushResult

TEST_CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


class BookingIntegrityEngineTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username="jane", password="test1234", role="customer")
        self.other_customer = User.objects.create_user(username="john", password="test1234", role="customer")
        destination = Destination.objects.create(name="Diani Beach")
        property_ = Property.objects.create(destination=destination, name="Sea Breeze Resort")
        self.resource = Resource.objects.create(
            property=property_, resource_type="room", name="Room 204", capacity=2, base_price=5000
        )
        self.start = timezone.now() + datetime.timedelta(days=1)
        self.end = timezone.now() + datetime.timedelta(days=3)

    def test_successful_booking(self):
        req = BookingRequest(customer=self.customer, resource=self.resource,
                              start_datetime=self.start, end_datetime=self.end, guests=2)
        booking = BookingIntegrityEngine.create_booking(req)
        self.assertEqual(booking.status, BookingStatus.PAYMENT_PENDING)
        self.assertTrue(booking.reference.startswith("TRV-"))

    def test_concurrent_booking_rejects_second_customer(self):

        req1 = BookingRequest(customer=self.customer, resource=self.resource,
                               start_datetime=self.start, end_datetime=self.end, guests=1)
        BookingIntegrityEngine.create_booking(req1)

        req2 = BookingRequest(customer=self.other_customer, resource=self.resource,
                               start_datetime=self.start, end_datetime=self.end, guests=1)
        with self.assertRaises(BookingConflictError):
            BookingIntegrityEngine.create_booking(req2)

    def test_duplicate_booking_by_same_customer_rejected(self):
        req = BookingRequest(customer=self.customer, resource=self.resource,
                              start_datetime=self.start, end_datetime=self.end, guests=1)
        BookingIntegrityEngine.create_booking(req)
        with self.assertRaises(BookingValidationError):
            BookingIntegrityEngine.create_booking(req)

    def test_cannot_book_inactive_resource(self):
        self.resource.is_active = False
        self.resource.save()
        req = BookingRequest(customer=self.customer, resource=self.resource,
                              start_datetime=self.start, end_datetime=self.end, guests=1)
        with self.assertRaises(BookingValidationError):
            BookingIntegrityEngine.create_booking(req)

    def test_cannot_exceed_capacity(self):
        req = BookingRequest(customer=self.customer, resource=self.resource,
                              start_datetime=self.start, end_datetime=self.end, guests=99)
        with self.assertRaises(BookingValidationError):
            BookingIntegrityEngine.create_booking(req)


class RolePermissionTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username="cust", password="test1234", role="customer")
        self.staff = User.objects.create_user(username="staffer", password="test1234", role="staff")
        self.manager = User.objects.create_user(username="boss", password="test1234", role="manager")

    def test_customer_cannot_access_manager_user_list(self):
        self.client.login(username="cust", password="test1234")
        response = self.client.get("/manager/users/")
        self.assertEqual(response.status_code, 403)

    def test_staff_cannot_access_manager_user_list(self):
        self.client.login(username="staffer", password="test1234")
        response = self.client.get("/manager/users/")
        self.assertEqual(response.status_code, 403)

    def test_manager_can_access_manager_user_list(self):
        self.client.login(username="boss", password="test1234")
        response = self.client.get("/manager/users/")
        self.assertEqual(response.status_code, 200)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get("/manager/users/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_manager_can_create_staff_account(self):
        self.client.login(username="boss", password="test1234")
        response = self.client.post("/manager/users/new/", {
            "username": "newstaff",
            "email": "newstaff@example.com",
            "first_name": "New",
            "last_name": "Staff",
            "phone_number": "0712345678",
            "role": "staff",
            "department": "Front Desk",
            "password1": "SuperSecret123!",
            "password2": "SuperSecret123!",
        })
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username="newstaff")
        self.assertEqual(created.role, "staff")
        self.assertTrue(hasattr(created, "staff_profile"))

    def test_public_registration_always_creates_customer(self):
        response = self.client.post("/accounts/register/", {
            "username": "walkin",
            "password1": "SuperSecret123!",
            "password2": "SuperSecret123!",
        })
        self.assertEqual(response.status_code, 302)
        created = User.objects.get(username="walkin")
        self.assertEqual(created.role, "customer")


class PaymentFlowTests(TestCase):
    """
    Phase 8 — M-Pesa STK push initiation and callback handling. Real
    Daraja calls are mocked; the callback tests simulate exactly the
    JSON shape Safaricom sends, so these exercise the actual parsing and
    idempotency logic, not just a stub.
    """
    def setUp(self):
        self.customer = User.objects.create_user(username="jane", password="test1234", role="customer")
        destination = Destination.objects.create(name="Diani Beach")
        property_ = Property.objects.create(destination=destination, name="Sea Breeze Resort")
        self.resource = Resource.objects.create(
            property=property_, resource_type="room", name="Room 1", capacity=2, base_price=5000
        )
        req = BookingRequest(
            customer=self.customer, resource=self.resource,
            start_datetime=timezone.now() + datetime.timedelta(days=1),
            end_datetime=timezone.now() + datetime.timedelta(days=2), guests=1,
        )
        self.booking = BookingIntegrityEngine.create_booking(req)
        self.client.login(username="jane", password="test1234")

    @patch("booking_app.views.MpesaClient")
    def test_initiate_payment_creates_pending_payment(self, mock_client_cls):
        mock_client_cls.return_value.stk_push.return_value = StkPushResult(
            checkout_request_id="ws_CO_123", merchant_request_id="mr_123",
            response_code="0", response_description="Success",
        )
        response = self.client.post(f"/bookings/{self.booking.reference}/pay/", {"phone_number": "254712345678"})
        self.assertEqual(response.status_code, 302)
        self.booking.refresh_from_db()
        self.assertIsNotNone(self.booking.payment)
        self.assertEqual(self.booking.payment.status, "pending")
        self.assertEqual(self.booking.payment.mpesa_checkout_request_id, "ws_CO_123")

    @patch("booking_app.views.MpesaClient")
    def test_stk_push_failure_does_not_crash_and_marks_payment_failed(self, mock_client_cls):
        mock_client_cls.return_value.stk_push.side_effect = Exception("network error")
        response = self.client.post(f"/bookings/{self.booking.reference}/pay/", {"phone_number": "254712345678"})
        self.assertEqual(response.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.payment.status, "failed")
        # Booking itself stays PAYMENT_PENDING — customer can retry.
        self.assertEqual(self.booking.status, "payment_pending")

    @override_settings(MPESA_CALLBACK_SECRET="testsecret")
    @patch("booking_app.views.MpesaClient")
    def test_successful_callback_confirms_booking(self, mock_client_cls):
        mock_client_cls.return_value.stk_push.return_value = StkPushResult(
            checkout_request_id="ws_CO_456", merchant_request_id="mr_456",
            response_code="0", response_description="Success",
        )
        self.client.post(f"/bookings/{self.booking.reference}/pay/", {"phone_number": "254712345678"})

        callback_payload = {
            "Body": {"stkCallback": {
                "MerchantRequestID": "mr_456", "CheckoutRequestID": "ws_CO_456",
                "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {"Item": [
                    {"Name": "Amount", "Value": 5000},
                    {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
                    {"Name": "PhoneNumber", "Value": 254712345678},
                ]},
            }}
        }
        response = self.client.post(
            "/payments/mpesa/callback/?token=testsecret",
            data=json.dumps(callback_payload), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")
        self.assertEqual(self.booking.payment.mpesa_receipt_number, "NLJ7RT61SV")

    @override_settings(MPESA_CALLBACK_SECRET="testsecret")
    @patch("booking_app.views.MpesaClient")
    def test_duplicate_callback_does_not_double_confirm(self, mock_client_cls):
        mock_client_cls.return_value.stk_push.return_value = StkPushResult(
            checkout_request_id="ws_CO_789", merchant_request_id="mr_789",
            response_code="0", response_description="Success",
        )
        self.client.post(f"/bookings/{self.booking.reference}/pay/", {"phone_number": "254712345678"})

        callback_payload = {
            "Body": {"stkCallback": {
                "MerchantRequestID": "mr_789", "CheckoutRequestID": "ws_CO_789",
                "ResultCode": 0, "ResultDesc": "Success",
                "CallbackMetadata": {"Item": [
                    {"Name": "MpesaReceiptNumber", "Value": "ABC123"},
                ]},
            }}
        }
        url = "/payments/mpesa/callback/?token=testsecret"
        self.client.post(url, data=json.dumps(callback_payload), content_type="application/json")
        self.client.post(url, data=json.dumps(callback_payload), content_type="application/json")

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")
        # Exactly one "confirmed" transition recorded, not two.
        confirmed_entries = self.booking.status_history.filter(new_status="confirmed").count()
        self.assertEqual(confirmed_entries, 1)

    @override_settings(MPESA_CALLBACK_SECRET="testsecret")
    @patch("booking_app.views.MpesaClient")
    def test_failed_payment_cancels_booking_and_releases_resource(self, mock_client_cls):
        mock_client_cls.return_value.stk_push.return_value = StkPushResult(
            checkout_request_id="ws_CO_fail", merchant_request_id="mr_fail",
            response_code="0", response_description="Success",
        )
        self.client.post(f"/bookings/{self.booking.reference}/pay/", {"phone_number": "254712345678"})

        callback_payload = {
            "Body": {"stkCallback": {
                "MerchantRequestID": "mr_fail", "CheckoutRequestID": "ws_CO_fail",
                "ResultCode": 1032, "ResultDesc": "Request cancelled by user",
            }}
        }
        response = self.client.post(
            "/payments/mpesa/callback/?token=testsecret",
            data=json.dumps(callback_payload), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "cancelled")

        # The resource is free again — a new customer can book the same slot.
        other_customer = User.objects.create_user(username="john", password="test1234", role="customer")
        req = BookingRequest(customer=other_customer, resource=self.resource,
                              start_datetime=self.booking.start_datetime,
                              end_datetime=self.booking.end_datetime, guests=1)
        new_booking = BookingIntegrityEngine.create_booking(req)
        self.assertEqual(new_booking.status, "payment_pending")

    def test_callback_rejected_without_valid_token(self):
        response = self.client.post(
            "/payments/mpesa/callback/",
            data=json.dumps({"Body": {"stkCallback": {}}}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(MPESA_CALLBACK_SECRET="testsecret")
    def test_callback_with_wrong_token_rejected(self):
        response = self.client.post(
            "/payments/mpesa/callback/?token=wrongtoken",
            data=json.dumps({"Body": {"stkCallback": {}}}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class CreateBookingViewTests(TestCase):
    """
    Regression test for the datetime-local string vs datetime.datetime
    TypeError caught on a live run — request.POST always returns strings,
    and create_booking_view must parse them before they reach the engine.
    """
    def setUp(self):
        self.customer = User.objects.create_user(username="jane", password="test1234", role="customer")
        destination = Destination.objects.create(name="Diani Beach")
        property_ = Property.objects.create(destination=destination, name="Sea Breeze Resort")
        self.resource = Resource.objects.create(
            property=property_, resource_type="room", name="Room 1", capacity=2, base_price=5000
        )
        self.client.login(username="jane", password="test1234")

    def test_create_booking_via_form_post_parses_datetime_strings(self):
        tomorrow = (timezone.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        day_after = (timezone.now() + datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(f"/bookings/new/{self.resource.pk}/", {
            "start_datetime": tomorrow,
            "end_datetime": day_after,
            "guests": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 1)

    def test_create_booking_rejects_unparseable_datetime(self):
        response = self.client.post(f"/bookings/new/{self.resource.pk}/", {
            "start_datetime": "not-a-date",
            "end_datetime": "also-not-a-date",
            "guests": "1",
        })
        self.assertEqual(response.status_code, 302)  # redirected back with an error message
        self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 0)


class AuditLockdownTests(TestCase):
    """
    Phase 9 — proves the two halves of the lockdown independently:
    (1) admin.py actually blocks Booking/Payment mutation through the
    Django admin, for every user including superusers, and (2) the
    signals.py backstop catches a Booking changed by any OTHER path
    (e.g. a raw .save() from the shell) without double-logging what the
    engine already logs explicitly.
    """
    def setUp(self):
        self.manager = User.objects.create_user(
            username="boss", password="test1234", role="manager", is_staff=True, is_superuser=True
        )
        self.customer = User.objects.create_user(username="jane", password="test1234", role="customer")
        destination = Destination.objects.create(name="Diani Beach")
        property_ = Property.objects.create(destination=destination, name="Sea Breeze Resort")
        self.resource = Resource.objects.create(
            property=property_, resource_type="room", name="Room 1", capacity=2, base_price=5000
        )
        req = BookingRequest(
            customer=self.customer, resource=self.resource,
            start_datetime=timezone.now() + datetime.timedelta(days=1),
            end_datetime=timezone.now() + datetime.timedelta(days=2), guests=1,
        )
        self.booking = BookingIntegrityEngine.create_booking(req)

    def test_superuser_cannot_add_booking_via_admin(self):
        self.client.login(username="boss", password="test1234")
        response = self.client.get("/admin/booking_app/booking/add/")
        # Django admin redirects to the changelist with a permission
        # error when add is disallowed, rather than a bare 403 — either
        # way, no add form is served.
        self.assertNotEqual(response.status_code, 200)

    def test_superuser_sees_booking_fields_as_readonly(self):
        self.client.login(username="boss", password="test1234")
        response = self.client.get(f"/admin/booking_app/booking/{self.booking.pk}/change/")
        self.assertEqual(response.status_code, 200)
        # No editable <select> or <input> for the status field — readonly
        # fields render as plain text, not form controls, in Django admin.
        self.assertNotContains(response, 'name="status"')

    def test_direct_save_outside_engine_creates_backstop_audit_entry(self):
        """Simulates a bypass — e.g. someone editing via shell or a future bug."""
        self.booking.status = "cancelled"
        self.booking.save()  # deliberately NOT setting _via_engine

        entry = AuditLog.objects.filter(
            target_model="Booking", target_id=str(self.booking.pk),
            action="booking.modified_outside_engine",
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.previous_value["status"], "payment_pending")
        self.assertEqual(entry.new_value["status"], "cancelled")

    def test_engine_confirm_payment_does_not_trigger_backstop(self):
        """The engine's own audit_record should be the only entry — no duplicate from the signal."""
        payment = Payment.objects.create(customer=self.customer, amount=5000, status="pending")
        BookingIntegrityEngine.confirm_payment(self.booking, payment, actor=self.manager)

        backstop_entries = AuditLog.objects.filter(
            target_model="Booking", target_id=str(self.booking.pk),
            action="booking.modified_outside_engine",
        ).count()
        self.assertEqual(backstop_entries, 0)
        engine_entries = AuditLog.objects.filter(
            target_model="Booking", target_id=str(self.booking.pk), action="booking.confirmed",
        ).count()
        self.assertEqual(engine_entries, 1)

    def test_schedule_save_is_audit_logged(self):
        schedule = Schedule.objects.create(
            resource=self.resource, start_date=timezone.now().date(),
            end_date=(timezone.now() + datetime.timedelta(days=30)).date(),
        )
        entry = AuditLog.objects.filter(
            target_model="Schedule", target_id=str(schedule.pk), action="schedule.created",
        ).first()
        self.assertIsNotNone(entry)

    def test_user_role_change_is_audit_logged(self):
        staff = User.objects.create_user(username="newstaff", password="test1234", role="staff")
        staff.role = "manager"
        staff.save()
        entry = AuditLog.objects.filter(
            target_model="User", target_id=str(staff.pk), action="user.role_changed",
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.previous_value["role"], "staff")
        self.assertEqual(entry.new_value["role"], "manager")


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class RealtimeBroadcastTests(TransactionTestCase):

    async def _make_fixtures(self):
        manager = await database_sync_to_async(User.objects.create_user)(
            username="boss", password="test1234", role="manager"
        )
        customer = await database_sync_to_async(User.objects.create_user)(
            username="jane", password="test1234", role="customer"
        )
        other_customer = await database_sync_to_async(User.objects.create_user)(
            username="john", password="test1234", role="customer"
        )
        destination = await database_sync_to_async(Destination.objects.create)(name="Diani Beach")
        property_ = await database_sync_to_async(Property.objects.create)(
            destination=destination, name="Sea Breeze Resort"
        )
        resource = await database_sync_to_async(Resource.objects.create)(
            property=property_, resource_type="room", name="Room 1", capacity=2, base_price=5000
        )
        return manager, customer, other_customer, resource

    @unittest.skipIf(
        platform.system() == "Windows",
        "Known asgiref/Windows event-loop limitation in this test's cross-thread "
        "broadcast delivery — the feature itself is verified manually (two browser "
        "windows) and the connection-authorization tests in this class still run "
        "on Windows.",
    )
    async def test_manager_dashboard_receives_booking_created_event(self):
        from config.asgi import application

        manager, customer, _, resource = await self._make_fixtures()

        communicator = WebsocketCommunicator(application, "/ws/dashboard/")
        communicator.scope["user"] = manager
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.receive_json_from()  # discard "connection.established"

        req = BookingRequest(
            customer=customer, resource=resource,
            start_datetime=timezone.now() + datetime.timedelta(days=1),
            end_datetime=timezone.now() + datetime.timedelta(days=2), guests=1,
        )
        await database_sync_to_async(BookingIntegrityEngine.create_booking)(req)

        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response["event"], "booking.created")
        self.assertEqual(response["customer"], "jane")
        await communicator.disconnect()

    async def test_customer_rejected_from_staff_dashboard(self):
        from config.asgi import application

        _, customer, _, _ = await self._make_fixtures()

        communicator = WebsocketCommunicator(application, "/ws/dashboard/")
        communicator.scope["user"] = customer
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    @unittest.skipIf(
        platform.system() == "Windows",
        "Known asgiref/Windows event-loop limitation in this test's cross-thread "
        "broadcast delivery — the feature itself is verified manually (two browser "
        "windows) and the connection-authorization tests in this class still run "
        "on Windows.",
    )
    async def test_booking_owner_receives_own_booking_updates(self):
        from config.asgi import application

        _, customer, _, resource = await self._make_fixtures()

        req = BookingRequest(
            customer=customer, resource=resource,
            start_datetime=timezone.now() + datetime.timedelta(days=1),
            end_datetime=timezone.now() + datetime.timedelta(days=2), guests=1,
        )
        booking = await database_sync_to_async(BookingIntegrityEngine.create_booking)(req)

        communicator = WebsocketCommunicator(application, f"/ws/bookings/{booking.reference}/")
        communicator.scope["user"] = customer
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        payment = await database_sync_to_async(Payment.objects.create)(
            customer=customer, amount=5000, status="pending"
        )
        await database_sync_to_async(BookingIntegrityEngine.confirm_payment)(booking, payment)

        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response["event"], "booking.confirmed")
        await communicator.disconnect()

    async def test_other_customer_rejected_from_someone_elses_booking(self):
        from config.asgi import application

        _, customer, other_customer, resource = await self._make_fixtures()

        req = BookingRequest(
            customer=customer, resource=resource,
            start_datetime=timezone.now() + datetime.timedelta(days=1),
            end_datetime=timezone.now() + datetime.timedelta(days=2), guests=1,
        )
        booking = await database_sync_to_async(BookingIntegrityEngine.create_booking)(req)

        communicator = WebsocketCommunicator(application, f"/ws/bookings/{booking.reference}/")
        communicator.scope["user"] = other_customer
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

class SearchAndListingTests(TestCase):
    """
    Phase 11 — the homepage/listings search is a real filter, not
    decoration (brief §17: "Search available accommodation/tours/
    services", "Check real-time availability"). These confirm text
    search, capacity filtering, category filtering, and — the one that
    actually matters — that a resource with an overlapping active
    booking is excluded from date-filtered results.
    """
    def setUp(self):
        destination = Destination.objects.create(name="Diani Beach")
        property_ = Property.objects.create(destination=destination, name="Sea Breeze Resort")
        self.room = Resource.objects.create(
            property=property_, resource_type="room", name="Room 204", capacity=2, base_price=5000
        )
        self.tour = Resource.objects.create(
            property=property_, resource_type="tour", name="Sunset Dhow Cruise", capacity=10, base_price=3500
        )
        self.customer = User.objects.create_user(username="jane", password="test1234", role="customer")

    def test_text_search_matches_resource_and_destination_name(self):
        response = self.client.get("/listings/?q=Diani")
        self.assertContains(response, "Room 204")
        self.assertContains(response, "Sunset Dhow Cruise")

        response = self.client.get("/listings/?q=Sunset")
        self.assertContains(response, "Sunset Dhow Cruise")
        self.assertNotContains(response, "Room 204")

    def test_guest_count_filters_by_capacity(self):
        response = self.client.get("/listings/?guests=5")
        self.assertNotContains(response, "Room 204")  # capacity 2, too small
        self.assertContains(response, "Sunset Dhow Cruise")  # capacity 10, fits

    def test_type_filter(self):
        response = self.client.get("/listings/?type=room")
        self.assertContains(response, "Room 204")
        self.assertNotContains(response, "Sunset Dhow Cruise")

    def test_date_filter_excludes_resource_with_overlapping_booking(self):
        req = BookingRequest(
            customer=self.customer, resource=self.room,
            start_datetime=timezone.now() + datetime.timedelta(days=5),
            end_datetime=timezone.now() + datetime.timedelta(days=8), guests=1,
        )
        BookingIntegrityEngine.create_booking(req)

        overlap_start = (timezone.now() + datetime.timedelta(days=6)).date().isoformat()
        overlap_end = (timezone.now() + datetime.timedelta(days=10)).date().isoformat()
        response = self.client.get(f"/listings/?start_date={overlap_start}&end_date={overlap_end}")
        self.assertNotContains(response, "Room 204")
        self.assertContains(response, "Sunset Dhow Cruise")  # unaffected, no booking on it

    def test_date_filter_includes_resource_when_dates_dont_overlap(self):
        req = BookingRequest(
            customer=self.customer, resource=self.room,
            start_datetime=timezone.now() + datetime.timedelta(days=5),
            end_datetime=timezone.now() + datetime.timedelta(days=8), guests=1,
        )
        BookingIntegrityEngine.create_booking(req)

        # Fully after the existing booking ends — no overlap.
        free_start = (timezone.now() + datetime.timedelta(days=20)).date().isoformat()
        free_end = (timezone.now() + datetime.timedelta(days=22)).date().isoformat()
        response = self.client.get(f"/listings/?start_date={free_start}&end_date={free_end}")
        self.assertContains(response, "Room 204")

    def test_homepage_renders_with_destinations_and_featured(self):
        self.room.is_featured = True
        self.room.save()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Diani Beach")
        self.assertContains(response, "Room 204")
        self.assertContains(response, "Why your booking is different")  # brief §19 differentiator section
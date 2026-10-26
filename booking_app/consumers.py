
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async


class BookingDashboardConsumer(AsyncJsonWebsocketConsumer):


    GROUP_NAME = "bookings_dashboard"

    async def connect(self):
        user = self.scope["user"]
        if not user.is_authenticated or not await self._is_staff_or_manager(user):
            await self.close()
            return
        await self.channel_layer.group_add(self.GROUP_NAME, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connection.established", "message": "System Live"})

    @database_sync_to_async
    def _is_staff_or_manager(self, user):
        return user.is_booking_staff() or user.is_manager()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.GROUP_NAME, self.channel_name)

    async def booking_event(self, event):
        """Handler name must match the `type` used in group_send calls ("booking.event")."""
        await self.send_json(event["payload"])


class BookingDetailConsumer(AsyncJsonWebsocketConsumer):
    """
    Feeds a single customer's 'My Booking' timeline page. Restricted to
    that booking's own customer, or staff/manager — not any logged-in
    user, which would let one customer watch another's booking update
    live just by guessing a reference in the URL.
    """
    async def connect(self):
        self.reference = self.scope["url_route"]["kwargs"]["reference"]
        user = self.scope["user"]
        if not user.is_authenticated or not await self._may_view(user, self.reference):
            await self.close()
            return
        self.group_name = f"booking_{self.reference}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    @database_sync_to_async
    def _may_view(self, user, reference):
        from .models import Booking
        booking = Booking.objects.filter(reference=reference).first()
        if booking is None:
            return False
        return booking.customer_id == user.id or user.is_booking_staff() or user.is_manager()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def booking_event(self, event):
        await self.send_json(event["payload"])
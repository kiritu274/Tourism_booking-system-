
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)

DASHBOARD_GROUP = "bookings_dashboard"


def broadcast_booking_event(event_type: str, booking) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    payload = {
        "event": event_type,
        "reference": booking.reference,
        "status": booking.status,
        "resource": booking.resource.name,
        "customer": booking.customer.username,
        "updated_at": booking.updated_at.isoformat(),
    }

    try:
        async_to_sync(channel_layer.group_send)(
            DASHBOARD_GROUP, {"type": "booking.event", "payload": payload}
        )
        async_to_sync(channel_layer.group_send)(
            f"booking_{booking.reference}", {"type": "booking.event", "payload": payload}
        )
    except Exception:
        logger.exception("Failed to broadcast booking event %s for %s", event_type, booking.reference)
from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/dashboard/$", consumers.BookingDashboardConsumer.as_asgi()),
    re_path(r"^ws/bookings/(?P<reference>[\w-]+)/$", consumers.BookingDetailConsumer.as_asgi()),
]
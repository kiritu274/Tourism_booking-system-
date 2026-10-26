from django.apps import AppConfig


class BookingAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "booking_app"
    verbose_name = "Tourism Booking Integrity System"

    def ready(self):
        from . import signals  # noqa: F401  — registers the audit signal receivers
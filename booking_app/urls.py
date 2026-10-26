from django.contrib.auth import views as auth_views
from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views
from .views import ResourceCreateView

app_name = "booking_app"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),

    # Auth
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="booking_app/login.html"),
        name="login",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("accounts/register/", views.RegisterView.as_view(), name="register"),

    # Password reset
    path(
        "accounts/forgot-password/",
        auth_views.PasswordResetView.as_view(
            template_name="booking_app/password_reset_form.html",
            email_template_name="booking_app/password_reset_email.html",
            success_url="/accounts/forgot-password/done/",
        ),
        name="password_reset",
    ),
    path(
        "accounts/forgot-password/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="booking_app/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "accounts/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="booking_app/password_reset_confirm.html",
            success_url="/accounts/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "accounts/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="booking_app/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),

    # Manager: staff/user management
    path("manager/users/", views.StaffUserListView.as_view(), name="manager_user_list"),
    path("manager/users/new/", views.StaffUserCreateView.as_view(), name="manager_user_create"),

    # Listings
    path("listings/", views.ResourceListView.as_view(), name="listing_list"),
    path("listings/<int:pk>/", views.ResourceDetailView.as_view(), name="listing_detail"),

    # Bookings
    path("bookings/new/<int:resource_id>/", views.create_booking_view, name="booking_create"),
    path("bookings/verify/", views.verify_booking_view, name="booking_verify"),
    path("bookings/<str:reference>/", views.BookingDetailView.as_view(), name="booking_detail"),

    # Staff/Manager
    path("staff/dashboard/", views.StaffDashboardView.as_view(), name="staff_dashboard"),
    path("staff/resources/add/", ResourceCreateView.as_view(), name="resource_create"),

    # Payments (M-Pesa)
    path("bookings/<str:reference>/pay/", views.initiate_payment_view, name="booking_pay"),
    path("payments/mpesa/callback/", views.mpesa_callback_view, name="mpesa_callback"),
# Customer dashboard
path("my/", views.CustomerDashboardView.as_view(), name="customer_dashboard"),
path("my/profile/", views.CustomerProfileUpdateView.as_view(), name="customer_profile"),
path("my/receipts/<str:reference>/", views.booking_receipt_view, name="booking_receipt"),
]
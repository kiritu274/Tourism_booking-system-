
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from functools import wraps


class ManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):

    raise_exception = False  # redirect to log in instead of a bare 403 for anonymous users

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_manager()


class StaffOrManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):

    raise_exception = False

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_booking_staff() or user.is_manager())


def manager_required(view_func):

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not request.user.is_manager():
            raise PermissionDenied("This page is restricted to managers.")
        return view_func(request, *args, **kwargs)
    return wrapped


def staff_or_manager_required(view_func):

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        if not (request.user.is_booking_staff() or request.user.is_manager()):
            raise PermissionDenied("This page is restricted to staff and managers.")
        return view_func(request, *args, **kwargs)
    return wrapped

from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import User, Role, StaffProfile
from django import forms
from .models import Resource, Property, ResourceType
from django import forms
from django.contrib.auth import get_user_model
from .models import CustomerProfile

User = get_user_model()


class CustomerProfileForm(forms.ModelForm):
    # Extra fields from CustomerProfile
    id_number = forms.CharField(max_length=30, required=False, label="ID / Passport number")
    phone_number = forms.CharField(max_length=20, required=False)

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone_number"]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "phone_number": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["id_number"].widget.attrs.update({"class": "form-control"})
        if self.instance and self.instance.pk:
            profile, _ = CustomerProfile.objects.get_or_create(user=self.instance)
            self.fields["id_number"].initial = profile.id_number
            self.fields["phone_number"].initial = self.instance.phone_number

    def save(self, commit=True):
        user = super().save(commit=commit)
        profile, _ = CustomerProfile.objects.get_or_create(user=user)
        profile.id_number = self.cleaned_data.get("id_number", "")
        profile.save()
        return user

class ResourceCreateForm(forms.ModelForm):
    class Meta:
        model = Resource
        fields = [
            "property",
            "resource_type",
            "name",
            "description",
            "capacity",
            "base_price",
            "is_active",
            "is_featured",
            "image",
        ]
        widgets = {
            "property": forms.Select(attrs={"class": "form-select"}),
            "resource_type": forms.Select(attrs={"class": "form-select"}),
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Deluxe Room / Maasai Mara Safari"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "capacity": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "base_price": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_featured": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "image": forms.ClearableFileInput(attrs={"class": "form-control", "accept": "image/*"}),
        }
        labels = {
            "resource_type": "Type (Room / Tour / Service)",
            "base_price": "Base price (KES)",
            "image": "Photo",
        }

class StaffCreationForm(UserCreationForm):
    role = forms.ChoiceField(
        choices=[(Role.STAFF, "Staff / Booking Officer"), (Role.MANAGER, "Manager / Administrator")],
    )
    department = forms.CharField(max_length=100, required=False)
    phone_number = forms.CharField(max_length=20, required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "first_name", "last_name", "phone_number", "role")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = self.cleaned_data["role"]
        user.phone_number = self.cleaned_data.get("phone_number", "")
        if commit:
            user.save()
            StaffProfile.objects.create(user=user, department=self.cleaned_data.get("department", ""))
        return user


class CustomerRegistrationForm(UserCreationForm):
    """
    Public self-registration form. UserCreationForm's default Meta binds
    to auth.User, which breaks once AUTH_USER_MODEL is swapped (as it is
    here) — this override is what actually fixes that, not just the role
    assignment RegisterView does afterward.
    """
    email = forms.EmailField(required=False)
    phone_number = forms.CharField(max_length=20, required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "phone_number")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = Role.CUSTOMER
        user.phone_number = self.cleaned_data.get("phone_number", "")
        if commit:
            user.save()
        return user
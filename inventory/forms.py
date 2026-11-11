from django import forms
from django.contrib.auth import get_user_model

from .models import (
    InventoryItem,
    AppConfig,
    UserPreference,
    Note,
)

User = get_user_model()


class InventoryItemForm(forms.ModelForm):
    version = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['version'].required = True
            self.initial['version'] = self.instance.updated_at.isoformat()

    class Meta:
        model = InventoryItem
        fields = ['name', 'part_id', 'color', 'quantity_total', 'quantity_used', 'storage_location', 'image_url', 'notes']
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def clean(self):
        cleaned = super().clean()
        qt = cleaned.get('quantity_total') or 0
        qu = cleaned.get('quantity_used') or 0
        if qu > qt:
            self.add_error('quantity_used', 'Used quantity cannot exceed total quantity.')
        if self.instance.user_id and all(k in cleaned for k in ('part_id', 'color', 'storage_location')):
            duplicates = InventoryItem.objects.filter(
                user_id=self.instance.user_id, part_id=cleaned['part_id'],
                color=cleaned['color'], storage_location=cleaned['storage_location'],
            ).exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise forms.ValidationError(
                    'This part and colour already exist at this location. Edit the existing item to change its quantity.'
                )
        return cleaned


class ImportCSVForm(forms.Form):
    file = forms.FileField(
        label="Inventory file (CSV/XLS/XLSX)",
        help_text=("Upload a CSV or Excel file with header: "
                   "name,part_id,color,quantity_total,quantity_used,storage_location,image_url,notes. "
                   "Max file size: 10MB"),
        widget=forms.ClearableFileInput(
            attrs={
                "accept": (
                    ".csv,"
                    "text/csv,"
                    "application/vnd.ms-excel,"
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "class": "form-control",
            }
        ),
    )

    def clean_file(self):
        from django.conf import settings
        file = self.cleaned_data.get('file')
        if file:
            # Check file size
            max_size = getattr(settings, 'FILE_UPLOAD_MAX_MEMORY_SIZE', 10 * 1024 * 1024)
            if file.size > max_size:
                raise forms.ValidationError(
                    f"File size exceeds maximum allowed size of {max_size // (1024*1024)}MB."
                )

            # Validate file extension
            allowed_extensions = ['.csv', '.xls', '.xlsx']
            file_name = file.name.lower()
            if not any(file_name.endswith(ext) for ext in allowed_extensions):
                raise forms.ValidationError(
                    "Invalid file type. Only CSV, XLS, and XLSX files are allowed."
                )
        return file


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["username"]
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control", "autocomplete": "username"}),
        }
        help_texts = {
            "username": None,
        }


class InviteCollaboratorForm(forms.Form):
    username = forms.CharField(
        label="Username to invite",
        max_length=150,
        help_text="Enter the username of the user you want to invite"
    )
    can_edit = forms.BooleanField(
        label="Allow editing (add/update items)", required=False, initial=True
    )
    can_delete = forms.BooleanField(
        label="Allow deleting items", required=False, initial=False
    )


class AppConfigForm(forms.ModelForm):
    """Admin-only site settings."""
    class Meta:
        model = AppConfig
        fields = [
            "site_name",
            "allow_registration",
            "default_from_email",
        ]
        widgets = {
            "site_name": forms.TextInput(attrs={"class": "form-control"}),
            "allow_registration": forms.CheckboxInput(),
            "default_from_email": forms.EmailInput(attrs={"class": "form-control"}),
        }


class UserSettingsForm(forms.ModelForm):
    """Per-user settings visible to all authenticated users."""
    class Meta:
        model = UserPreference
        fields = ["items_per_page", "rebrickable_api_key"]
        widgets = {
            "items_per_page": forms.NumberInput(attrs={"class": "form-control", "min": 5, "max": 500, "step": 1}),
            "rebrickable_api_key": forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
        }


class NoteForm(forms.ModelForm):
    """Form for creating and editing user notes."""
    class Meta:
        model = Note
        fields = ["title", "description"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "Note title"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 10, "placeholder": "Write your note here..."}),
        }

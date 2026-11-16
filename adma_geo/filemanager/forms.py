from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from .models import Folder

User = get_user_model()

class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result

class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = ['name', 'is_public']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter folder name'
            }),
            'is_public': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            })
        }

class FileUploadForm(forms.Form):
    files = MultipleFileField(required=True)
    is_public = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input'
        })
    )


class NewPasswordChangeForm(PasswordChangeForm):
    """Django's change password form, refusing to keep the current password.

    An account made by the roster loader starts with a one-time password that was
    printed in a table. Typing it again as the new one would defeat the change.
    """

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get('new_password1')
        if new_password and self.user.check_password(new_password):
            self.add_error('new_password1', 'Choose a password different from your current one.')
        return cleaned_data

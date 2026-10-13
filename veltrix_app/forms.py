from django.contrib.auth.forms import UserCreationForm
from django import forms
from django.contrib.auth import get_user_model

User = get_user_model()


class RegistrationForm(UserCreationForm):
    email = forms.EmailField(required=True)
    first_name = forms.CharField(max_length=60, required=False)
    last_name = forms.CharField(max_length=60, required=False)

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("A user with that email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["email"]
        user.email = self.cleaned_data["email"].lower()
        user.is_active = True
        if commit:
            user.save()
        return user


class SupportForm(forms.Form):
    subject = forms.CharField(max_length=160)
    message = forms.CharField(widget=forms.Textarea)

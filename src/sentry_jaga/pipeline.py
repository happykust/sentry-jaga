"""Integration installation: the form for the Jaga URL and service account."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import requests
from django import forms
from django.core.exceptions import ValidationError
from django.http.request import HttpRequest
from django.http.response import HttpResponseBase

from sentry_jaga.client.api import JagaClient
from sentry_jaga.client.exceptions import JagaApiError, JagaAuthError

if TYPE_CHECKING:
    from sentry.integrations.pipeline import IntegrationPipeline


def verify_credentials(instance_url: str, email: str, password: str) -> None:
    """Check that Jaga is reachable and the service account credentials are valid."""
    client = JagaClient(instance_url=instance_url, email=email, password=password)
    try:
        client.login()
    except JagaAuthError as exc:
        raise ValidationError(
            "Jaga rejected the credentials. Check the email and password of the service account."
        ) from exc
    except JagaApiError as exc:
        raise ValidationError(f"Jaga returned an error on login: {exc}") from exc
    except requests.RequestException as exc:
        raise ValidationError(f"Could not connect to Jaga at {instance_url}: {exc}") from exc


class InstallationForm(forms.Form):
    # `assume_scheme="https"` is not cosmetic: on Django 5.x a URLField without it completes a
    # schemeless address to `http://`, and Sentry does not set FORMS_URLFIELD_ASSUME_HTTPS. An admin
    # types `jaga.example.com`, and the service account password goes out in plain text while the
    # http address settles into `Integration.metadata` forever.
    instance_url = forms.URLField(
        label="Jaga URL",
        assume_scheme="https",
        help_text=(
            "Base address of your Jaga instance, without a path — for example "
            "https://jaga.example.com. Sentry appends the API prefix itself."
        ),
        widget=forms.TextInput(attrs={"placeholder": "https://jaga.example.com"}),
    )
    email = forms.EmailField(
        label="Service account email",
        help_text=(
            "The Jaga account Sentry acts on behalf of. It must be a member of every space "
            "you want to file tasks into."
        ),
        widget=forms.TextInput(attrs={"placeholder": "sentry-bot@example.com"}),
    )
    password = forms.CharField(
        label="Service account password",
        help_text=(
            "Stored by Sentry so it can call Jaga later. The credentials are verified against "
            "Jaga before the integration is created."
        ),
        widget=forms.PasswordInput(render_value=False),
    )

    def clean(self) -> dict[str, Any]:
        # The annotation is for mypy: without Django stubs, `Form.clean()` returns Any.
        data: dict[str, Any] = super().clean()
        instance_url = data.get("instance_url")
        email = data.get("email")
        password = data.get("password")
        if instance_url and email and password:
            verify_credentials(instance_url, email, password)
        return data


class InstallationConfigView:
    """The only installation step: a form with the URL and the credentials."""

    # Not coverable: it needs `render_to_response` and a real `IntegrationPipeline`, neither of
    # which exists in the unit run. It holds no logic of its own — that lives in `InstallationForm`
    # and `verify_credentials`, which are covered.
    def dispatch(  # pragma: no cover
        self,
        request: HttpRequest,
        pipeline: IntegrationPipeline,
    ) -> HttpResponseBase:
        # Deferred import: the form and `verify_credentials` must be importable without sentry.
        from sentry.web.helpers import render_to_response

        if request.method == "POST":
            form = InstallationForm(request.POST)
            if form.is_valid():
                pipeline.bind_state("installation_data", form.cleaned_data)
                return pipeline.next_step()
        else:
            form = InstallationForm()

        return render_to_response(
            template="sentry_jaga/config.html",
            context={"form": form},
            request=request,
        )

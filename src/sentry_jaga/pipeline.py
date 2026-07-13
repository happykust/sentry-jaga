"""Установка интеграции: форма ввода адреса Яги и сервисной учётки."""

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
    """Проверить, что Яга доступна и сервисная учётка валидна."""
    client = JagaClient(instance_url=instance_url, email=email, password=password)
    try:
        client.login()
    except JagaAuthError as exc:
        raise ValidationError(
            "Яга отклонила учётные данные. Проверьте email и пароль сервисного аккаунта."
        ) from exc
    except JagaApiError as exc:
        raise ValidationError(f"Яга вернула ошибку при входе: {exc}") from exc
    except requests.RequestException as exc:
        raise ValidationError(
            f"Не удалось подключиться к Яге по адресу {instance_url}: {exc}"
        ) from exc


class InstallationForm(forms.Form):
    instance_url = forms.URLField(
        label="Адрес Яги",
        help_text="Базовый URL инсталляции Яги, например https://jaga.example.com",
        widget=forms.TextInput(attrs={"placeholder": "https://jaga.example.com"}),
    )
    email = forms.EmailField(
        label="Email сервисного аккаунта",
        help_text="Учётная запись, от имени которой Sentry будет работать с Ягой.",
    )
    password = forms.CharField(
        label="Пароль сервисного аккаунта",
        widget=forms.PasswordInput(),
    )

    def clean(self) -> dict[str, Any]:
        # Аннотация нужна mypy: без stubs у Django `Form.clean()` возвращает Any.
        data: dict[str, Any] = super().clean()
        instance_url = data.get("instance_url")
        email = data.get("email")
        password = data.get("password")
        if instance_url and email and password:
            verify_credentials(instance_url, email, password)
        return data


class InstallationConfigView:
    """Единственный шаг установки: форма с адресом и учёткой."""

    def dispatch(self, request: HttpRequest, pipeline: IntegrationPipeline) -> HttpResponseBase:
        # Импорт отложен: форма и verify_credentials должны быть импортируемы без
        # установленного sentry (их юнит-тесты зависят только от django).
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

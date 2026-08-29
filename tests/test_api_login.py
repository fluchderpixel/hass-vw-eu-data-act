"""Login error classification: 5xx from the identity provider is transient.

VW's identity service intermittently answers the credentials POST with
HTTP 500 (a branded "general error" page). That is a server-side outage, not a
credential problem, so it must surface as a retryable ApiError (with status)
rather than an AuthError — otherwise the coordinator raises
ConfigEntryAuthFailed and Home Assistant demands reauthentication for an error
that would clear on the next poll.
"""
from __future__ import annotations

import pytest

from custom_components.vw_eu_data_act.api import ApiError, AuthError, EudaApiClient

SIGNIN_HTML = """
<form action="/signin-service/v1/client@apps/login/identifier" method="POST">
  <input type="hidden" name="_csrf" value="csrf-1"/>
  <input type="hidden" name="hmac" value="hmac-1"/>
  <input type="hidden" name="relayState" value="relay-1"/>
</form>
"""

AUTHENTICATE_HTML = """
<form method="POST">
  <input type="hidden" name="_csrf" value="csrf-2"/>
  <input type="hidden" name="hmac" value="hmac-2"/>
  <input type="hidden" name="relayState" value="relay-1"/>
</form>
"""

ERROR_500_HTML = """
<!DOCTYPE html>
<html><head><meta name="identitykit" content="generalErrorBranded"/>
<title>Volkswagen ID</title></head><body></body></html>
"""


class FakeResponse:
    def __init__(self, status: int, text: str, url: str) -> None:
        self.status = status
        self._text = text
        self.url = url

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """Serves a scripted queue of responses for get/post alike."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)

    async def get(self, url, **kwargs) -> FakeResponse:
        return self._responses.pop(0)

    def post(self, url, **kwargs) -> FakeResponse:
        return self._responses.pop(0)


def _login_responses(final: FakeResponse) -> list[FakeResponse]:
    """The four responses _do_login consumes: prime, authorize, identifier, credentials."""
    return [
        FakeResponse(200, "", "https://datamanagement.apps.emea.vwapps.io/"),
        FakeResponse(200, SIGNIN_HTML, "https://identity.vwgroup.io/signin-service/v1/x/login"),
        FakeResponse(
            200,
            AUTHENTICATE_HTML,
            "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate?relayState=relay-1",
        ),
        final,
    ]


async def test_login_500_raises_retryable_api_error() -> None:
    session = FakeSession(
        _login_responses(
            FakeResponse(
                500,
                ERROR_500_HTML,
                "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate",
            )
        )
    )
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(ApiError) as excinfo:
        await client.async_login()

    assert not isinstance(excinfo.value, AuthError)
    assert excinfo.value.status == 500


async def test_login_bad_credentials_still_raises_auth_error() -> None:
    # Bad credentials re-render the identity sign-in page with HTTP 200.
    session = FakeSession(
        _login_responses(
            FakeResponse(
                200,
                SIGNIN_HTML,
                "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate",
            )
        )
    )
    client = EudaApiClient(session, "user@example.com", "wrong-password")

    with pytest.raises(AuthError):
        await client.async_login()

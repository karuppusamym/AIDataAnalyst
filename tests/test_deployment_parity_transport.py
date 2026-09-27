"""A restarting deployment is unknown, not a crashed parity checker."""

from http.client import RemoteDisconnected

import pytest

from scripts.check_deployment_parity import http_get_json


@pytest.mark.parametrize("error", [RemoteDisconnected("restarting"), TimeoutError("timed out")])
def test_transport_failure_returns_unknown(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    status, reason = http_get_json("http://localhost:8000", "/openapi.json")
    assert status == 0
    assert str(error) in reason

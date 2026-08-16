from __future__ import annotations

import unittest

from _requests_compat import requests
from services.domain import PlexConnection
from services.plex_gateway import PlexDeleteOutcomeUnknown, PlexGateway


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.text = "{}"


class _Session:
    def __init__(self, outcome) -> None:
        self.headers = {}
        self.outcome = outcome
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class DeleteTransportUncertaintyTest(unittest.TestCase):
    def _gateway(self, outcome):
        session = _Session(outcome)
        gateway = PlexGateway(
            PlexConnection("http://plex.local:32400", "machine-1", "top-secret"),
            session=session,
        )
        return gateway, session

    def test_connection_reset_after_delete_is_unknown_and_never_retried(self) -> None:
        gateway, session = self._gateway(requests.ConnectionError("connection reset"))
        with self.assertRaises(PlexDeleteOutcomeUnknown):
            gateway.delete_media("100", "10")
        self.assertEqual(len(session.calls), 1)

    def test_non_success_delete_response_is_unknown_and_never_retried(self) -> None:
        gateway, session = self._gateway(_Response(500))
        with self.assertRaises(PlexDeleteOutcomeUnknown):
            gateway.delete_media("100", "10")
        self.assertEqual(len(session.calls), 1)

    def test_delete_never_puts_token_in_url_or_query(self) -> None:
        gateway, session = self._gateway(_Response(200))
        gateway.delete_media("100", "10")
        call = session.calls[0]
        self.assertNotIn("top-secret", call["url"])
        self.assertNotIn("X-Plex-Token", call["params"])
        self.assertEqual(session.headers["X-Plex-Token"], "top-secret")
        self.assertFalse(call["allow_redirects"])


if __name__ == "__main__":
    unittest.main()

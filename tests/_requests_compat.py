"""Provide the tiny requests surface needed by tests in the bundled runtime."""

import sys
import types


try:
    import requests as requests  # type: ignore
    if any(
        not hasattr(requests, name)
        for name in ("Session", "Response", "Timeout", "ConnectionError", "RequestException")
    ):
        raise ImportError("incomplete requests module")
except (ModuleNotFoundError, ImportError):
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class Session(object):
        def __init__(self):
            self.headers = {}

        def request(self, **kwargs):
            raise NotImplementedError

    requests.RequestException = RequestException
    requests.Timeout = Timeout
    requests.ConnectionError = ConnectionError
    requests.Session = Session
    requests.Response = object
    sys.modules["requests"] = requests

from __future__ import annotations

import json
import unittest

from _requests_compat import requests

from services.domain import LibrarySection, PlexConnection
from services.plex_gateway import (
    PlexDeleteOutcomeUnknown,
    PlexGateway,
    PlexGatewayError,
    parse_metadata,
)


class FakeResponse:
    def __init__(self, payload=None, status=200, content_type="application/json", text=None):
        self.payload = payload
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def metadata_payload():
    return {
        "MediaContainer": {
            "Metadata": [
                {
                    "ratingKey": "100",
                    "guid": "plex://movie/example",
                    "type": "movie",
                    "title": "Example",
                    "year": 2024,
                    "Media": [
                        {
                            "id": "10",
                            "duration": 1000,
                            "bitrate": 5000,
                            "width": 1920,
                            "height": 1080,
                            "videoResolution": "1080",
                            "videoCodec": "h264",
                            "audioCodec": "aac",
                            "audioChannels": 2,
                            "container": "mkv",
                            "Part": [
                                {
                                    "id": "101",
                                    "file": "/media/a.mkv",
                                    "size": 100,
                                    "duration": 1000,
                                    "container": "mkv",
                                    "exists": 1,
                                    "Stream": [
                                        {"streamType": 2, "codec": "ac3", "channels": 6, "language": "eng"}
                                    ],
                                }
                            ],
                        },
                        {"id": "20", "Part": [{"id": "201", "file": "/media/b.mkv", "size": 200}]},
                    ],
                }
            ]
        }
    }


class GatewayTests(unittest.TestCase):
    def connection(self):
        return PlexConnection("http://plex.local:32400", "machine-1", "secret-token")

    def test_identity_and_sections_keep_token_in_header(self):
        session = FakeSession(
            [
                FakeResponse({"MediaContainer": {"machineIdentifier": "machine-1", "version": "1.40"}}),
                FakeResponse(
                    {
                        "MediaContainer": {
                            "Directory": [
                                {"key": "1", "title": "Movies", "type": "movie"},
                                {"key": "2", "title": "Shows", "type": "show"},
                                {"key": "3", "title": "Music", "type": "artist"},
                            ]
                        }
                    }
                ),
            ]
        )
        gateway = PlexGateway(self.connection(), session=session)
        self.assertEqual(gateway.validate_identity("machine-1").version, "1.40")
        self.assertEqual([item.key for item in gateway.list_sections()], ["1", "2"])
        self.assertEqual(session.headers["X-Plex-Token"], "secret-token")
        self.assertNotIn("X-Plex-Token", session.calls[0]["params"])
        self.assertNotIn("secret-token", session.calls[0]["url"])

    def test_machine_mismatch_is_rejected(self):
        session = FakeSession([FakeResponse({"MediaContainer": {"machineIdentifier": "other"}})])
        with self.assertRaises(PlexGatewayError):
            PlexGateway(self.connection(), session=session).validate_identity("machine-1")

    def test_duplicate_search_paginates_and_deduplicates_keys(self):
        session = FakeSession(
            [
                FakeResponse({"MediaContainer": {"totalSize": 3, "Metadata": [{"ratingKey": "10"}, {"ratingKey": "11"}]}}),
                FakeResponse({"MediaContainer": {"totalSize": 3, "Metadata": [{"ratingKey": "11"}]}}),
            ]
        )
        gateway = PlexGateway(self.connection(), session=session)
        keys = gateway.duplicate_rating_keys(LibrarySection("1", "Movies", "movie"), page_size=2)
        self.assertEqual(keys, ["10", "11"])
        self.assertEqual(session.calls[0]["params"]["X-Plex-Container-Start"], "0")
        self.assertEqual(session.calls[1]["params"]["X-Plex-Container-Start"], "2")

    def test_get_metadata_normalizes_parts_and_audio_streams(self):
        session = FakeSession([FakeResponse(metadata_payload())])
        item = PlexGateway(self.connection(), session=session).get_metadata("100")
        self.assertEqual(item.rating_key, "100")
        self.assertEqual(len(item.media), 2)
        self.assertEqual(item.media[0].parts[0].file, "/media/a.mkv")
        self.assertEqual(item.media[0].audio_tracks[0].channels, 6)

    def test_xml_fallback_is_supported(self):
        xml = '<MediaContainer machineIdentifier="machine-1" version="1.40"></MediaContainer>'
        session = FakeSession([FakeResponse(status=200, content_type="application/xml", text=xml)])
        identity = PlexGateway(self.connection(), session=session).identity()
        self.assertEqual(identity.machine_id, "machine-1")

    def test_delete_timeout_is_unknown_and_not_retried(self):
        session = FakeSession([requests.Timeout("read timeout")])
        gateway = PlexGateway(self.connection(), session=session)
        with self.assertRaises(PlexDeleteOutcomeUnknown):
            gateway.delete_media("100", "10")
        self.assertEqual(len(session.calls), 1)

    def test_ids_are_validated_before_request(self):
        session = FakeSession([])
        gateway = PlexGateway(self.connection(), session=session)
        with self.assertRaises(PlexGatewayError):
            gateway.get_metadata("../identity")
        with self.assertRaises(PlexGatewayError):
            gateway.delete_media("100", "x")
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()

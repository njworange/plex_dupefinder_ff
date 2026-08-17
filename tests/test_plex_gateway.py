from __future__ import annotations

import json
import unittest

from _requests_compat import requests

from services.domain import LibrarySection, PlexConnection
from services.plex_gateway import (
    PlexDeleteOutcomeUnknown,
    PlexGateway,
    PlexGatewayError,
    PlexHTTPError,
    PlexMetadataNotFound,
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

    def test_identity_mismatch_can_be_classified_by_caller_without_retry(self):
        session = FakeSession(
            [FakeResponse({"MediaContainer": {"machineIdentifier": "other"}})]
        )
        identity = PlexGateway(self.connection(), session=session).validate_identity(
            "machine-1", require_match=False
        )
        self.assertEqual(identity.machine_id, "other")

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

    def test_metadata_not_found_has_local_stale_item_taxonomy(self):
        for response in (
            FakeResponse(status=404),
            FakeResponse(status=410),
            FakeResponse({"MediaContainer": {}}),
        ):
            with self.subTest(status=response.status_code):
                gateway = PlexGateway(
                    self.connection(), session=FakeSession([response])
                )
                with self.assertRaises(PlexMetadataNotFound):
                    gateway.get_metadata("100")

        gateway = PlexGateway(
            self.connection(), session=FakeSession([FakeResponse(status=500)])
        )
        with self.assertRaises(PlexHTTPError) as error:
            gateway.get_metadata("100")
        self.assertEqual(error.exception.status_code, 500)

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

    def test_partial_refresh_uses_section_path_and_keeps_token_in_header(self):
        session = FakeSession([FakeResponse(status=200)])
        gateway = PlexGateway(self.connection(), session=session)

        status = gateway.refresh_section_path("7", "/library/tv/Example Show")

        self.assertEqual(status, 200)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith("/library/sections/7/refresh"))
        self.assertEqual(call["params"], {"path": "/library/tv/Example Show"})
        self.assertEqual(session.headers["X-Plex-Token"], "secret-token")
        self.assertNotIn("X-Plex-Token", call["params"])
        self.assertNotIn("secret-token", call["url"])

    def test_section_locations_come_from_the_selected_live_library(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "MediaContainer": {
                            "Directory": [
                                {
                                    "key": "7",
                                    "title": "Shows",
                                    "type": "show",
                                    "Location": [
                                        {"id": "1", "path": "/library/tv"},
                                        {"id": "2", "path": "/archive/tv"},
                                    ],
                                }
                            ]
                        }
                    }
                )
            ]
        )

        locations = PlexGateway(self.connection(), session=session).section_locations("7")

        self.assertEqual(locations, ["/library/tv", "/archive/tv"])
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertTrue(session.calls[0]["url"].endswith("/library/sections"))
        self.assertNotIn("secret-token", session.calls[0]["url"])

    def test_partial_refresh_rejects_invalid_section_or_path_before_request(self):
        session = FakeSession([])
        gateway = PlexGateway(self.connection(), session=session)

        for section, path in (("../7", "/library/tv"), ("7", ""), ("7", "relative")):
            with self.subTest(section=section, path=path):
                with self.assertRaises(PlexGatewayError):
                    gateway.refresh_section_path(section, path)

        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()

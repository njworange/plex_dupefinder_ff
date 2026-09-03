from __future__ import annotations

import unittest

from services import (
    LibrarySection,
    PlexConnection,
    PlexDeleteUncertainError,
    PlexGateway,
    PlexIdentityMismatch,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = content.decode("utf-8", errors="replace") if content else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def metadata_payload(media):
    return {
        "MediaContainer": {
            "size": 1,
            "Metadata": [
                {
                    "ratingKey": "42",
                    "guid": "plex://movie/abc",
                    "type": "movie",
                    "title": "Example",
                    "year": 2024,
                    "Media": media,
                }
            ],
        }
    }


class PlexGatewayTests(unittest.TestCase):
    def connection(self):
        return PlexConnection(
            base_url="http://plex.local:32400/", token="token-value", machine_id="m1"
        )

    def test_token_is_header_timeout_is_bounded_and_redirects_are_disabled(self):
        session = FakeSession(
            FakeResponse(
                payload={
                    "MediaContainer": {
                        "machineIdentifier": "m1",
                        "version": "1.40",
                        "allowMediaDeletion": True,
                    }
                }
            )
        )
        gateway = PlexGateway(self.connection(), timeout=(2, 9), session=session)

        identity = gateway.validate_identity("m1", require_match=True)

        self.assertEqual(identity.machine_id, "m1")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertNotIn("token-value", url)
        self.assertEqual(kwargs["headers"]["X-Plex-Token"], "token-value")
        self.assertEqual(kwargs["timeout"], (2.0, 9.0))
        self.assertFalse(kwargs["allow_redirects"])

    def test_identity_mismatch_can_be_required(self):
        session = FakeSession(
            FakeResponse(payload={"MediaContainer": {"machineIdentifier": "other"}})
        )
        gateway = PlexGateway(self.connection(), session=session)
        with self.assertRaises(PlexIdentityMismatch):
            gateway.validate_identity("m1", require_match=True)

    def test_duplicate_keys_are_paginated_and_section_type_is_sent(self):
        session = FakeSession(
            FakeResponse(
                payload={
                    "MediaContainer": {
                        "size": 1,
                        "totalSize": 2,
                        "Metadata": [{"ratingKey": "41"}],
                    }
                }
            ),
            FakeResponse(
                payload={
                    "MediaContainer": {
                        "size": 1,
                        "totalSize": 2,
                        "Metadata": [{"ratingKey": "42"}],
                    }
                }
            ),
        )
        gateway = PlexGateway(self.connection(), session=session)

        keys = gateway.duplicate_rating_keys(LibrarySection("3", "Movies", "movie"))

        self.assertEqual(keys, ("41", "42"))
        self.assertEqual(session.calls[0][2]["params"]["type"], 1)
        self.assertEqual(session.calls[1][2]["params"]["X-Plex-Container-Start"], 1)

    def test_string_section_id_is_resolved_to_episode_type(self):
        session = FakeSession(
            FakeResponse(
                payload={
                    "MediaContainer": {
                        "Directory": [
                            {"key": "3", "title": "TV", "type": "show"}
                        ]
                    }
                }
            ),
            FakeResponse(
                payload={
                    "MediaContainer": {
                        "size": 1,
                        "totalSize": 1,
                        "Metadata": [{"ratingKey": "42"}],
                    }
                }
            ),
        )
        gateway = PlexGateway(self.connection(), session=session)

        keys = gateway.duplicate_rating_keys("3")

        self.assertEqual(keys, ("42",))
        self.assertTrue(session.calls[0][1].endswith("/library/sections"))
        self.assertEqual(session.calls[1][2]["params"]["type"], 4)

    def test_metadata_parser_supports_multipart_and_audio_streams(self):
        session = FakeSession(
            FakeResponse(
                payload=metadata_payload(
                    [
                        {
                            "id": "10",
                            "duration": 120000,
                            "bitrate": 8000,
                            "width": 1920,
                            "height": 1080,
                            "videoResolution": "1080",
                            "videoCodec": "h264",
                            "audioCodec": "aac",
                            "Part": [
                                {
                                    "id": "101",
                                    "file": "/media/Example.CD1.mkv",
                                    "size": 100,
                                    "Stream": [
                                        {"streamType": 1, "codec": "h264"},
                                        {
                                            "streamType": 2,
                                            "codec": "flac",
                                            "channels": 6,
                                        },
                                    ],
                                },
                                {
                                    "id": "102",
                                    "file": "/media/Example.CD2.mkv",
                                    "size": 200,
                                },
                            ],
                        },
                        {
                            "id": "11",
                            "Part": [{"id": "103", "file": "/media/Example.mp4"}],
                        },
                    ]
                )
            )
        )
        group = PlexGateway(self.connection(), session=session).get_metadata("42")

        self.assertTrue(group.is_duplicate)
        self.assertTrue(group.candidates[0].multipart)
        self.assertEqual(group.candidates[0].total_size, 300)
        self.assertEqual(group.candidates[0].audio_tracks[0].codec, "flac")

    def test_duplicate_groups_stream_metadata_one_group_at_a_time(self):
        duplicate_media = [
            {"id": "10", "Part": [{"id": "1", "file": "/a.mkv"}]},
            {"id": "11", "Part": [{"id": "2", "file": "/b.mkv"}]},
        ]
        session = FakeSession(
            FakeResponse(
                payload={
                    "MediaContainer": {
                        "size": 2,
                        "totalSize": 2,
                        "Metadata": [{"ratingKey": "41"}, {"ratingKey": "42"}],
                    }
                }
            ),
            FakeResponse(payload=metadata_payload(duplicate_media)),
            FakeResponse(payload=metadata_payload(duplicate_media)),
        )
        gateway = PlexGateway(self.connection(), session=session)

        iterator = gateway.iter_duplicate_groups(LibrarySection("3", "Movies", "movie"))
        first = next(iterator)

        self.assertTrue(first.is_duplicate)
        self.assertEqual(len(session.calls), 2)  # list + first metadata only
        second = next(iterator)
        self.assertTrue(second.is_duplicate)
        self.assertEqual(len(session.calls), 3)

    def test_dry_run_has_no_request_and_delete_is_once_then_metadata_can_be_read(self):
        session = FakeSession(
            FakeResponse(status_code=200),
            FakeResponse(
                payload=metadata_payload(
                    [{"id": "10", "Part": [{"id": "1", "file": "/a.mkv"}]}]
                )
            ),
        )
        gateway = PlexGateway(self.connection(), session=session)

        dry_receipt = gateway.delete_media("42", "11", dry_run=True)
        receipt = gateway.delete_media("42", "11")
        refreshed = gateway.get_metadata("42")

        self.assertTrue(dry_receipt.dry_run)
        self.assertEqual(receipt.status_code, 200)
        self.assertIsNotNone(refreshed.candidate("10"))
        self.assertIsNone(refreshed.candidate("11"))
        self.assertEqual([call[0] for call in session.calls], ["DELETE", "GET"])

    def test_delete_transport_error_is_not_retried(self):
        session = FakeSession(TimeoutError("timeout"), FakeResponse(status_code=200))
        gateway = PlexGateway(self.connection(), session=session)

        with self.assertRaises(PlexDeleteUncertainError):
            gateway.delete_media("42", "11")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], "DELETE")


if __name__ == "__main__":
    unittest.main()

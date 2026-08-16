from services.domain import MediaPart, MediaVersion, MetadataItem


def version(media_id, path, bitrate=1000, resolution="1080", size=1000):
    return MediaVersion(
        media_id=str(media_id),
        duration=7200000,
        bitrate=bitrate,
        width=1920 if resolution == "1080" else 3840,
        height=1080 if resolution == "1080" else 2160,
        video_resolution=resolution,
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        container="mkv",
        parts=(MediaPart(str(media_id) + "1", path, size=size, duration=7200000, container="mkv"),),
    )


def movie_item(versions=None):
    return MetadataItem(
        rating_key="100",
        guid="plex://movie/review-test",
        media_type="movie",
        title="Review Test",
        year=2024,
        media=tuple(
            versions
            or (
                version("10", "/media/movies/review-1080.mkv"),
                version("20", "/media/movies/review-4k.mkv", 2000, "4k", 2000),
            )
        ),
    )

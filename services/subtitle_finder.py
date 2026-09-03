"""Locate and remove exact-stem external subtitle sidecars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Tuple, Union

from .domain import MediaCandidate


DEFAULT_SUBTITLE_EXTENSIONS = (
    ".srt",
    ".smi",
    ".ssa",
    ".ass",
    ".vtt",
    ".sub",
    ".idx",
    ".sup",
)
DEFAULT_SUBTITLE_DIRS = ("Subs", "Subtitles")


def normalize_extensions(extensions: Iterable[str]) -> Tuple[str, ...]:
    normalised = {
        "." + str(item).strip().casefold().lstrip(".")
        for item in extensions
        if str(item).strip().lstrip(".")
    }
    if not normalised:
        raise ValueError("at least one subtitle extension is required")
    return tuple(sorted(normalised))


def _normal_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def find_sidecars(
    video_paths: Union[str, os.PathLike, Iterable[Union[str, os.PathLike]]],
    extensions: Iterable[str] = DEFAULT_SUBTITLE_EXTENSIONS,
    subtitle_dirs: Sequence[str] = DEFAULT_SUBTITLE_DIRS,
) -> Tuple[str, ...]:
    """Find sidecars beside each video and one level in Subs/Subtitles.

    A match must have exactly the video's stem, optionally followed by dot
    separated subtitle qualifiers, and a whitelisted final extension.  The
    function never recurses and ignores symlinks.
    """

    if isinstance(video_paths, (str, os.PathLike)):
        videos = (Path(video_paths),)
    else:
        videos = tuple(Path(item) for item in video_paths)
    allowed = frozenset(normalize_extensions(extensions))
    wanted_dirs = {str(item).casefold() for item in subtitle_dirs}
    found: Dict[str, str] = {}

    for video in videos:
        parent = video.parent
        locations = [parent]
        try:
            locations.extend(
                item
                for item in parent.iterdir()
                if item.is_dir()
                and not item.is_symlink()
                and item.name.casefold() in wanted_dirs
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue

        expected_stem = video.stem.casefold()
        for location in locations:
            try:
                entries = tuple(location.iterdir())
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                continue
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_file():
                        continue
                except OSError:
                    continue
                if entry.suffix.casefold() not in allowed:
                    continue
                subtitle_stem = entry.name[: -len(entry.suffix)].casefold()
                if subtitle_stem != expected_stem and not subtitle_stem.startswith(
                    expected_stem + "."
                ):
                    continue
                key = _normal_path(entry)
                found.setdefault(key, os.fspath(entry.absolute()))

    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True)
class SubtitleDeleteResult:
    planned: Tuple[str, ...]
    deleted: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    failed: Tuple[Tuple[str, str], ...] = ()
    dry_run: bool = True

    @property
    def ok(self) -> bool:
        return not self.failed


def delete_sidecars(
    paths: Iterable[Union[str, os.PathLike]],
    *,
    dry_run: bool = True,
    unlink: Callable[[str], None] = os.unlink,
) -> SubtitleDeleteResult:
    planned = tuple(dict.fromkeys(os.path.abspath(os.fspath(item)) for item in paths))
    if dry_run:
        return SubtitleDeleteResult(planned=planned, dry_run=True)

    deleted: List[str] = []
    missing: List[str] = []
    failed: List[Tuple[str, str]] = []
    for path in planned:
        try:
            unlink(path)
            deleted.append(path)
        except FileNotFoundError:
            missing.append(path)
        except OSError as exc:
            failed.append((path, str(exc)))
    return SubtitleDeleteResult(
        planned=planned,
        deleted=tuple(deleted),
        missing=tuple(missing),
        failed=tuple(failed),
        dry_run=False,
    )


class SubtitleFinder:
    def __init__(
        self,
        extensions: Iterable[str] = DEFAULT_SUBTITLE_EXTENSIONS,
        subtitle_dirs: Sequence[str] = DEFAULT_SUBTITLE_DIRS,
        *,
        unlink: Callable[[str], None] = os.unlink,
    ) -> None:
        self.extensions = normalize_extensions(extensions)
        self.subtitle_dirs = tuple(subtitle_dirs)
        self._unlink = unlink

    def find(
        self,
        video_paths: Union[str, os.PathLike, Iterable[Union[str, os.PathLike]]],
    ) -> Tuple[str, ...]:
        return find_sidecars(video_paths, self.extensions, self.subtitle_dirs)

    def find_for_candidate(self, candidate: MediaCandidate) -> Tuple[str, ...]:
        return self.find(candidate.paths)

    snapshot = find
    snapshot_for_candidate = find_for_candidate

    def delete(
        self, paths: Iterable[Union[str, os.PathLike]], *, dry_run: bool = True
    ) -> SubtitleDeleteResult:
        return delete_sidecars(paths, dry_run=dry_run, unlink=self._unlink)

    def delete_for_candidate(
        self, candidate: MediaCandidate, *, dry_run: bool = True
    ) -> SubtitleDeleteResult:
        return self.delete(self.find_for_candidate(candidate), dry_run=dry_run)

    unlink_sidecars = delete_for_candidate
    delete_snapshot = delete


__all__ = [
    "DEFAULT_SUBTITLE_DIRS",
    "DEFAULT_SUBTITLE_EXTENSIONS",
    "SubtitleDeleteResult",
    "SubtitleFinder",
    "delete_sidecars",
    "find_sidecars",
    "normalize_extensions",
]

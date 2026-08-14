"""Opt-in synchronization of canonical EmulationStation library metadata.

Raw ``gamelist.xml`` files are import/render surfaces, never the synchronized
object.  The canonical JSON document is keyed by a deterministic identity
derived from catalog system + primary source path.  Merge is additive:
existing canonical non-empty values win conflicts and blank input never
deletes data.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import posixpath
import shutil
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from romcloud.core.exceptions import (
    LibrarySyncConnectivityError,
    LibrarySyncError,
)
from romcloud.core.capabilities import Capability, CapabilityPolicy
from romcloud.core.models.game import Game
from romcloud.core.models.librarysync import LibraryImportPreview, LibrarySyncReport
from romcloud.core.models.proxy import ProxyRecord
from romcloud.core.progress import ProgressSink, emit_progress
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository

SCHEMA_VERSION = 1
OWNERSHIP_TAG = "romcloudId"
LOCAL_MEDIA_DIR = ".romcloud-media"
CANONICAL_FILENAME = "library.json"
STATE_FILENAME = "library-sync-state.json"
FINGERPRINT_VERSION = 1
FINGERPRINT_SAMPLE_BYTES = 64 * 1024

METADATA_TAGS = frozenset(
    {
        "name", "sortname", "desc", "rating", "releasedate", "developer",
        "publisher", "genre", "players", "lang", "region", "favorite",
        "kidgame", "hidden", "playcount", "lastplayed",
    }
)
MEDIA_TAGS = frozenset(
    {"image", "thumbnail", "video", "marquee", "fanart", "manual", "boxback", "bezel", "wheel"}
)


class _DirectoryFileIndex:
    """Cache ordinary-file membership with one listing per parent directory."""

    def __init__(self, report: LibrarySyncReport) -> None:
        self._report = report
        self._files_by_directory: dict[Path, set[str]] = {}

    def contains(self, path: Path) -> bool:
        self._report.media_presence_checks += 1
        directory = path.parent
        files = self._files_by_directory.get(directory)
        if files is None:
            self._report.media_directories_listed += 1
            try:
                with os.scandir(directory) as entries:
                    files = {
                        entry.name
                        for entry in entries
                        if entry.is_file(follow_symlinks=False)
                    }
            except (FileNotFoundError, NotADirectoryError):
                files = set()
            except OSError as exc:
                raise LibrarySyncError(
                    f"Cannot enumerate media directory {directory}: {exc}"
                ) from exc
            self._files_by_directory[directory] = files
        return path.name in files

    def add(self, path: Path) -> None:
        files = self._files_by_directory.get(path.parent)
        if files is not None:
            files.add(path.name)


def library_id_for_game(game: Game) -> str:
    """Cross-device identity independent of mount root and presentation path."""
    primary = game.primary_asset
    if primary is None:
        raise LibrarySyncError(f"Game {game.id} has no primary asset")
    rel = _source_relative(game)
    material = "\0".join(("romcloud-library-v1", game.system.casefold(), rel))
    return hashlib.sha256(unicodedata.normalize("NFC", material).encode("utf-8")).hexdigest()


def _source_relative(game: Game) -> str:
    primary = game.primary_asset
    assert primary is not None
    parts = PurePosixPath(primary.relative_path.replace("\\", "/")).parts
    if parts and parts[0].casefold() == game.system.casefold():
        parts = parts[1:]
    normalized = _safe_relative(PurePosixPath(*parts).as_posix()) if parts else None
    if normalized is None:
        raise LibrarySyncError(f"Unsafe empty primary path for {game.id}")
    return normalized


def _empty_dataset() -> dict:
    return {"schema_version": SCHEMA_VERSION, "records": {}}


def _safe_relative(text: str, *, strip_romcloud: bool = False) -> Optional[str]:
    raw = (text or "").strip().replace("\\", "/")
    if not raw or raw.startswith(("/", "~")) or "://" in raw:
        return None
    while raw.startswith("./"):
        raw = raw[2:]
    normalized = posixpath.normpath(raw)
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        return None
    parts = PurePosixPath(normalized).parts
    if strip_romcloud and parts and parts[0].casefold() == "romcloud":
        parts = parts[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return PurePosixPath(*parts).as_posix()


def _read_dataset(path: Path) -> dict:
    if not path.exists():
        return _empty_dataset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LibrarySyncError(f"Cannot read canonical library {path}: {exc}") from exc
    if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("records"), dict):
        raise LibrarySyncError(f"Unsupported canonical library format: {path}")
    return payload


def _write_dataset(path: Path, dataset: dict) -> bool:
    content = json.dumps(dataset, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    atomic_write_text(path, content)
    # Treat a failed/read-incomplete destination write as a failed commit.
    if _read_dataset(path) != dataset:
        raise LibrarySyncError(f"Canonical library verification failed: {path}")
    return True


class LibrarySyncService:
    def __init__(
        self,
        *,
        enabled: bool,
        provider: Optional[StorageProvider],
        connectivity_root: Optional[str],
        source_root: str,
        local_roms_root: str,
        data_root: str,
        remote_root: Optional[str],
        game_access_mode: str,
        game_repo: GameRepository,
        proxy_repo: ProxyRepository,
        capability_policy: Optional[CapabilityPolicy] = None,
    ) -> None:
        self.enabled = enabled
        self._provider = provider
        self._connectivity_root = connectivity_root
        self._source_root = Path(source_root)
        self._local_roms_root = Path(local_roms_root)
        self._local_root = Path(data_root) / "library"
        self._remote_root = Path(remote_root) if remote_root else None
        self._state_path = Path(data_root) / STATE_FILENAME
        self._mode = game_access_mode
        self._games = game_repo
        self._proxies = proxy_repo
        self._capabilities = capability_policy or CapabilityPolicy("smart_cache")

    @property
    def is_remote_configured(self) -> bool:
        return self._provider is not None and self._remote_root is not None

    def is_remote_reachable(self) -> bool:
        return bool(
            self._provider is not None
            and self._connectivity_root
            and self._provider.is_reachable(self._connectivity_root)
        )

    def status(self) -> dict[str, object]:
        state: dict[str, object] = {}
        try:
            if self._state_path.is_file():
                loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state = loaded
        except (OSError, ValueError):
            pass
        return {
            "enabled": self.enabled,
            "remote_configured": self.is_remote_configured,
            "remote_reachable": self.is_remote_reachable() if self.enabled else False,
            "last_sync": state.get("last_sync"),
            "last_direction": state.get("direction"),
            "last_report": state.get("report"),
        }

    def pull(self, *, full: bool = False) -> LibrarySyncReport:
        """Pull metadata; copy missing payloads or explicitly repair all."""
        return self._run("pull", write_remote=False, full=full)

    def push(self, *, full: bool = False) -> LibrarySyncReport:
        """Push metadata; copy missing payloads or explicitly repair all."""
        return self._run("push", write_remote=True, full=full)

    def sync(
        self, progress: ProgressSink = None, *, full: bool = False
    ) -> LibrarySyncReport:
        """Merge both ways using Quick presence checks unless ``full``."""
        return self._run("sync", write_remote=True, progress=progress, full=full)

    def preview_source_import(self) -> LibraryImportPreview:
        """Inspect XML only; never resolve, hash, or copy referenced media."""
        self._require_available()
        games = self._games.list_all()
        systems = tuple(sorted({game.system for game in games}))
        files = 0
        xml_bytes = 0
        artwork = 0
        videos = 0
        other_media = 0
        artwork_tags = MEDIA_TAGS - {"video", "manual"}
        for system in systems:
            for path in (
                self._source_root / system / "gamelist.xml",
                self._local_roms_root / system / "gamelist.xml",
            ):
                if not path.is_file() or path.is_symlink():
                    continue
                files += 1
                try:
                    raw = path.read_bytes()
                    xml_bytes += len(raw)
                    root = ET.fromstring(raw)
                except (OSError, ET.ParseError):
                    continue
                for element in root.findall("game"):
                    for child in element:
                        if not (child.text or "").strip():
                            continue
                        if child.tag == "video":
                            videos += 1
                        elif child.tag in artwork_tags:
                            artwork += 1
                        elif child.tag in MEDIA_TAGS:
                            other_media += 1
        return LibraryImportPreview(
            games_eligible=len(games),
            systems=systems,
            gamelist_files=files,
            gamelist_bytes=xml_bytes,
            media_references=artwork + videos + other_media,
            artwork_references=artwork,
            video_references=videos,
            other_media_references=other_media,
        )

    def render_local(self) -> LibrarySyncReport:
        """Regenerate presentation from local state without fetching media.

        This reconciliation path is used by operating-mode and lifecycle work.
        Expensive canonical-media materialization belongs only to an explicit
        Library Sync operation (``pull``, ``push``, or ``sync``).
        """
        if not self.enabled:
            raise LibrarySyncError("Library Sync is disabled; enable it in configuration first.")
        report = LibrarySyncReport(direction="render", reconciliation="local")
        validation = self._read_media_validation()
        report.rendered = self._render_local(
            _read_dataset(self._local_root / CANONICAL_FILENAME),
            report,
            media_validation=validation,
            materialize_media=False,
        )
        self._write_media_validation(validation)
        return report

    def remove_local_metadata(self) -> int:
        """Remove only entries carrying ROMCloud's ownership marker."""
        removed = 0
        for system in self._games.list_systems():
            path = self._local_roms_root / system / "gamelist.xml"
            if not path.is_file() or path.is_symlink():
                continue
            existing = path.read_text(encoding="utf-8")
            try:
                root = ET.fromstring(existing)
            except ET.ParseError:
                continue
            system_removed = 0
            for element in list(root.findall("game")):
                if (element.findtext(OWNERSHIP_TAG) or "").strip():
                    root.remove(element)
                    removed += 1
                    system_removed += 1
            if system_removed:
                ET.indent(root, space="  ")
                result = '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
                if result != existing:
                    atomic_write_text(path, result)
        return removed

    def _require_available(self) -> None:
        self._capabilities.require(Capability.LIBRARY_SYNC, "Library Sync")
        if not self.enabled:
            raise LibrarySyncError("Library Sync is disabled; enable it in configuration first.")
        if not self.is_remote_configured:
            raise LibrarySyncConnectivityError(
                "Library Sync requires configured writable ROMCloud data storage."
            )
        if not self.is_remote_reachable():
            raise LibrarySyncConnectivityError(
                f"ROMCloud data storage is not reachable: {self._connectivity_root}"
            )

    def _run(
        self,
        direction: str,
        *,
        write_remote: bool,
        progress: ProgressSink = None,
        full: bool = False,
    ) -> LibrarySyncReport:
        self._require_available()
        assert self._remote_root is not None
        report = LibrarySyncReport(
            direction=direction,
            reconciliation="full" if full else "quick",
        )
        media_validation = self._read_media_validation()
        media_presence = _DirectoryFileIndex(report)
        remote_path = self._remote_root / CANONICAL_FILENAME
        local_path = self._local_root / CANONICAL_FILENAME
        lock = _exclusive_lock(self._remote_root / ".library-sync.lock") if write_remote else nullcontext()
        with lock:
            remote = _read_dataset(remote_path)
            local = _read_dataset(local_path)

            # Remote is authoritative for existing non-empty values. Local/source
            # input may only fill gaps; conflicts are retained and surfaced.
            merged = json.loads(json.dumps(remote))
            self._merge(merged, local, report, "local canonical")
            emit_progress(
                progress,
                "library_sync",
                "metadata",
                "running",
                "Reading source metadata…",
            )
            imported, origins = self._import_gamelists(report, progress)
            self._merge(merged, imported, report, "gamelist import")
            self._seed_catalog_records(merged, report)
            if write_remote:
                self._materialize_remote_media(
                    merged,
                    origins,
                    report,
                    progress,
                    verify_existing=full,
                    media_presence=media_presence,
                )
                _write_dataset(remote_path, merged)
        _write_dataset(local_path, merged)
        report.rendered = self._render_local(
            merged,
            report,
            progress,
            media_validation=media_validation,
            verify_existing=full,
            media_presence=media_presence,
        )
        self._write_state(report, media_validation)
        emit_progress(
            progress,
            "library_sync",
            "complete",
            "success",
            "Source metadata import complete",
            current=report.rendered,
            total=report.rendered,
        )
        return report

    def _merge(self, base: dict, incoming: dict, report: LibrarySyncReport, source: str) -> None:
        records = base["records"]
        for library_id, candidate in incoming.get("records", {}).items():
            if library_id not in records:
                records[library_id] = json.loads(json.dumps(candidate))
                report.metadata_added += 1
                report.media_added += len(candidate.get("media", {}))
                continue
            current = records[library_id]
            changed = False
            for section in ("metadata", "media"):
                current_section = current.setdefault(section, {})
                for key, value in candidate.get(section, {}).items():
                    if value in (None, "", {}):
                        continue
                    existing = current_section.get(key)
                    if existing in (None, "", {}):
                        current_section[key] = json.loads(json.dumps(value))
                        changed = True
                        if section == "media":
                            report.media_added += 1
                    elif (
                        section == "media"
                        and isinstance(existing, dict)
                        and isinstance(value, dict)
                    ):
                        nested_changed = False
                        nested_conflict = False
                        for nested_key, nested_value in value.items():
                            if nested_value in (None, ""):
                                continue
                            if existing.get(nested_key) in (None, ""):
                                existing[nested_key] = nested_value
                                nested_changed = True
                            elif existing[nested_key] != nested_value:
                                nested_conflict = True
                        if nested_changed:
                            changed = True
                        elif not nested_conflict:
                            report.unchanged += 1
                        if nested_conflict:
                            report.conflicts.append(
                                f"{library_id[:12]} {key}: kept canonical media over {source}"
                            )
                    elif existing != value:
                        report.conflicts.append(
                            f"{library_id[:12]} {key}: kept canonical value over {source}"
                        )
                    else:
                        report.unchanged += 1
            if changed:
                report.metadata_updated += 1

    def _seed_catalog_records(self, dataset: dict, report: LibrarySyncReport) -> None:
        records = dataset["records"]
        for game in self._games.list_all():
            library_id = library_id_for_game(game)
            record = records.get(library_id)
            if record is None:
                records[library_id] = {
                    "system": game.system,
                    "source_path": _source_relative(game),
                    "metadata": {"name": game.title},
                    "media": {},
                }
                report.metadata_added += 1
            else:
                metadata = record.setdefault("metadata", {})
                if not metadata.get("name"):
                    metadata["name"] = game.title
                    report.metadata_updated += 1

    def _import_gamelists(
        self, report: LibrarySyncReport, progress: ProgressSink = None
    ) -> tuple[dict, dict[tuple[str, str], Path]]:
        dataset = _empty_dataset()
        origins: dict[tuple[str, str], Path] = {}
        games = self._games.list_all()
        by_id = {library_id_for_game(game): game for game in games}
        examined = 0
        for system in sorted({game.system for game in games}):
            system_games = [game for game in games if game.system == system]
            source_xml = self._source_root / system / "gamelist.xml"
            if source_xml.is_file() and not source_xml.is_symlink():
                examined += self._import_one_xml(
                    source_xml, system, system_games, by_id, dataset, origins,
                    report, source=True, progress=progress, examined=examined,
                )
            local_xml = self._local_roms_root / system / "gamelist.xml"
            if local_xml.is_file() and not local_xml.is_symlink():
                examined += self._import_one_xml(
                    local_xml, system, system_games, by_id, dataset, origins,
                    report, source=False, progress=progress, examined=examined,
                )
        return dataset, origins

    def _import_one_xml(
        self,
        path: Path,
        system: str,
        games: list[Game],
        by_id: dict[str, Game],
        dataset: dict,
        origins: dict[tuple[str, str], Path],
        report: LibrarySyncReport,
        *,
        source: bool,
        progress: ProgressSink = None,
        examined: int = 0,
    ) -> int:
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except (OSError, ET.ParseError) as exc:
            report.failures.append(f"Ignored malformed {path}: {exc}")
            return 0
        if root.tag != "gameList":
            report.failures.append(f"Ignored malformed {path}: root must be gameList")
            return 0
        by_source = {_source_relative(game): game for game in games}
        by_proxy: dict[str, Game] = {}
        for game in games:
            record = self._proxies.get(game.id)
            if record is not None:
                try:
                    rel = Path(record.proxy_path).relative_to(self._local_roms_root / system).as_posix()
                except ValueError:
                    continue
                by_proxy[rel] = game

        elements = root.findall("game")
        for index, element in enumerate(elements, start=1):
            marker = (element.findtext(OWNERSHIP_TAG) or "").strip()
            game = by_id.get(marker)
            game_path = _safe_relative(element.findtext("path") or "", strip_romcloud=not source)
            if game is None and game_path:
                game = by_source.get(game_path) if source else (by_proxy.get(game_path) or by_source.get(game_path))
            if game is None:
                continue
            library_id = library_id_for_game(game)
            record = dataset["records"].setdefault(
                library_id,
                {"system": system, "source_path": _source_relative(game), "metadata": {}, "media": {}},
            )
            for child in element:
                tag = child.tag
                value = (child.text or "").strip()
                if not value:
                    continue
                if tag in METADATA_TAGS:
                    existing = record["metadata"].get(tag)
                    if existing in (None, ""):
                        record["metadata"][tag] = value
                    elif existing != value:
                        report.conflicts.append(
                            f"{library_id[:12]} {tag}: kept earlier canonical import value"
                        )
                elif tag in MEDIA_TAGS:
                    rel = _safe_relative(value, strip_romcloud=not source)
                    if rel is None:
                        report.failures.append(f"Ignored unsafe media path in {path}: {value!r}")
                        continue
                    origin_root = self._source_root / system if source or value.replace("\\", "/").lstrip("./").casefold().startswith("romcloud/") else self._local_roms_root / system
                    origin = origin_root / PurePosixPath(rel)
                    if not _within(origin, origin_root) or not origin.is_file() or origin.is_symlink():
                        continue
                    descriptor: dict[str, object] = {}
                    if source or origin_root == self._source_root / system:
                        descriptor["source_path"] = rel
                    existing_media = record["media"].get(tag)
                    if existing_media is None:
                        record["media"][tag] = descriptor
                        origins[(library_id, tag)] = origin
                    elif existing_media != descriptor and descriptor:
                        report.conflicts.append(
                            f"{library_id[:12]} {tag}: kept earlier canonical media import"
                        )
                    else:
                        origins.setdefault((library_id, tag), origin)
            current = examined + index
            if index == len(elements) or current % 100 == 0:
                emit_progress(
                    progress,
                    "library_sync",
                    "metadata",
                    "running",
                    f"{system}: {current:,} metadata entries examined",
                    current=current,
                    metadata={"system": system, "unit": "entries"},
                )
        return len(elements)

    def _materialize_remote_media(
        self,
        dataset: dict,
        origins: dict[tuple[str, str], Path],
        report: LibrarySyncReport,
        progress: ProgressSink = None,
        *,
        verify_existing: bool = False,
        media_presence: Optional[_DirectoryFileIndex] = None,
    ) -> None:
        assert self._remote_root is not None
        media_presence = media_presence or _DirectoryFileIndex(report)
        emit_progress(
            progress,
            "library_sync",
            "media",
            "running",
            f"Examining {len(origins):,} referenced media files…",
            detail="Transfer bytes are counted only when a copy is required.",
            current=0,
            total=len(origins),
            metadata=_media_progress_metadata(report),
        )
        for index, (key, origin) in enumerate(origins.items(), start=1):
            library_id, tag = key
            system = str(dataset["records"][library_id].get("system", ""))
            report.media_examined += 1
            emit_progress(
                progress,
                "library_sync",
                "media",
                "running",
                f"{system}: examining media file {index:,} / {len(origins):,}",
                detail=f"{origin.name} · {_media_progress_detail(report)}",
                current=index - 1,
                total=len(origins),
                metadata={
                    **_media_progress_metadata(report),
                    "system": system,
                    "media_tag": tag,
                },
            )
            media = dataset["records"][library_id]["media"]
            if tag not in media:
                report.media_added += 1
            descriptor = media.setdefault(tag, {})
            expected_digest = descriptor.get("sha256")
            expected_size = descriptor.get("size")
            expected_suffix = descriptor.get("suffix")
            persisted_blob = descriptor.get("blob")
            safe_persisted_blob = (
                _safe_relative(persisted_blob)
                if isinstance(persisted_blob, str)
                else None
            )
            persisted_destination = (
                self._remote_root / safe_persisted_blob
                if safe_persisted_blob is not None
                else None
            )
            if (
                not verify_existing
                and _valid_sha256(expected_digest)
                and isinstance(expected_size, int)
                and expected_size >= 0
                and isinstance(expected_suffix, str)
                and expected_suffix in ("", Path("x" + expected_suffix).suffix)
                and "/" not in expected_suffix
                and "\\" not in expected_suffix
                and safe_persisted_blob == (
                    f"media/sha256/{str(expected_digest)[:2]}/"
                    f"{expected_digest}{expected_suffix}"
                )
                and persisted_destination is not None
                and _within(persisted_destination, self._remote_root)
                and media_presence.contains(persisted_destination)
            ):
                # Quick reconciliation is deliberately presence-only.  The
                # canonical descriptor already identifies the content-addressed
                # target, so an existing ordinary destination is sufficient;
                # do not stat, sample, hash, or open either payload.
                report.media_skipped += 1
                report.unchanged += 1
                outcome = "skipped present"
                emit_progress(
                    progress,
                    "library_sync",
                    "media",
                    "running",
                    f"{system}: media file {index:,} / {len(origins):,} â€” {outcome}",
                    detail=_media_progress_detail(report),
                    current=index,
                    total=len(origins),
                    metadata={
                        **_media_progress_metadata(report),
                        "system": system,
                        "media_tag": tag,
                    },
                )
                continue
            source_unchanged = (
                _valid_sha256(expected_digest)
                and isinstance(expected_size, int)
                and expected_size >= 0
                and _fingerprint_matches(
                    origin,
                    descriptor.get("source_fingerprint"),
                    expected_size=expected_size,
                )
            )
            if source_unchanged:
                digest = str(expected_digest)
                size = int(expected_size)
            else:
                before = _stat_fields(origin)
                digest, size = _hash_file(origin)
                _record_media_hash(report, size)
                after = _stat_fields(origin)
                if not _stat_unchanged(before, after) or after["size"] != size:
                    raise LibrarySyncError(f"Media changed while it was being read: {origin}")

            suffix = origin.suffix.lower() if len(origin.suffix) <= 12 else ""
            source_relative = _relative_media_path(
                origin, self._source_root / system
            )
            same_persisted_source = (
                source_relative is not None
                and descriptor.get("source_path") == source_relative
            )
            if expected_digest not in (None, digest) and not same_persisted_source:
                report.conflicts.append(f"{library_id[:12]} {tag}: kept canonical media")
                outcome = "conflict retained"
            else:
                descriptor.update(
                    {
                        "sha256": digest,
                        "size": size,
                        "suffix": suffix,
                        "blob": f"media/sha256/{digest[:2]}/{digest}{suffix}",
                        "source_fingerprint": (
                            descriptor.get("source_fingerprint")
                            if source_unchanged
                            else _capture_fingerprint(origin, expected_size=size)
                        ),
                    }
                )
                destination = self._remote_root / str(descriptor["blob"])
                blob_unchanged = (
                    media_presence.contains(destination)
                    if not verify_existing
                    else _fingerprint_matches(
                        destination,
                        descriptor.get("blob_fingerprint"),
                        expected_size=size,
                    )
                )
                if blob_unchanged:
                    report.media_skipped += 1
                    report.unchanged += 1
                    outcome = (
                        "skipped present"
                        if not verify_existing
                        else (
                            "skipped unchanged"
                            if source_unchanged
                            else "destination skipped unchanged"
                        )
                    )
                else:
                    destination_matches = False
                    if verify_existing and destination.is_file():
                        destination_digest, destination_size = _hash_file(destination)
                        _record_media_hash(report, destination_size)
                        destination_matches = (destination_digest, destination_size) == (
                            digest,
                            size,
                        )
                    if destination_matches:
                        report.unchanged += 1
                        outcome = "destination verified"
                    else:
                        _copy_verified(origin, destination, digest, size)
                        _record_media_hash(report, size)  # temporary verification
                        _record_media_copy(report, size)
                        media_presence.add(destination)
                        outcome = "copied and verified"
                    if verify_existing:
                        descriptor["blob_fingerprint"] = _capture_fingerprint(
                            destination, expected_size=size
                        )

            emit_progress(
                progress,
                "library_sync",
                "media",
                "running",
                f"{system}: media file {index:,} / {len(origins):,} — {outcome}",
                detail=_media_progress_detail(report),
                current=index,
                total=len(origins),
                metadata={
                    **_media_progress_metadata(report),
                    "system": system,
                    "media_tag": tag,
                },
            )

    def _render_local(
        self,
        dataset: dict,
        report: LibrarySyncReport,
        progress: ProgressSink = None,
        *,
        media_validation: Optional[dict[str, dict]] = None,
        materialize_media: bool = True,
        verify_existing: bool = False,
        media_presence: Optional[_DirectoryFileIndex] = None,
    ) -> int:
        media_validation = media_validation if media_validation is not None else {}
        media_presence = media_presence or _DirectoryFileIndex(report)
        rendered = 0
        by_system: dict[str, list[tuple[Game, str, dict]]] = {}
        for game in self._games.list_all():
            library_id = library_id_for_game(game)
            record = dataset["records"].get(library_id)
            if record is not None:
                by_system.setdefault(game.system, []).append((game, library_id, record))
        # Retained ineligible rows are intentionally absent above, but their
        # previously rendered ROMCloud-owned gamelist entries still need a
        # reconciliation pass so they cannot linger in Library presentation.
        for system in self._games.list_systems(include_ineligible=True):
            by_system.setdefault(system, [])
        total = sum(len(entries) for entries in by_system.values())
        # Loaded once for the whole render — resolving each game's local
        # launch path must never issue its own per-game database query.
        proxies_by_id = {record.game_id: record for record in self._proxies.list_all()}
        emit_progress(
            progress,
            "library_sync",
            "render",
            "running",
            f"Updating EmulationStation presentation for {total:,} games…",
            current=0,
            total=total,
        )
        for system, entries in by_system.items():
            rendered += self._render_system(
                system,
                entries,
                report,
                progress=progress,
                rendered_before=rendered,
                total=total,
                media_validation=media_validation,
                materialize_media=materialize_media,
                verify_existing=verify_existing,
                media_presence=media_presence,
                proxies_by_id=proxies_by_id,
            )
            emit_progress(
                progress,
                "library_sync",
                "render",
                "running",
                f"{system}: {rendered:,} / {total:,} games rendered",
                current=rendered,
                total=total,
                metadata={"system": system, "unit": "games"},
            )
        return rendered

    def _render_system(
        self,
        system: str,
        entries: Iterable[tuple[Game, str, dict]],
        report: LibrarySyncReport,
        *,
        progress: ProgressSink = None,
        rendered_before: int = 0,
        total: int = 0,
        media_validation: Optional[dict[str, dict]] = None,
        materialize_media: bool = True,
        verify_existing: bool = False,
        media_presence: Optional[_DirectoryFileIndex] = None,
        proxies_by_id: Optional[dict[str, ProxyRecord]] = None,
    ) -> int:
        media_validation = media_validation if media_validation is not None else {}
        media_presence = media_presence or _DirectoryFileIndex(report)
        system_root = self._local_roms_root / system
        path = system_root / "gamelist.xml"
        if path.is_symlink():
            report.failures.append(f"Refused symlink gamelist: {path}")
            return 0
        existing = ""
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
                root = ET.fromstring(existing)
            except (OSError, ET.ParseError) as exc:
                report.failures.append(f"Left malformed local gamelist untouched: {path}: {exc}")
                return 0
            if root.tag != "gameList":
                report.failures.append(
                    f"Left malformed local gamelist untouched: {path}: root must be gameList"
                )
                return 0
        else:
            root = ET.Element("gameList")

        owned: dict[str, ET.Element] = {}
        unowned_by_path: dict[str, ET.Element] = {}
        for candidate in root.findall("game"):
            marker = (candidate.findtext(OWNERSHIP_TAG) or "").strip()
            if marker:
                owned[marker] = candidate
                continue
            path_text = _safe_relative(candidate.findtext("path") or "")
            if path_text is not None and path_text not in unowned_by_path:
                unowned_by_path[path_text] = candidate
        desired: set[str] = set()
        count = 0
        for game, library_id, record in entries:
            emit_progress(
                progress,
                "library_sync",
                "render",
                "running",
                f"{system}: updating {game.title}",
                current=rendered_before + count,
                total=total,
                metadata={"system": system, "unit": "games"},
            )
            launch_path = self._local_launch_path(game, proxies_by_id)
            if launch_path is None:
                continue
            desired.add(library_id)
            element = owned.get(library_id)
            if element is None:
                # Adopt a matching local ROMCloud path (for pre-marker beta data)
                # but never an unrelated local game. Looked up from an index
                # built once above — a linear `root.findall` scan per game
                # here would make rendering an O(catalog²) operation.
                element = unowned_by_path.pop(_safe_relative(launch_path), None)
            if element is None:
                element = ET.SubElement(root, "game")
            _set_child(element, "path", launch_path)
            for tag, value in record.get("metadata", {}).items():
                if tag in METADATA_TAGS and str(value).strip():
                    _set_child(element, tag, str(value))
            for tag, descriptor in record.get("media", {}).items():
                if tag not in MEDIA_TAGS:
                    continue
                try:
                    rendered_media = self._render_media(
                        system_root,
                        descriptor,
                        report,
                        media_validation,
                        materialize=materialize_media,
                        verify_existing=verify_existing,
                        media_presence=media_presence,
                    )
                except (LibrarySyncError, OSError) as exc:
                    # One unreadable/corrupt/missing remote blob must never
                    # abort rendering for every other game and system — and
                    # must never remove an already-correct existing tag just
                    # because this particular attempt to refresh it failed.
                    report.failures.append(
                        f"{system}/{game.title} {tag}: could not render media: {exc}"
                    )
                    continue
                if rendered_media:
                    _set_child(element, tag, rendered_media)
                else:
                    stale = element.find(tag)
                    if stale is not None:
                        element.remove(stale)
            _set_child(element, OWNERSHIP_TAG, library_id)
            count += 1

        for marker, element in owned.items():
            if marker not in desired:
                root.remove(element)

        ET.indent(root, space="  ")
        result = '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
        if result != existing:
            if existing:
                backup = path.with_name(path.name + ".romcloud.bak")
                if not backup.exists():
                    atomic_write_text(backup, existing)
            atomic_write_text(path, result)
        return count

    def _local_launch_path(
        self, game: Game, proxies_by_id: Optional[dict[str, ProxyRecord]] = None
    ) -> Optional[str]:
        if self._mode == "direct_nas":
            return f"./ROMCloud/{_source_relative(game)}"
        proxy = (
            proxies_by_id.get(game.id)
            if proxies_by_id is not None
            else self._proxies.get(game.id)
        )
        if proxy is None:
            return None
        path = Path(proxy.proxy_path)
        if offline_library_enabled_for_roots(self._local_root.parent) and not path.is_file():
            return None
        try:
            rel = path.relative_to(self._local_roms_root / game.system).as_posix()
        except ValueError:
            return None
        # A normal refresh while offline deliberately leaves NAS-only
        # proxies absent; never recreate them here.
        if not path.is_file():
            return None
        return f"./{rel}"

    def _render_media(
        self,
        system_root: Path,
        descriptor: dict,
        report: LibrarySyncReport,
        media_validation: dict[str, dict],
        *,
        materialize: bool = True,
        verify_existing: bool = False,
        media_presence: Optional[_DirectoryFileIndex] = None,
    ) -> Optional[str]:
        source_path = descriptor.get("source_path")
        safe_source_path = _safe_relative(source_path) if isinstance(source_path, str) else None
        if self._mode == "direct_nas" and safe_source_path:
            return f"./ROMCloud/{safe_source_path}"
        blob = descriptor.get("blob")
        digest = descriptor.get("sha256")
        size = descriptor.get("size")
        suffix = str(descriptor.get("suffix", ""))
        safe_blob = _safe_relative(blob) if isinstance(blob, str) else None
        if (
            safe_blob is None
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(size, int)
            or size < 0
            or suffix not in ("", Path("x" + suffix).suffix)
            or "/" in suffix
            or "\\" in suffix
        ):
            return None
        relative = PurePosixPath(LOCAL_MEDIA_DIR, digest[:2], digest + suffix)
        destination = system_root / relative
        media_presence = media_presence or _DirectoryFileIndex(report)
        validation_key = f"{system_root.name}/{relative.as_posix()}"
        validation = media_validation.get(validation_key)
        report.media_examined += 1
        if not materialize:
            # Mode/presentation reconciliation must remain local-only.  An
            # existing ordinary file is safe to reference without opening it;
            # a missing file is optional artwork, not a reason to reach into
            # canonical remote storage or fail the mode transition.
            if media_presence.contains(destination):
                report.media_skipped += 1
                report.unchanged += 1
                return f"./{relative.as_posix()}"
            return None
        if self._remote_root is None:
            return None
        source = self._remote_root / safe_blob
        if not _within(source, self._remote_root):
            return None
        if not verify_existing and media_presence.contains(destination):
            report.media_skipped += 1
            report.unchanged += 1
        elif (
            verify_existing
            and isinstance(validation, dict)
            and validation.get("sha256") == digest
            and validation.get("size") == size
            and _fingerprint_matches(
                destination,
                validation.get("fingerprint"),
                expected_size=size,
            )
        ):
            report.media_skipped += 1
            report.unchanged += 1
        else:
            destination_matches = False
            if verify_existing and destination.is_file():
                destination_digest, destination_size = _hash_file(destination)
                _record_media_hash(report, destination_size)
                destination_matches = (destination_digest, destination_size) == (
                    digest,
                    size,
                )
            if destination_matches:
                report.unchanged += 1
            else:
                _copy_verified(source, destination, digest, size)
                _record_media_hash(report, size)  # temporary verification
                _record_media_copy(report, size)
                media_presence.add(destination)
            if verify_existing:
                media_validation[validation_key] = {
                    "sha256": digest,
                    "size": size,
                    "fingerprint": _capture_fingerprint(
                        destination, expected_size=size
                    ),
                }
        return f"./{relative.as_posix()}"

    def _read_state_payload(self) -> dict:
        try:
            if self._state_path.is_file() and not self._state_path.is_symlink():
                payload = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        except (OSError, ValueError):
            pass
        return {}

    def _read_media_validation(self) -> dict[str, dict]:
        validation = self._read_state_payload().get("media_validation")
        if not isinstance(validation, dict):
            return {}
        return {
            str(key): value
            for key, value in validation.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _write_state(
        self, report: LibrarySyncReport, media_validation: dict[str, dict]
    ) -> None:
        # A partial/failed pass must not replace the last known successful
        # operation or establish validation state that was never completed.
        if not report.ok:
            return
        atomic_write_text(
            self._state_path,
            json.dumps(
                {
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                    "direction": report.direction,
                    "report": report.as_dict(),
                    "media_validation": media_validation,
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
        )

    def _write_media_validation(self, media_validation: dict[str, dict]) -> None:
        payload = self._read_state_payload()
        payload["media_validation"] = media_validation
        atomic_write_text(
            self._state_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )


def offline_library_enabled_for_roots(data_root: Path) -> bool:
    """Small adapter avoiding construction of a second AppConfig."""
    path = data_root / "library-view.json"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload in (
            {"version": 1, "offline_library": True},
            {"version": 2, "mode": "offline"},
        )
    except (OSError, ValueError):
        return False


def _set_child(parent: ET.Element, tag: str, value: str) -> None:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = value


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _stat_fields(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
    }


# Fields compared to decide "has this file observably changed". ``ctime_ns``
# is intentionally excluded: on CIFS/SMB mounts the reported change time is
# not stable across independent stat() calls for a file whose content and
# mtime have not changed (server-side attribute synthesis/caching differs
# from local filesystems), so gating on it makes every fingerprint
# spuriously "miss" and forces a full re-hash of unchanged media on every
# sync. It is still recorded in ``_stat_fields``/fingerprints for diagnostics.
_STABLE_STAT_KEYS = ("size", "mtime_ns")


def _stat_unchanged(before: dict[str, int], after: dict[str, int]) -> bool:
    return all(before.get(key) == after.get(key) for key in _STABLE_STAT_KEYS)


def _sample_file_hash(path: Path, size: int) -> str:
    """Hash bounded samples so timestamps are never the sole cache proof."""
    sample = min(FINGERPRINT_SAMPLE_BYTES, size)
    offsets = sorted({0, max(0, (size - sample) // 2), max(0, size - sample)})
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            chunk = handle.read(sample)
            digest.update(offset.to_bytes(8, "big", signed=False))
            digest.update(len(chunk).to_bytes(8, "big", signed=False))
            digest.update(chunk)
    return digest.hexdigest()


def _capture_fingerprint(path: Path, *, expected_size: int) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise LibrarySyncError(f"Media file is unavailable or unsafe: {path}")
    before = _stat_fields(path)
    if before["size"] != expected_size:
        raise LibrarySyncError(f"Media size changed while it was being processed: {path}")
    sample_sha256 = _sample_file_hash(path, expected_size)
    after = _stat_fields(path)
    if not _stat_unchanged(before, after):
        raise LibrarySyncError(f"Media changed while it was being sampled: {path}")
    return {
        "version": FINGERPRINT_VERSION,
        **after,
        "sample_sha256": sample_sha256,
    }


def _fingerprint_matches(
    path: Path, persisted: object, *, expected_size: int
) -> bool:
    if not isinstance(persisted, dict) or path.is_symlink():
        return False
    if persisted.get("version") != FINGERPRINT_VERSION:
        return False
    if not _valid_sha256(persisted.get("sample_sha256")):
        return False
    if persisted.get("size") != expected_size:
        return False
    if not isinstance(persisted.get("mtime_ns"), int):
        return False
    try:
        before = _stat_fields(path)
    except OSError:
        return False
    expected = {"size": expected_size, "mtime_ns": persisted.get("mtime_ns")}
    if not _stat_unchanged(before, expected):
        return False
    try:
        sample_sha256 = _sample_file_hash(path, expected_size)
        after = _stat_fields(path)
    except OSError:
        return False
    return (
        _stat_unchanged(before, after)
        and sample_sha256 == persisted.get("sample_sha256")
    )


def _relative_media_path(path: Path, root: Path) -> Optional[str]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return None
    return _safe_relative(relative)


def _record_media_hash(report: LibrarySyncReport, size: int) -> None:
    report.media_hashed += 1
    report.media_bytes_hashed += max(0, size)


def _record_media_copy(report: LibrarySyncReport, size: int) -> None:
    report.media_transferred += 1
    report.media_bytes_transferred += max(0, size)


def _media_progress_metadata(report: LibrarySyncReport) -> dict[str, int]:
    return {
        "media_examined": report.media_examined,
        "media_skipped": report.media_skipped,
        "media_hashed": report.media_hashed,
        "media_copied": report.media_transferred,
        "presence_checks": report.media_presence_checks,
        "directories_listed": report.media_directories_listed,
        "bytes_hashed": report.media_bytes_hashed,
        "bytes_transferred": report.media_bytes_transferred,
    }


def _media_progress_detail(report: LibrarySyncReport) -> str:
    return (
        f"Examined {report.media_examined:,} · skipped {report.media_skipped:,} · "
        f"hashed {report.media_hashed:,} ({_format_bytes(report.media_bytes_hashed)}) · "
        f"copied {report.media_transferred:,} · "
        f"transferred {_format_bytes(report.media_bytes_transferred)}"
    )


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _copy_verified(source: Path, destination: Path, digest: str, size: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise LibrarySyncError(f"Media source is unavailable: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # SIGTERM cancellation cannot run the child process's ``finally`` block.
    # Remove only stale temp files for this exact content-addressed target;
    # the source and committed destination remain untouched.
    for stale in destination.parent.glob(f".{destination.name}.*.partial"):
        stale.unlink(missing_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    try:
        shutil.copyfile(source, temporary)
        if _hash_file(temporary) != (digest, size):
            raise LibrarySyncError(f"Media verification failed: {source}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(path: Path):
    """Serialize canonical writers across local filesystems and CIFS clients."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LibrarySyncError(
                "Another Library Sync writer is active; try again after it finishes."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

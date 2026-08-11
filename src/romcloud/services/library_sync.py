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
from romcloud.core.models.librarysync import LibrarySyncReport
from romcloud.core.storage import StorageProvider
from romcloud.infrastructure.atomic_file import atomic_write_text
from romcloud.infrastructure.repositories.game import GameRepository
from romcloud.infrastructure.repositories.proxy import ProxyRepository

SCHEMA_VERSION = 1
OWNERSHIP_TAG = "romcloudId"
LOCAL_MEDIA_DIR = ".romcloud-media"
CANONICAL_FILENAME = "library.json"
STATE_FILENAME = "library-sync-state.json"

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

    def pull(self) -> LibrarySyncReport:
        return self._run("pull", write_remote=False)

    def push(self) -> LibrarySyncReport:
        return self._run("push", write_remote=True)

    def sync(self) -> LibrarySyncReport:
        return self._run("sync", write_remote=True)

    def render_local(self) -> LibrarySyncReport:
        """Regenerate only device presentation from the last local canonical copy."""
        if not self.enabled:
            raise LibrarySyncError("Library Sync is disabled; enable it in configuration first.")
        report = LibrarySyncReport(direction="render")
        report.rendered = self._render_local(
            _read_dataset(self._local_root / CANONICAL_FILENAME), report
        )
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

    def _run(self, direction: str, *, write_remote: bool) -> LibrarySyncReport:
        self._require_available()
        assert self._remote_root is not None
        report = LibrarySyncReport(direction=direction)
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
            imported, origins = self._import_gamelists(report)
            self._merge(merged, imported, report, "gamelist import")
            self._seed_catalog_records(merged, report)
            if write_remote:
                self._materialize_remote_media(merged, origins, report)
                _write_dataset(remote_path, merged)
        _write_dataset(local_path, merged)
        report.rendered = self._render_local(merged, report)
        self._write_state(report)
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

    def _import_gamelists(self, report: LibrarySyncReport) -> tuple[dict, dict[tuple[str, str], Path]]:
        dataset = _empty_dataset()
        origins: dict[tuple[str, str], Path] = {}
        games = self._games.list_all()
        by_id = {library_id_for_game(game): game for game in games}
        for system in sorted({game.system for game in games}):
            system_games = [game for game in games if game.system == system]
            source_xml = self._source_root / system / "gamelist.xml"
            if source_xml.is_file() and not source_xml.is_symlink():
                self._import_one_xml(
                    source_xml, system, system_games, by_id, dataset, origins,
                    report, source=True,
                )
            local_xml = self._local_roms_root / system / "gamelist.xml"
            if local_xml.is_file() and not local_xml.is_symlink():
                self._import_one_xml(
                    local_xml, system, system_games, by_id, dataset, origins,
                    report, source=False,
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
    ) -> None:
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8"))
        except (OSError, ET.ParseError) as exc:
            report.failures.append(f"Ignored malformed {path}: {exc}")
            return
        if root.tag != "gameList":
            report.failures.append(f"Ignored malformed {path}: root must be gameList")
            return
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

        for element in root.findall("game"):
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

    def _materialize_remote_media(
        self,
        dataset: dict,
        origins: dict[tuple[str, str], Path],
        report: LibrarySyncReport,
    ) -> None:
        assert self._remote_root is not None
        for key, origin in origins.items():
            library_id, tag = key
            media = dataset["records"][library_id]["media"]
            if tag not in media:
                report.media_added += 1
            descriptor = media.setdefault(tag, {})
            digest, size = _hash_file(origin)
            suffix = origin.suffix.lower() if len(origin.suffix) <= 12 else ""
            if descriptor.get("sha256") not in (None, digest):
                report.conflicts.append(f"{library_id[:12]} {tag}: kept canonical media")
                continue
            descriptor.update({"sha256": digest, "size": size, "suffix": suffix, "blob": f"media/sha256/{digest[:2]}/{digest}{suffix}"})
            destination = self._remote_root / str(descriptor["blob"])
            if destination.is_file() and _hash_file(destination) == (digest, size):
                report.unchanged += 1
                continue
            _copy_verified(origin, destination, digest, size)
            report.media_transferred += 1

    def _render_local(self, dataset: dict, report: LibrarySyncReport) -> int:
        rendered = 0
        by_system: dict[str, list[tuple[Game, str, dict]]] = {}
        for game in self._games.list_all():
            library_id = library_id_for_game(game)
            record = dataset["records"].get(library_id)
            if record is not None:
                by_system.setdefault(game.system, []).append((game, library_id, record))
        for system, entries in by_system.items():
            rendered += self._render_system(system, entries, report)
        return rendered

    def _render_system(
        self,
        system: str,
        entries: Iterable[tuple[Game, str, dict]],
        report: LibrarySyncReport,
    ) -> int:
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

        owned = {
            (candidate.findtext(OWNERSHIP_TAG) or "").strip(): candidate
            for candidate in root.findall("game")
            if (candidate.findtext(OWNERSHIP_TAG) or "").strip()
        }
        desired: set[str] = set()
        count = 0
        for game, library_id, record in entries:
            launch_path = self._local_launch_path(game)
            if launch_path is None:
                continue
            desired.add(library_id)
            element = owned.get(library_id)
            if element is None:
                # Adopt a matching local ROMCloud path (for pre-marker beta data)
                # but never an unrelated local game.
                element = next(
                    (item for item in root.findall("game") if _safe_relative(item.findtext("path") or "") == _safe_relative(launch_path)),
                    None,
                )
            if element is None:
                element = ET.SubElement(root, "game")
            _set_child(element, "path", launch_path)
            for tag, value in record.get("metadata", {}).items():
                if tag in METADATA_TAGS and str(value).strip():
                    _set_child(element, tag, str(value))
            for tag, descriptor in record.get("media", {}).items():
                rendered_media = self._render_media(system_root, descriptor, report)
                if tag in MEDIA_TAGS and rendered_media:
                    _set_child(element, tag, rendered_media)
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

    def _local_launch_path(self, game: Game) -> Optional[str]:
        if self._mode == "direct_nas":
            return f"./ROMCloud/{_source_relative(game)}"
        proxy = self._proxies.get(game.id)
        if proxy is None:
            return None
        path = Path(proxy.proxy_path)
        if offline_library_enabled_for_roots(self._local_root.parent) and not path.is_file():
            return None
        try:
            rel = path.relative_to(self._local_roms_root / game.system).as_posix()
        except ValueError:
            return None
        # A normal refresh while offline deliberately leaves online-only
        # proxies absent; never recreate them here.
        if not path.is_file():
            return None
        return f"./{rel}"

    def _render_media(self, system_root: Path, descriptor: dict, report: LibrarySyncReport) -> Optional[str]:
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
            or self._remote_root is None
        ):
            return None
        source = self._remote_root / safe_blob
        if not _within(source, self._remote_root):
            return None
        relative = PurePosixPath(LOCAL_MEDIA_DIR, digest[:2], digest + suffix)
        destination = system_root / relative
        if destination.is_file() and _hash_file(destination) == (digest, size):
            report.unchanged += 1
        else:
            _copy_verified(source, destination, digest, size)
            report.media_transferred += 1
        return f"./{relative.as_posix()}"

    def _write_state(self, report: LibrarySyncReport) -> None:
        atomic_write_text(
            self._state_path,
            json.dumps(
                {
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                    "direction": report.direction,
                    "report": report.as_dict(),
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
        )


def offline_library_enabled_for_roots(data_root: Path) -> bool:
    """Small adapter avoiding construction of a second AppConfig."""
    path = data_root / "library-view.json"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")) == {"version": 1, "offline_library": True}
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


def _copy_verified(source: Path, destination: Path, digest: str, size: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise LibrarySyncError(f"Media source is unavailable: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
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

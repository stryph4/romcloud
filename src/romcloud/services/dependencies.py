"""Lazy, recursive dependency closure resolution for cache requests."""

from __future__ import annotations

import posixpath
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from romcloud.core.dependency_resolvers import DEFAULT_RESOLVERS, Resolver
from romcloud.core.exceptions import DependencyResolutionError, ProviderError
from romcloud.core.models.game import Game, GameAsset
from romcloud.core.storage import RemoteEntry, StorageProvider


class DependencyResolverRegistry:
    """Resolve a game's bounded descriptor dependency closure on demand."""

    def __init__(
        self,
        provider: StorageProvider,
        *,
        source_root: Optional[str] = None,
        resolvers: Optional[dict[str, Resolver]] = None,
        max_depth: int = 16,
        max_dependencies: int = 512,
        max_descriptor_bytes: int = 1024 * 1024,
    ) -> None:
        self._provider = provider
        self._source_root = source_root
        self._resolvers = dict(resolvers or DEFAULT_RESOLVERS)
        self._max_depth = max_depth
        self._max_dependencies = max_dependencies
        self._max_descriptor_bytes = max_descriptor_bytes

    @property
    def descriptor_extensions(self) -> frozenset[str]:
        return frozenset(self._resolvers)

    def resolve(self, game: Game) -> Game:
        """Return *game* with a fully resolved, primary-first asset closure."""
        primary = game.primary_asset
        if primary is None:
            raise DependencyResolutionError(f"Game {game.id!r} has no primary asset")

        source_root = self._source_root or game.source_root
        assets: dict[str, GameAsset] = {}
        directory_cache: dict[str, list[RemoteEntry]] = {}

        def walk(
            relative_path: str,
            *,
            primary_asset: bool,
            depth: int,
            ancestry: tuple[str, ...],
            known: Optional[GameAsset] = None,
            required: bool = True,
        ) -> None:
            normalized = self._validate_catalog_path(game.system, relative_path)
            if normalized in ancestry:
                chain = " -> ".join((*ancestry, normalized))
                raise DependencyResolutionError(f"Dependency cycle detected: {chain}")
            if normalized in assets:
                return
            if normalized != primary.relative_path and len(assets) >= self._max_dependencies + 1:
                raise DependencyResolutionError(
                    f"Dependency count exceeds limit of {self._max_dependencies}"
                )

            entry = self._source_entry(
                source_root,
                normalized,
                required=required,
                directory_cache=directory_cache,
            )
            if entry is None:
                return
            extension = PurePosixPath(normalized).suffix.lower()
            size = (
                entry.size_bytes
                if extension in self._resolvers
                else (known.size_bytes if known is not None else None)
            )
            if size is None:
                size = entry.size_bytes
            if size is None:
                size = self._provider.get_size(str(Path(source_root) / normalized))
            asset = GameAsset(
                filename=PurePosixPath(normalized).name,
                relative_path=normalized,
                size_bytes=size if size is not None else (known.size_bytes if known else None),
                is_primary=primary_asset,
            )
            assets[normalized] = asset

            resolver = self._resolvers.get(extension)
            if resolver is None:
                return
            if entry.is_directory:
                raise DependencyResolutionError(
                    f"Descriptor dependency is a directory: {normalized}"
                )
            if depth >= self._max_depth:
                raise DependencyResolutionError(
                    f"Dependency recursion exceeds limit of {self._max_depth}: {normalized}"
                )
            if size is not None and size > self._max_descriptor_bytes:
                raise DependencyResolutionError(
                    f"Descriptor exceeds {self._max_descriptor_bytes} byte limit: {normalized}"
                )
            try:
                text = self._provider.read_text(str(Path(source_root) / normalized))
            except ProviderError as exc:
                raise DependencyResolutionError(
                    f"Cannot read dependency descriptor {normalized}: {exc}"
                ) from exc
            for reference in resolver(normalized, text):
                child = self._resolve_reference(normalized, reference.path, game.system)
                walk(
                    child,
                    primary_asset=False,
                    depth=depth + 1,
                    ancestry=(*ancestry, normalized),
                    required=reference.required,
                )

        walk(
            primary.relative_path,
            primary_asset=True,
            depth=0,
            ancestry=(),
            known=primary,
        )
        # Preserve any non-descriptor catalog companions (legacy cue/bin
        # metadata and package assets) without making refresh responsible for
        # recursively resolving descriptors.
        for catalog_asset in game.assets:
            if catalog_asset.relative_path in assets:
                continue
            walk(
                catalog_asset.relative_path,
                primary_asset=False,
                depth=0,
                ancestry=(),
                known=catalog_asset,
            )
        return replace(game, assets=list(assets.values()))

    @staticmethod
    def _validate_catalog_path(system: str, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != system
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise DependencyResolutionError(
                f"Unsafe dependency path outside {system!r}: {relative_path!r}"
            )
        return path.as_posix()

    @staticmethod
    def _resolve_reference(descriptor: str, reference: str, system: str) -> str:
        raw = reference.strip().replace("\\", "/")
        windows = PureWindowsPath(reference.strip())
        if (
            not raw
            or raw.startswith(("/", "//", "~"))
            or PurePosixPath(raw).is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or "://" in raw
        ):
            raise DependencyResolutionError(
                f"Absolute dependency reference is not allowed in {descriptor}: {reference!r}"
            )
        candidate = posixpath.normpath(
            posixpath.join(posixpath.dirname(descriptor), raw)
        )
        parts = PurePosixPath(candidate).parts
        if not parts or parts[0] != system or any(part == ".." for part in parts):
            raise DependencyResolutionError(
                f"Dependency escapes the {system!r} source root in {descriptor}: {reference!r}"
            )
        return PurePosixPath(*parts).as_posix()

    def _source_entry(
        self,
        source_root: str,
        relative_path: str,
        *,
        required: bool,
        directory_cache: dict[str, list[RemoteEntry]],
    ) -> Optional[RemoteEntry]:
        parent = PurePosixPath(relative_path).parent.as_posix()
        if parent not in directory_cache:
            try:
                directory_cache[parent] = self._provider.list_entries(
                    source_root, parent
                )
            except Exception as exc:  # noqa: BLE001 - normalize provider failures
                raise DependencyResolutionError(
                    f"Cannot inspect dependency parent {parent}: {exc}"
                ) from exc
        entries = directory_cache[parent]
        entry = next(
            (item for item in entries if item.relative_path == relative_path), None
        )
        if entry is None:
            if not required:
                return None
            raise DependencyResolutionError(
                f"Required dependency is missing: {relative_path}"
            )
        if entry.is_symlink:
            raise DependencyResolutionError(
                f"Symlink dependency is not allowed: {relative_path}"
            )
        return entry

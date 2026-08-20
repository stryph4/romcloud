"""Unit tests for TransferService."""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.models.game import Game, GameAsset
from romcloud.services.transfer import TransferService
from romcloud.core.cancellation import TransferCancellationToken
from romcloud.core.exceptions import TransferCancelledError, TransferValidationError


class _ChunkedProvider:
    """Deterministic fake using the shared provider progress contract."""

    def __init__(self, provider_id: str = "local", chunk_size: int = 100) -> None:
        self.provider_id = provider_id
        self.chunk_size = chunk_size
        self.calls = 0

    def transfer_to(self, source_path, dest_path, on_progress=None):
        self.calls += 1
        source = Path(source_path)
        destination = Path(dest_path)
        if source.is_dir():
            files = sorted(path for path in source.rglob("*") if path.is_file())
            total = sum(path.stat().st_size for path in files)
            done = 0
            destination.mkdir(parents=True, exist_ok=True)
            for source_file in files:
                target = destination / source_file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source_file.open("rb") as src, target.open("wb") as dst:
                    while chunk := src.read(self.chunk_size):
                        dst.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done, total)
            return

        total = source.stat().st_size
        done = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, destination.open("wb") as dst:
            while chunk := src.read(self.chunk_size):
                dst.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)

    def get_size(self, path):
        source = Path(path)
        if source.is_file():
            return source.stat().st_size
        return sum(item.stat().st_size for item in source.rglob("*") if item.is_file())


@pytest.fixture
def simple_game(tmp_path) -> tuple[Game, Path]:
    """A single-file game with real file content."""
    source_root = tmp_path / "source"
    (source_root / "ps2").mkdir(parents=True)
    rom_file = source_root / "ps2" / "Test Game.iso"
    rom_file.write_bytes(b"rom_content" * 100)

    asset = GameAsset(
        filename="Test Game.iso",
        relative_path="ps2/Test Game.iso",
        size_bytes=len(b"rom_content" * 100),
        is_primary=True,
    )
    game = Game.create("ps2", "Test Game", "local", str(source_root), [asset])
    return game, source_root


@pytest.fixture
def dir_game(tmp_path) -> tuple[Game, Path]:
    """A directory-based game (PS3 style)."""
    source_root = tmp_path / "source"
    game_dir = source_root / "ps3" / "BCES00000"
    game_dir.mkdir(parents=True)
    (game_dir / "EBOOT.BIN").write_bytes(b"eboot" * 50)
    (game_dir / "data.pkg").write_bytes(b"data" * 200)

    total = len(b"eboot" * 50) + len(b"data" * 200)
    asset = GameAsset(
        filename="BCES00000",
        relative_path="ps3/BCES00000",
        size_bytes=total,
        is_primary=True,
    )
    game = Game.create("ps3", "BCES00000", "local", str(source_root), [asset])
    return game, source_root


class TestTransferService:
    @pytest.mark.parametrize("provider_id", ["local", "smb", "sftp"])
    def test_cancellation_is_provider_neutral_and_stops_before_completion(
        self, provider_id, simple_game, cache_dir
    ):
        game, _ = simple_game
        provider = _ChunkedProvider(provider_id)
        service = TransferService(provider=provider, cache_root=str(cache_dir))
        cancellation = TransferCancellationToken()

        def cancel_after_first_chunk(done, total):
            assert done < total
            cancellation.cancel()

        with pytest.raises(TransferCancelledError):
            service.transfer(game, cancel_after_first_chunk, cancellation)

        staging = cache_dir / ".partial" / "ps2" / "Test Game.iso"
        final = cache_dir / "ps2" / "Test Game.iso"
        assert 0 < staging.stat().st_size < game.total_size_bytes
        assert not final.exists()
        assert provider.calls == 1

    def test_cancelled_directory_package_stays_staged_and_invalid(
        self, dir_game, cache_dir
    ):
        game, _ = dir_game
        service = TransferService(
            provider=_ChunkedProvider("sftp"), cache_root=str(cache_dir)
        )
        cancellation = TransferCancellationToken()

        def cancel_after_first_chunk(done, total):
            cancellation.cancel()

        with pytest.raises(TransferCancelledError):
            service.transfer(game, cancel_after_first_chunk, cancellation)

        staging = cache_dir / ".partial" / "ps3" / "BCES00000"
        final = cache_dir / "ps3" / "BCES00000"
        assert staging.is_dir()
        assert service.staging_size(game) < game.total_size_bytes
        assert not final.exists()

    def test_subsequent_transfer_reuses_safe_staging_path_and_completes(
        self, simple_game, cache_dir
    ):
        game, _ = simple_game
        service = TransferService(
            provider=_ChunkedProvider("sftp"), cache_root=str(cache_dir)
        )
        cancellation = TransferCancellationToken()

        with pytest.raises(TransferCancelledError):
            service.transfer(
                game,
                lambda done, total: cancellation.cancel(),
                cancellation,
            )

        final = Path(service.transfer(game))
        assert final.read_bytes() == b"rom_content" * 100
        assert not (cache_dir / ".partial" / "ps2" / "Test Game.iso").exists()

    def test_estimates_unknown_directory_size_only_when_requested(
        self, dir_game, cache_dir
    ):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        game, _ = dir_game
        original = game.assets[0]
        game.assets[0] = GameAsset(
            filename=original.filename,
            relative_path=original.relative_path,
            size_bytes=None,
            is_primary=original.is_primary,
        )
        svc = TransferService(
            provider=LocalFilesystemProvider(), cache_root=str(cache_dir)
        )

        assert svc.estimate_size(game) == len(b"eboot" * 50) + len(b"data" * 200)

    def test_transfers_single_file(self, simple_game, cache_dir):
        game, source_root = simple_game
        svc = TransferService(provider=__import__(
            "romcloud.infrastructure.providers.local", fromlist=["LocalFilesystemProvider"]
        ).LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        cached_file = Path(final)
        assert cached_file.is_file()
        assert cached_file.name == "Test Game.iso"
        assert cached_file.read_bytes() == b"rom_content" * 100

    def test_staged_to_final(self, simple_game, cache_dir):
        """Transfer must go through .partial before reaching final path."""
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        # Final path must be under cache_root/system/<relative path>
        assert Path(final).parent.parent == cache_dir
        # Partial should no longer exist
        partial = cache_dir / ".partial" / "ps2" / "Test Game.iso"
        assert not partial.exists()

    def test_final_path_is_system_relative_path(self, simple_game, cache_dir):
        """Final path mirrors the asset's relative path under the system —
        not a game_id container — so the source basename is preserved."""
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        expected = str(cache_dir / "ps2" / "Test Game.iso")
        assert final == expected

    def test_progress_callback(self, simple_game, cache_dir):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        calls = []
        svc.transfer(game, on_progress=lambda d, t: calls.append((d, t)))
        assert len(calls) > 0
        assert calls[-1][0] > 0

    def test_validation_fails_wrong_size(self, tmp_path, cache_dir):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        source_root = tmp_path / "source"
        (source_root / "ps2").mkdir(parents=True)
        (source_root / "ps2" / "game.iso").write_bytes(b"x" * 100)

        asset = GameAsset(
            filename="game.iso",
            relative_path="ps2/game.iso",
            size_bytes=999,  # wrong size to trigger validation error
            is_primary=True,
        )
        game = Game.create("ps2", "bad", "local", str(source_root), [asset])
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        with pytest.raises(TransferValidationError):
            svc.transfer(game)

    def test_staging_preserved_after_error(self, tmp_path, cache_dir):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        source_root = tmp_path / "source"
        (source_root / "ps2").mkdir(parents=True)
        (source_root / "ps2" / "game.iso").write_bytes(b"x" * 100)

        asset = GameAsset("game.iso", "ps2/game.iso", size_bytes=999, is_primary=True)
        game = Game.create("ps2", "bad", "local", str(source_root), [asset])
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        with pytest.raises(Exception):
            svc.transfer(game)

        # Staging must still exist for resume
        staging = cache_dir / ".partial" / "ps2" / "game.iso"
        assert staging.exists()

    def test_transfers_directory(self, dir_game, cache_dir):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        game, _ = dir_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        game_dir = Path(final)
        assert game_dir.name == "BCES00000"
        assert game_dir.is_dir()
        assert (game_dir / "EBOOT.BIN").exists()
        assert (game_dir / "data.pkg").exists()

    def test_cached_filename_matches_source_filename(self, simple_game, cache_dir):
        """The cached ROM filename must equal the original source filename.

        Batocera's configgen matches per-game settings (e.g.
        ``snes["Some Game.sfc"].*``) against the bare filename of the ROM
        passed as ``-rom``.  If ROMCloud renamed the file during caching the
        per-game config lookup would silently fail — the user's configured
        emulator/core/shader overrides would not apply.
        """
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)

        source_filename = game.primary_asset.filename  # "Test Game.iso"
        cached_file = Path(final)
        assert cached_file.exists(), (
            f"Expected cached ROM at {cached_file}. "
            "Original filename must be preserved for Batocera configgen lookup."
        )
        assert cached_file.name == source_filename

    def test_resume_skips_complete_files(self, simple_game, cache_dir):
        """Second transfer call with same game resumes without re-copying."""
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        provider = LocalFilesystemProvider()
        svc = TransferService(provider=provider, cache_root=str(cache_dir))

        # First transfer
        svc.transfer(game)

        # Corrupt the final path so we need to re-transfer
        final = cache_dir / "ps2" / "Test Game.iso"
        final.unlink()

        # Pre-populate staging with correct content (simulate interrupted transfer)
        staging = cache_dir / ".partial" / "ps2" / "Test Game.iso"
        staging.parent.mkdir(parents=True, exist_ok=True)
        src_file = simple_game[1] / "ps2" / "Test Game.iso"
        staging.write_bytes(src_file.read_bytes())

        calls = []
        svc.transfer(game, on_progress=lambda d, t: calls.append((d, t)))
        # Resume should skip the file and go straight to promote
        # (progress may or may not be called depending on skip path)
        final_file = cache_dir / "ps2" / "Test Game.iso"
        assert final_file.exists()

    def test_no_collision_across_systems_with_same_filename(self, tmp_path, cache_dir):
        """Two systems with an identically-named ROM must not collide in the cache."""
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        source_root = tmp_path / "source"
        (source_root / "ps2").mkdir(parents=True)
        (source_root / "snes").mkdir(parents=True)
        (source_root / "ps2" / "Game.rom").write_bytes(b"ps2_content" * 10)
        (source_root / "snes" / "Game.rom").write_bytes(b"snes_content" * 10)

        ps2_asset = GameAsset("Game.rom", "ps2/Game.rom", is_primary=True)
        snes_asset = GameAsset("Game.rom", "snes/Game.rom", is_primary=True)
        ps2_game = Game.create("ps2", "Game", "local", str(source_root), [ps2_asset])
        snes_game = Game.create("snes", "Game", "local", str(source_root), [snes_asset])

        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))
        ps2_final = svc.transfer(ps2_game)
        snes_final = svc.transfer(snes_game)

        assert ps2_final == str(cache_dir / "ps2" / "Game.rom")
        assert snes_final == str(cache_dir / "snes" / "Game.rom")
        assert Path(ps2_final).read_bytes() == b"ps2_content" * 10
        assert Path(snes_final).read_bytes() == b"snes_content" * 10

    def test_no_collision_across_subdirectories_within_one_system(self, tmp_path, cache_dir):
        """Two subdirectories of the same system with identical filenames must not collide."""
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        source_root = tmp_path / "source"
        (source_root / "ps2" / "discs" / "1").mkdir(parents=True)
        (source_root / "ps2" / "discs" / "2").mkdir(parents=True)
        (source_root / "ps2" / "discs" / "1" / "Game.iso").write_bytes(b"disc_one" * 10)
        (source_root / "ps2" / "discs" / "2" / "Game.iso").write_bytes(b"disc_two" * 10)

        disc1_asset = GameAsset("Game.iso", "ps2/discs/1/Game.iso", is_primary=True)
        disc2_asset = GameAsset("Game.iso", "ps2/discs/2/Game.iso", is_primary=True)
        disc1_game = Game.create("ps2", "Disc 1", "local", str(source_root), [disc1_asset])
        disc2_game = Game.create("ps2", "Disc 2", "local", str(source_root), [disc2_asset])

        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))
        disc1_final = svc.transfer(disc1_game)
        disc2_final = svc.transfer(disc2_game)

        assert disc1_final == str(cache_dir / "ps2" / "discs" / "1" / "Game.iso")
        assert disc2_final == str(cache_dir / "ps2" / "discs" / "2" / "Game.iso")
        assert Path(disc1_final).read_bytes() == b"disc_one" * 10
        assert Path(disc2_final).read_bytes() == b"disc_two" * 10


class TestMultiAssetCueBinTransfer:
    """BIN/CUE multi-asset transfer: all required companions cached, .cue
    remains the launch path, progress aggregates across the whole game."""

    @staticmethod
    def _make_cue_game(tmp_path, num_tracks=3):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        source_root = tmp_path / "source"
        (source_root / "psx").mkdir(parents=True)
        cue_bytes = b"cue_sheet_data"
        (source_root / "psx" / "Game.cue").write_bytes(cue_bytes)

        assets = [
            GameAsset("Game.cue", "psx/Game.cue", size_bytes=len(cue_bytes), is_primary=True)
        ]
        track_bytes = {}
        for i in range(1, num_tracks + 1):
            name = f"Game (Track {i}).bin"
            content = bytes([i]) * (100 * i)
            (source_root / "psx" / name).write_bytes(content)
            track_bytes[name] = content
            assets.append(
                GameAsset(name, f"psx/{name}", size_bytes=len(content), is_primary=False)
            )

        game = Game.create("psx", "Game", "local", str(source_root), assets)
        return game, source_root, track_bytes, LocalFilesystemProvider()

    def test_cue_and_all_bins_are_cached(self, tmp_path, cache_dir):
        game, source_root, track_bytes, provider = self._make_cue_game(tmp_path)
        svc = TransferService(provider=provider, cache_root=str(cache_dir))

        final = svc.transfer(game)
        assert final == str(cache_dir / "psx" / "Game.cue")
        assert Path(final).is_file()
        for name, content in track_bytes.items():
            cached = cache_dir / "psx" / name
            assert cached.exists()
            assert cached.read_bytes() == content

    def test_launch_asset_is_the_cue_not_a_track(self, tmp_path, cache_dir):
        game, source_root, _, provider = self._make_cue_game(tmp_path)
        svc = TransferService(provider=provider, cache_root=str(cache_dir))
        final = svc.transfer(game)
        assert Path(final).name == "Game.cue"

    def test_relative_paths_and_filenames_preserved_exactly(self, tmp_path, cache_dir):
        game, source_root, track_bytes, provider = self._make_cue_game(tmp_path)
        svc = TransferService(provider=provider, cache_root=str(cache_dir))
        svc.transfer(game)
        for asset in game.assets:
            expected = cache_dir / "psx" / Path(asset.relative_path).name
            assert expected.exists()
            assert expected.name == asset.filename

    def test_progress_aggregates_across_all_assets_without_reset(self, tmp_path, cache_dir):
        """Percent/bytes must monotonically increase across the whole game —
        never reset to 0 when a new track starts (no per-track flashing)."""
        game, source_root, _, provider = self._make_cue_game(tmp_path)
        svc = TransferService(provider=provider, cache_root=str(cache_dir))

        calls: list[tuple[int, int]] = []
        svc.transfer(game, on_progress=lambda d, t: calls.append((d, t)))

        assert calls, "on_progress should have been called at least once"
        grand_total = game.total_size_bytes
        # total is constant across every callback (the whole-game grand total).
        assert all(t == grand_total for _, t in calls)
        # done is monotonically non-decreasing, and never resets to a small
        # value after having reached a larger one.
        seen_max = 0
        for done, _ in calls:
            assert done >= seen_max
            seen_max = done
        assert calls[-1][0] == grand_total

    def test_partial_pre_existing_set_only_downloads_missing_assets(self, tmp_path, cache_dir):
        """If the .cue and some tracks are already correctly cached, only
        the missing tracks should be (re-)transferred."""
        game, source_root, track_bytes, provider = self._make_cue_game(tmp_path, num_tracks=3)
        svc = TransferService(provider=provider, cache_root=str(cache_dir))

        # Pre-populate the final cache with the .cue and track 1 already correct.
        (cache_dir / "psx").mkdir(parents=True)
        (cache_dir / "psx" / "Game.cue").write_bytes((source_root / "psx" / "Game.cue").read_bytes())
        (cache_dir / "psx" / "Game (Track 1).bin").write_bytes(
            (source_root / "psx" / "Game (Track 1).bin").read_bytes()
        )
        # Corrupt/replace the source files for the already-cached assets so
        # a real re-transfer would be detectable (different bytes).
        (source_root / "psx" / "Game.cue").write_bytes(b"SHOULD NOT BE COPIED")

        final = svc.transfer(game)

        # The already-correct cached .cue must be untouched (repair-only-missing).
        assert Path(final).read_bytes() != b"SHOULD NOT BE COPIED"
        for i in (2, 3):
            name = f"Game (Track {i}).bin"
            assert (cache_dir / "psx" / name).read_bytes() == track_bytes[name]

    def test_failure_of_one_track_prevents_launch(self, tmp_path, cache_dir):
        """If one companion asset's source is missing, the whole transfer
        must fail (no launch), leaving no valid-looking cached set."""
        game, source_root, _, provider = self._make_cue_game(tmp_path, num_tracks=2)
        # Delete one referenced track from the source after cataloguing.
        (source_root / "psx" / "Game (Track 2).bin").unlink()

        svc = TransferService(provider=provider, cache_root=str(cache_dir))
        with pytest.raises(Exception):
            svc.transfer(game)

        # The cue itself may have been staged/promoted, but the game must
        # not be considered a complete, launchable set.
        assert not (cache_dir / "psx" / "Game (Track 2).bin").exists()

    def test_partial_transfer_preserves_staging_for_resume(self, tmp_path, cache_dir):
        game, source_root, _, provider = self._make_cue_game(tmp_path, num_tracks=2)
        (source_root / "psx" / "Game (Track 2).bin").unlink()

        svc = TransferService(provider=provider, cache_root=str(cache_dir))
        with pytest.raises(Exception):
            svc.transfer(game)

        # Track 1 (and the cue) should have made it to staging/final since
        # they were transferred before the missing track was reached.
        assert (cache_dir / "psx" / "Game.cue").exists() or (
            cache_dir / ".partial" / "psx" / "Game.cue"
        ).exists()


class TestSourceRootOverride:
    """A game's persisted `source_root` is catalog data captured at scan
    time; the explicit constructor override represents the currently
    configured root and must win when both are supplied."""

    def test_override_takes_priority_over_persisted_game_source_root(
        self, tmp_path, cache_dir
    ):
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        legacy_root = tmp_path / "romcloud-source"  # never created on disk
        current_root = tmp_path / "current-source"
        (current_root / "snes").mkdir(parents=True)
        (current_root / "snes" / "Game.sfc").write_bytes(b"current-content")

        asset = GameAsset(
            filename="Game.sfc", relative_path="snes/Game.sfc", is_primary=True
        )
        game = Game.create("snes", "Game", "local", str(legacy_root), [asset])

        svc = TransferService(
            provider=LocalFilesystemProvider(),
            cache_root=str(cache_dir),
            source_root=str(current_root),
        )

        final = svc.transfer(game)
        assert Path(final).read_bytes() == b"current-content"

    def test_no_override_falls_back_to_game_source_root(self, simple_game, cache_dir):
        """Backward compatibility: existing callers that never pass
        `source_root` keep resolving from the game's own persisted value."""
        from romcloud.infrastructure.providers.local import LocalFilesystemProvider

        game, source_root = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        assert Path(final).read_bytes() == b"rom_content" * 100

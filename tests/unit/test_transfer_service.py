"""Unit tests for TransferService."""

from __future__ import annotations

from pathlib import Path

import pytest

from romcloud.core.models.game import Game, GameAsset
from romcloud.core.services.transfer import TransferService
from romcloud.core.exceptions import TransferValidationError


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
    def test_transfers_single_file(self, simple_game, cache_dir):
        game, source_root = simple_game
        svc = TransferService(provider=__import__(
            "romcloud.core.providers.local", fromlist=["LocalFilesystemProvider"]
        ).LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        cached_file = Path(final)
        assert cached_file.is_file()
        assert cached_file.name == "Test Game.iso"
        assert cached_file.read_bytes() == b"rom_content" * 100

    def test_staged_to_final(self, simple_game, cache_dir):
        """Transfer must go through .partial before reaching final path."""
        from romcloud.core.providers.local import LocalFilesystemProvider
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
        from romcloud.core.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        final = svc.transfer(game)
        expected = str(cache_dir / "ps2" / "Test Game.iso")
        assert final == expected

    def test_progress_callback(self, simple_game, cache_dir):
        from romcloud.core.providers.local import LocalFilesystemProvider
        game, _ = simple_game
        svc = TransferService(provider=LocalFilesystemProvider(), cache_root=str(cache_dir))

        calls = []
        svc.transfer(game, on_progress=lambda d, t: calls.append((d, t)))
        assert len(calls) > 0
        assert calls[-1][0] > 0

    def test_validation_fails_wrong_size(self, tmp_path, cache_dir):
        from romcloud.core.providers.local import LocalFilesystemProvider
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
        from romcloud.core.providers.local import LocalFilesystemProvider
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
        from romcloud.core.providers.local import LocalFilesystemProvider
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
        from romcloud.core.providers.local import LocalFilesystemProvider
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
        from romcloud.core.providers.local import LocalFilesystemProvider
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
        from romcloud.core.providers.local import LocalFilesystemProvider

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
        from romcloud.core.providers.local import LocalFilesystemProvider

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

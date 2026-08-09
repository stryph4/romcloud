"""Unit tests for romcloud.core.cue_parser."""

from __future__ import annotations

from romcloud.core.cue_parser import parse_cue_references, resolve_cue_dependencies


class TestParseCueReferences:
    def test_single_bin_quoted(self):
        text = 'FILE "Game.bin" BINARY\n  TRACK 01 MODE1/2352\n'
        result = parse_cue_references(text)
        assert result.references == ["Game.bin"]
        assert result.warnings == []

    def test_multiple_bins(self):
        text = (
            'FILE "Game (Track 1).bin" BINARY\n'
            "  TRACK 01 MODE1/2352\n"
            '    INDEX 01 00:00:00\n'
            'FILE "Game (Track 2).bin" BINARY\n'
            "  TRACK 02 AUDIO\n"
            '    INDEX 00 00:00:00\n'
            '    INDEX 01 00:02:00\n'
            'FILE "Game (Track 3).bin" BINARY\n'
            "  TRACK 03 AUDIO\n"
        )
        result = parse_cue_references(text)
        assert result.references == [
            "Game (Track 1).bin",
            "Game (Track 2).bin",
            "Game (Track 3).bin",
        ]

    def test_quoted_filename_with_spaces(self):
        text = 'FILE "My Game (Disc 1) (Track 2).bin" BINARY\n'
        result = parse_cue_references(text)
        assert result.references == ["My Game (Disc 1) (Track 2).bin"]

    def test_bare_unquoted_filename(self):
        text = "FILE Game.bin BINARY\n"
        result = parse_cue_references(text)
        assert result.references == ["Game.bin"]

    def test_uppercase_file_keyword_and_type(self):
        text = 'file "GAME.BIN" binary\n'
        result = parse_cue_references(text)
        assert result.references == ["GAME.BIN"]

    def test_relative_subpath_reference(self):
        text = 'FILE "tracks/Track01.bin" BINARY\n'
        result = parse_cue_references(text)
        assert result.references == ["tracks/Track01.bin"]

    def test_malformed_file_line_no_type(self):
        text = 'FILE "Game.bin"\n'
        result = parse_cue_references(text)
        assert result.references == []
        assert len(result.warnings) == 1
        assert "malformed" in result.warnings[0].reason

    def test_malformed_file_line_no_filename(self):
        text = "FILE\n"
        result = parse_cue_references(text)
        assert result.references == []
        assert len(result.warnings) == 1

    def test_empty_quoted_filename(self):
        text = 'FILE "" BINARY\n'
        result = parse_cue_references(text)
        assert result.references == []
        assert len(result.warnings) == 1
        assert "empty" in result.warnings[0].reason

    def test_one_malformed_line_does_not_break_others(self):
        text = (
            'FILE "Good1.bin" BINARY\n'
            "FILE\n"
            'FILE "Good2.bin" BINARY\n'
        )
        result = parse_cue_references(text)
        assert result.references == ["Good1.bin", "Good2.bin"]
        assert len(result.warnings) == 1

    def test_ignores_non_file_lines(self):
        text = (
            "REM COMMENT this is a comment\n"
            'FILE "Game.bin" BINARY\n'
            "  TRACK 01 MODE1/2352\n"
            "    INDEX 01 00:00:00\n"
        )
        result = parse_cue_references(text)
        assert result.references == ["Game.bin"]

    def test_empty_cue_text(self):
        result = parse_cue_references("")
        assert result.references == []
        assert result.warnings == []


class TestResolveCueDependencies:
    def test_single_bin_same_directory(self):
        result = resolve_cue_dependencies(
            "psx/Game.cue", 'FILE "Game.bin" BINARY\n'
        )
        assert len(result.dependencies) == 1
        dep = result.dependencies[0]
        assert dep.raw_reference == "Game.bin"
        assert dep.relative_path == "psx/Game.bin"
        assert result.rejected == []

    def test_multiple_bins_same_directory(self):
        text = (
            'FILE "Game (Track 1).bin" BINARY\n'
            'FILE "Game (Track 2).bin" BINARY\n'
        )
        result = resolve_cue_dependencies("psx/Game.cue", text)
        paths = [d.relative_path for d in result.dependencies]
        assert paths == ["psx/Game (Track 1).bin", "psx/Game (Track 2).bin"]

    def test_directory_scoped_cue(self):
        """psx/Game A/Game A.cue referencing sibling tracks in its own dir."""
        text = 'FILE "Track 01.bin" BINARY\nFILE "Track 02.bin" BINARY\n'
        result = resolve_cue_dependencies("psx/Game A/Game A.cue", text)
        paths = [d.relative_path for d in result.dependencies]
        assert paths == ["psx/Game A/Track 01.bin", "psx/Game A/Track 02.bin"]

    def test_relative_subpath_from_cue_directory(self):
        text = 'FILE "tracks/Track01.bin" BINARY\n'
        result = resolve_cue_dependencies("psx/Game.cue", text)
        assert result.dependencies[0].relative_path == "psx/tracks/Track01.bin"

    def test_shared_track_across_sibling_directory(self):
        """A cue in a subdirectory may reference a file in a sibling dir,
        as long as it stays within the same system's source root."""
        text = 'FILE "../SharedTracks/Track01.bin" BINARY\n'
        result = resolve_cue_dependencies("psx/Game A/Game A.cue", text)
        assert result.dependencies[0].relative_path == "psx/SharedTracks/Track01.bin"
        assert result.rejected == []

    def test_traversal_attempt_outside_system_root_rejected(self):
        text = 'FILE "../../etc/passwd" BINARY\n'
        result = resolve_cue_dependencies("psx/Game.cue", text)
        assert result.dependencies == []
        assert len(result.rejected) == 1
        assert result.rejected[0].raw_reference == "../../etc/passwd"
        assert "traversal" in result.rejected[0].reason

    def test_traversal_attempt_into_sibling_system_rejected(self):
        """Escaping into a different Batocera system's root must also be rejected."""
        text = 'FILE "../../snes/Some Game.sfc" BINARY\n'
        result = resolve_cue_dependencies("psx/Game.cue", text)
        assert result.dependencies == []
        assert len(result.rejected) == 1

    def test_mixed_valid_and_traversal_references(self):
        text = (
            'FILE "Game.bin" BINARY\n'
            'FILE "../../../etc/passwd" BINARY\n'
        )
        result = resolve_cue_dependencies("psx/Game.cue", text)
        assert [d.relative_path for d in result.dependencies] == ["psx/Game.bin"]
        assert len(result.rejected) == 1

    def test_malformed_cue_produces_no_dependencies_but_no_crash(self):
        result = resolve_cue_dependencies("psx/Game.cue", "not a cue file at all")
        assert result.dependencies == []
        assert result.rejected == []
        assert result.warnings == []

    def test_case_insensitive_extensions_do_not_affect_parsing(self):
        text = 'FILE "GAME.BIN" BINARY\n'
        result = resolve_cue_dependencies("PSX/GAME.CUE", text)
        assert result.dependencies[0].relative_path == "PSX/GAME.BIN"

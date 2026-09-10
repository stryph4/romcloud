#!/usr/bin/env python3
"""Wall-clock + operation-count profiler for the synchronous gameStop Auto SaveSync path.

Runs one complete ``AutoSaveSyncCoordinator.game_stop`` against a throwaway
fixture tree and reports where the time went, plus the number of directory
walks, file hashes and bytes hashed on each side. Operation counts are the
stable signal; wall-clock is reported for scale only.

The remote side can be given synthetic per-file latency (``--remote-latency-ms``)
to approximate a CIFS/SMB mount, which is what real hardware uses.

Usage::

    python scripts/profile_auto_savesync.py --scenario ps2-change
    python scripts/profile_auto_savesync.py --scenario no-change --repeat 3
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from romcloud.core.save_selection import DEFAULT_SAVE_SELECTION_POLICY  # noqa: E402
from romcloud.core.storage import ProviderCapabilities, StorageProvider  # noqa: E402
from romcloud.infrastructure import save_tree  # noqa: E402
from romcloud.services.auto_savesync import AutoSaveSyncCoordinator  # noqa: E402
from romcloud.services.saves import SaveSyncService  # noqa: E402


class _Provider(StorageProvider):
    """Local-like provider: the "remote" is a real directory, exactly like a
    mounted CIFS share on Batocera."""

    @property
    def provider_id(self) -> str:
        return "profile"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            has_filesystem_semantics=True, supports_durable_transactions=True
        )

    def is_reachable(self, root: str) -> bool:
        return True

    def list_systems(self, rom_root: str):
        raise NotImplementedError

    def list_entries(self, rom_root: str, system: str):
        raise NotImplementedError

    def get_size(self, path: str):
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def transfer_to(self, source_path: str, dest_path: str, on_progress=None) -> None:
        raise NotImplementedError


@dataclass
class _Counters:
    local_walks: int = 0
    remote_walks: int = 0
    local_hashes: int = 0
    remote_hashes: int = 0
    local_hash_bytes: int = 0
    remote_hash_bytes: int = 0
    sleep_seconds: float = 0.0
    sleep_calls: int = 0
    stages: list[tuple[str, float]] = field(default_factory=list)


class _Instrumentation:
    """Monkeypatches the real scanner so the same harness measures any checkout."""

    def __init__(self, local_root: Path, remote_root: Path, remote_latency_ms: float):
        self.counters = _Counters()
        self._local_root = local_root.resolve()
        self._remote_root = remote_root.resolve()
        self._latency = remote_latency_ms / 1000.0
        self._original_hash = save_tree.hash_file
        self._original_iter = save_tree._iter_approved_files
        self._original_sleep = time.sleep

    def _is_remote(self, path: Path) -> bool:
        try:
            return self._remote_root in Path(path).resolve().parents
        except OSError:
            return False

    def install(self) -> None:
        counters = self.counters
        original_hash = self._original_hash
        original_iter = self._original_iter
        original_sleep = self._original_sleep
        latency = self._latency
        is_remote = self._is_remote

        def hash_file(path: Path) -> str:
            remote = is_remote(path)
            if remote and latency:
                original_sleep(latency)
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            if remote:
                counters.remote_hashes += 1
                counters.remote_hash_bytes += size
            else:
                counters.local_hashes += 1
                counters.local_hash_bytes += size
            return original_hash(path)

        def iter_approved_files(root: Path, *, recursive: bool):
            if is_remote(root) or root == self._remote_root:
                counters.remote_walks += 1
                if latency:
                    original_sleep(latency)
            else:
                counters.local_walks += 1
            yield from original_iter(root, recursive=recursive)

        def sleep(seconds: float) -> None:
            counters.sleep_calls += 1
            counters.sleep_seconds += seconds
            original_sleep(seconds)

        save_tree.hash_file = hash_file
        save_tree._iter_approved_files = iter_approved_files
        time.sleep = sleep

    def remove(self) -> None:
        save_tree.hash_file = self._original_hash
        save_tree._iter_approved_files = self._original_iter
        time.sleep = self._original_sleep


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _blob(size: int, seed: int) -> bytes:
    return (seed.to_bytes(8, "little") * ((size // 8) + 1))[:size]


_SCENARIOS = {
    # (system, emulator, core, rom, {relative path: size}, changed relative path)
    "no-change": (
        "snes",
        "libretro",
        "snes9x",
        "Super Metroid.sfc",
        {"snes/Super Metroid.srm": 32 * 1024},
        None,
    ),
    "tiny-change": (
        "snes",
        "libretro",
        "snes9x",
        "Super Metroid.sfc",
        {"snes/Super Metroid.srm": 32 * 1024},
        "snes/Super Metroid.srm",
    ),
    "psx-change": (
        "psx",
        "libretro",
        "swanstation",
        "Final Fantasy VII.chd",
        {"psx/duckstation/memcards/shared_card_1.mcd": 128 * 1024},
        "psx/duckstation/memcards/shared_card_1.mcd",
    ),
    "ps2-change": (
        "ps2",
        "pcsx2",
        "pcsx2",
        "Tekken 5.iso",
        {
            "ps2/pcsx2/Mcd001.ps2": 8 * 1024 * 1024,
            "ps2/pcsx2/Mcd002.ps2": 8 * 1024 * 1024,
            "ps2/pcsx2/sstates/Tekken 5.00.p2s": 24 * 1024 * 1024,
            "ps2/pcsx2/sstates/Tekken 5.01.p2s": 24 * 1024 * 1024,
            "ps2/pcsx2/sstates/Ico.00.p2s": 24 * 1024 * 1024,
        },
        "ps2/pcsx2/Mcd001.ps2",
    ),
    "ps2-no-change": (
        "ps2",
        "pcsx2",
        "pcsx2",
        "Tekken 5.iso",
        {
            "ps2/pcsx2/Mcd001.ps2": 8 * 1024 * 1024,
            "ps2/pcsx2/Mcd002.ps2": 8 * 1024 * 1024,
            "ps2/pcsx2/sstates/Tekken 5.00.p2s": 24 * 1024 * 1024,
            "ps2/pcsx2/sstates/Tekken 5.01.p2s": 24 * 1024 * 1024,
            "ps2/pcsx2/sstates/Ico.00.p2s": 24 * 1024 * 1024,
        },
        None,
    ),
}


def _run_once(
    scenario: str, *, quiet_seconds: float, remote_latency_ms: float
) -> tuple[_Counters, float, str]:
    system, emulator, core, rom, files, changed = _SCENARIOS[scenario]
    root = Path(tempfile.mkdtemp(prefix="romcloud-profile-"))
    try:
        local = root / "local"
        remote = root / "remote"
        local.mkdir()
        for index, (relative, size) in enumerate(files.items()):
            _write(local / relative, _blob(size, index + 1))

        service = SaveSyncService(
            provider=_Provider(),
            connectivity_root=str(root / "remote-data"),
            local_root=str(local),
            remote_root=str(remote),
            state_path=root / "data" / "savesync-state.json",
        )
        service.full_sync()

        coordinator = AutoSaveSyncCoordinator(
            service,
            data_root=root / "data",
            enabled=True,
            policy=DEFAULT_SAVE_SELECTION_POLICY,
            quiet_seconds=quiet_seconds,
        )
        coordinator.game_start(system=system, emulator=emulator, core=core, rom=rom)
        if changed is not None:
            _write(local / changed, _blob(files[changed], 99))

        instrumentation = _Instrumentation(local, remote, remote_latency_ms)
        instrumentation.install()
        started = time.perf_counter()
        try:
            coordinator.game_stop(
                system=system, emulator=emulator, core=core, rom=rom
            )
            status = "ok"
        except Exception as exc:  # noqa: BLE001 - profiling must still report
            status = f"{type(exc).__name__}: {exc}"
        finally:
            elapsed = time.perf_counter() - started
            instrumentation.remove()
        return instrumentation.counters, elapsed, status
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=sorted(_SCENARIOS), default="ps2-change"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--quiet-seconds",
        type=float,
        default=1.0,
        help="Stability settle interval (production default is 1.0)",
    )
    parser.add_argument(
        "--remote-latency-ms",
        type=float,
        default=8.0,
        help="Synthetic per-file remote latency approximating a CIFS mount",
    )
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)

    rows = []
    for _ in range(max(1, arguments.repeat)):
        counters, elapsed, status = _run_once(
            arguments.scenario,
            quiet_seconds=arguments.quiet_seconds,
            remote_latency_ms=arguments.remote_latency_ms,
        )
        rows.append((counters, elapsed, status))

    if arguments.json:
        print(
            json.dumps(
                [
                    {
                        "scenario": arguments.scenario,
                        "status": status,
                        "elapsed_s": round(elapsed, 3),
                        **{
                            key: value
                            for key, value in vars(counters).items()
                            if key != "stages"
                        },
                    }
                    for counters, elapsed, status in rows
                ],
                indent=2,
            )
        )
        return 0

    print(f"scenario: {arguments.scenario}  repeats: {len(rows)}")
    print(
        f"settle interval: {arguments.quiet_seconds}s   "
        f"remote latency: {arguments.remote_latency_ms}ms/file"
    )
    print("-" * 78)
    for index, (counters, elapsed, status) in enumerate(rows, start=1):
        print(f"run {index}: total {elapsed:7.3f}s   status={status}")
        print(
            f"    sleeping           {counters.sleep_seconds:7.3f}s "
            f"({counters.sleep_seconds / elapsed * 100:5.1f}%) "
            f"in {counters.sleep_calls} call(s)"
        )
        print(
            f"    local  walks={counters.local_walks:<4d} "
            f"hashes={counters.local_hashes:<4d} "
            f"bytes={counters.local_hash_bytes / 1024 / 1024:8.1f} MiB"
        )
        print(
            f"    remote walks={counters.remote_walks:<4d} "
            f"hashes={counters.remote_hashes:<4d} "
            f"bytes={counters.remote_hash_bytes / 1024 / 1024:8.1f} MiB"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

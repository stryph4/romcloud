# ROMCloud

**Browse a large ROM library in Batocera without keeping the whole library on
the device.**

ROMCloud makes ROMs stored on a NAS, another PC, or external storage appear in
Batocera's normal EmulationStation library. **Smart Cache** downloads games on
first launch and keeps frequently used games locally. **Direct/NAS** exposes
the read-only remote system folders directly and requires the source to remain
reachable while browsing and playing.

## Why use ROMCloud?

- **Browse the full library in EmulationStation.** Small `.romcloud` entries
  represent remote games alongside ordinary local games.
- **Download on first launch.** A cache miss shows transfer progress, verifies
  the result, and hands the local copy to Batocera.
- **Keep a bounded local library automatically.** Configurable cache capacity,
  a minimum free-space reserve, least-recently-used eviction, and pinning keep
  storage use predictable.
- **Play cached games while the source is offline.** A complete cache hit does
  not require the ROM source to be reachable. Uncached or incomplete games do.
- **Sync selected game-progress saves manually.** SaveSync previews uploads or
  downloads, verifies staged data, and commits a whole dataset transactionally.
- **Optionally sync library metadata.** Library Sync imports scraped metadata
  and media into ROMCloud-owned canonical records, then renders the correct
  local paths for Smart Cache or Direct/NAS without modifying source XML.
- **Use familiar storage.** The current beta supports SMB shares and local or
  externally mounted directories. A NAS or PC can expose an SMB share; USB and
  other mounted storage work through the local-filesystem path.
- **Set up and maintain ROMCloud graphically.** The Batocera Ports interface
  handles storage setup, connection controls, catalog refresh, SaveSync,
  health checks, and updates with controller, touch, or keyboard input.
- **Recover instead of starting over.** Interrupted game transfers retain
  staging data; completed assets can be reused on the next attempt. Repair and
  update reconcile installed artifacts and restore missing ROMCloud-owned
  proxies when catalog records are available.
- **Manage the complete lifecycle.** Built-in update, repair, uninstall, and
  purge workflows distinguish replaceable runtime files from user data.

> [!IMPORTANT]
> The ROM-source SMB mount remains read-only in both modes. Direct/NAS does not
> provide downloads, pinning, eviction, cache cleanup, or offline games.
> SaveSync and Library Sync, when enabled, use a separate explicitly writable
> data location.

## Public beta status

ROMCloud is ready for public beta use, but it is still under active development.
The core proxy, launch, cache, SMB mount, updater, and Batocera integration paths
have evidence from a Steam Deck running **Batocera 42**. Save-selection rules
also include layouts audited against **Batocera v43** generators, but that is not
the same as full end-to-end v43 hardware validation.

Back up important saves before using any beta synchronization tool. Review the
[known limitations](#known-limitations) and [SaveSync scope](#savesync) before
depending on ROMCloud as the only copy of data.

## Common setups

- **NAS library:** expose a read-only-capable SMB share containing Batocera
  system folders such as `ps2`, `psx`, and `snes`.
- **PC-hosted library:** share a folder over SMB from Linux, Windows, or another
  system reachable by the Batocera device.
- **External storage:** select an already mounted local/USB directory through
  the graphical folder browser or CLI configuration.
- **Remote NAS over a private network:** ROMCloud can use an SMB address made
  reachable by software such as Tailscale, but ROMCloud does not configure or
  monitor Tailscale itself.

SFTP is **not implemented** in the current beta. Native, userspace SMB access is
also not implemented; SMB sources use Batocera's `mount.cifs` support and are
then read through ROMCloud's local-filesystem provider.

## How it works

```text
NAS / PC / external storage
        │
        │  complete ROM library
        ▼
ROMCloud catalog refresh
        │
        │  creates tiny local .romcloud entries
        ▼
Batocera EmulationStation library
        │
        ├── cached ───────────────► launch local copy immediately
        │
        └── not cached
                │
                ├── transfer and verify game assets
                ▼
        managed local LRU cache
                │
                ▼
        Batocera emulatorlauncher
```

In Direct/NAS mode ROMCloud creates only a verified, manifest-owned `ROMCloud`
directory symlink inside each existing `/userdata/roms/<system>/` directory.
Local ROMs remain alongside it. ROMCloud never owns the system directory and
uninstall/reconfigure unlinks only symlinks whose recorded path and target still
match. An existing foreign file, directory, or symlink at that reserved path is
reported as a conflict and left untouched.

ROMCloud does not replace EmulationStation, Batocera's emulator configuration,
or `emulatorlauncher`. Smart Cache's launch wrapper changes only the ROM path
for a `.romcloud` game. Ordinary local and Direct/NAS ROM launches use
Batocera's normal path.

## Storage support

| Source or destination | Current status | How it works |
| --- | --- | --- |
| SMB ROM source | Supported | Mounted with CIFS at a local path, read-only, then scanned through the local-filesystem provider. |
| Local or external ROM source | Supported | Uses an existing absolute directory directly. |
| SMB synchronized data | Supported | Separate SMB target for SaveSync/Library Sync, mounted read/write and verified with a write/read/cleanup probe. |
| Local or external synchronized data | Supported | Explicit writable directory verified with the same non-destructive probe. |
| SFTP | Not implemented | No SFTP provider or setup flow exists. |
| Direct/native SMB provider | Not implemented | The source tree contains only a future placeholder; current SMB support uses mounted CIFS. |

For SMB configurations, ROMCloud keeps two distinct trust boundaries:

- the ROM catalog source is read-only;
- optional ROMCloud data storage for SaveSync and Library Sync is read/write and may use a
  different server, share, subdirectory, username, and password.

ROMCloud rejects overlapping source and remote-data locations. Credentials are
stored separately from `romcloud.toml`, atomically, with mode `0600`, and are
not placed in process arguments or normal logs.

## Quick start on Batocera

### 1. Prepare the library

The selected source root should contain folders named with Batocera system
identifiers:

```text
Roms/
├── dreamcast/
├── ps2/
├── psx/
├── snes/
└── xbox360/
```

Only folders that match systems known to the installed Batocera system
definitions are cataloged. Systems do not all need to exist.

### 2. Install

From a Batocera shell:

```bash
curl -fsSL https://raw.githubusercontent.com/stryph4/romcloud/main/scripts/bootstrap.sh | bash
```

The bootstrap requires `curl`, `tar`, a writable `/userdata`, and `python3`.
It downloads the requested GitHub archive, runs the idempotent installer, and
creates a private Python environment under `/userdata/system/romcloud`. It does
not install packages into Batocera's system Python or edit
`/userdata/system/custom.sh`.

To install a specific tag, branch, or full commit SHA:

```bash
curl -fsSL https://raw.githubusercontent.com/stryph4/romcloud/main/scripts/bootstrap.sh \
  | ROMCLOUD_REF=<tag-branch-or-commit> bash
```

### 3. Run graphical setup

Update EmulationStation's game lists if necessary, then launch
**Ports → ROMCloud**.

The first-run wizard can configure either:

- an SMB source, including server, port, credentials, share discovery, and an
  optional folder inside the share; or
- a local/external directory selected with the graphical folder browser.

The wizard validates the source, detects recognizable system folders, asks for
cache limits, and optionally configures independent writable storage for
SaveSync. It then applies the configuration, mounts configured SMB targets,
performs the initial catalog refresh, creates proxies, and reconciles the
EmulationStation integration.

Setup is retryable. A failed fresh setup retains credential-free failure state;
a failed repair of an existing valid setup restores the previous configuration
and credentials. EmulationStation still needs a game-list refresh or restart to
show newly cataloged games.

### CLI setup alternative

The guided CLI remains available:

```bash
/userdata/system/romcloud/bin/romcloud configure
/userdata/system/romcloud/bin/romcloud healthcheck
/userdata/system/romcloud/bin/romcloud refresh
```

`romcloud configure` supports guided SMB discovery and local/external paths.
Non-interactive options are available through `romcloud configure --help`.

## Graphical interface

The optional graphical application is installed when Batocera has a system
Python capable of importing pygame. It runs under that system Python while all
ROMCloud backend work remains in the isolated virtual environment. The two
processes communicate through credential-safe JSON subprocess requests;
`ports_gfx` never imports the installed `romcloud` package.

The current category-based interface provides:

- **Library:** catalog status/refresh, cache and offline-presentation controls,
  and Library Sync when enabled;
- **Storage:** setup/reconfiguration, connection status, mount/reconnect, and
  unmount;
- **SaveSync:** status, upload/download preview, hold-to-confirm commit, and the
  Original Xbox opt-in setting;
- **Maintenance:** check for updates and install an update;
- **Settings:** health check, controller diagnostics/remapping, and exit.

Long operations run without a fixed subprocess timeout. Their progress and
technical details remain scrollable while the graphical event loop stays
responsive. The UI supports SDL game controllers, touchscreen input, physical
keyboards, and a controller/touch on-screen keyboard for setup fields.

If pygame is unavailable, installation of the graphical component is skipped
without disabling the CLI, cache, launcher, or mount service.

## Batocera and EmulationStation integration

A catalog refresh writes small JSON `.romcloud` proxy files into Batocera's
normal ROM directories, for example:

```text
/userdata/roms/psx/Alundra (USA).romcloud
/userdata/roms/ps2/Some Game.romcloud
```

ROMCloud records ownership and refuses to overwrite or remove an unrelated
file. Existing local ROMs are left byte-for-byte untouched and can coexist in a
managed system directory.

The EmulationStation override is stored at:

```text
/userdata/system/configs/emulationstation/es_systems_romcloud.cfg
```

ROMCloud does not modify Batocera's stock
`/usr/share/emulationstation/es_systems.cfg`. The generated override preserves
Batocera's launcher arguments and extensions, adds `.romcloud`, and routes only
those proxy launches through `romcloud-run`.

At launch:

1. `romcloud-run` recognizes a `.romcloud` path.
2. ROMCloud resolves its catalog entry and verifies whether every required game
   asset is already cached.
3. A cache hit launches immediately from local storage.
4. A cache miss requires a reachable source, transfers into staging, validates
   the assets, and promotes them into the cache.
5. The wrapper passes the local primary asset to Batocera's normal
   `emulatorlauncher`, preserving all other launch arguments.

The launcher supports ordinary single-file or directory-valued games and
multi-asset BIN/CUE games. CUE dependencies are cataloged together, and a
missing companion makes the cache incomplete. `.m3u` multi-disc playlists and
CCD/IMG/SUB dependency grouping are not yet implemented.

## Catalog refresh and recovery

Initial setup performs a catalog refresh automatically. After changing the
source library, run **Library → Refresh Catalog** or:

```bash
/userdata/system/romcloud/bin/romcloud refresh
```

Refresh behavior is intentionally conservative:

- known system directories are scanned independently and report per-system
  progress;
- existing games are not duplicated;
- changed companion-asset metadata is updated in place, preserving game IDs,
  cache history, and pin state;
- stale entries that have become companions of a CUE set are pruned;
- a source game that simply disappears is **not** automatically deleted from
  the catalog;
- only ROMCloud-owned proxy files may be rewritten or removed;
- EmulationStation registration is regenerated from cataloged systems after a
  successful refresh.

Updates and `romcloud repair` reconcile managed launchers/integrations and can
restore a missing owned proxy from retained catalog data. Launch-time CUE
resolution also repairs legacy companion metadata when the current source is
available. These are targeted recovery paths, not a general backup of the
catalog or ROM source.

Refresh does not restart EmulationStation. Update game lists or restart
EmulationStation after catalog changes.

## Library Sync

Library Sync is disabled by default. It synchronizes normalized metadata, not
raw `gamelist.xml` files. Enable it in graphical setup, with
`romcloud configure --library-sync`, or after setup with:

```bash
romcloud library-sync enable
romcloud library-sync sync
```

When enabled, ROMCloud may read a `gamelist.xml` under each source system to
initialize names, descriptions, ratings, dates, developer/publisher/genre,
players, and supported media. Source/NAS XML is never written. Local system
gamelists are merged atomically: unrelated local-game entries are retained,
while ROMCloud-managed entries carry a stable `romcloudId` ownership marker.
Malformed XML and paths escaping a system root are ignored or left untouched.

Canonical records live at:

```text
<remote_data.root>/library/library.json
<remote_data.root>/library/media/sha256/...
```

The device working copy is `data/library/library.json`. Record identity is a
SHA-256 key derived from the Batocera system and normalized primary source-ROM
path; mount paths, local `.romcloud` paths, and access mode are excluded.
Smart Cache renders `./Game.romcloud` and local content-addressed media copies.
Direct/NAS renders `./ROMCloud/Game.chd` and prefers source-hosted media such as
`./ROMCloud/images/Game.png`. Switching modes regenerates presentation without
changing canonical records.

The beta merge policy is non-destructive and additive. Missing fields/media
are filled; blank local values never delete canonical data. If two non-empty
values differ, the existing canonical value wins deterministically and a
conflict is reported. Media are SHA-256 addressed and copied only when missing
or changed. No operation recursively deletes media trees.

Explicit operations are:

```bash
romcloud library-sync status
romcloud library-sync pull
romcloud library-sync push
romcloud library-sync sync
romcloud library-sync remove-local
```

`pull` updates local canonical/presentation state without writing the remote
canonical document. `push` and `sync` add local/source improvements to the
remote store. `remove-local` removes only marked local entries and preserves
canonical/source data and media. Catalog refresh runs Library Sync only while
the opt-in is enabled and NAS Mode is active. Library Sync push, pull, and
sync are blocked in Offline Mode; the canonical remote library is left
untouched and locally scraped metadata remains available for a later NAS Mode
sync. Local rendering never recreates absent NAS-only proxies.

## Local cache and offline use

This section applies to Smart Cache. Direct/NAS hides these controls in the
graphical interface. Explicit CLI maintenance remains possible for diagnostics
with `romcloud cache --override ...`; the override applies to that invocation
only and does not change the configured mode.

### NAS and Offline modes

Smart Cache has one authoritative operating state, shown as two opposite
choices on the graphical main menu. **Offline Mode** shows only ROMCloud games
whose complete cached assets are currently valid and disables remote-dependent
work. **NAS Mode** is the full-library state. The CLI is:

```bash
romcloud library offline
romcloud library status
romcloud library nas
```

Offline Mode allows cached launches, cache status/pin/unpin/removal/eviction,
local settings and diagnostics, and connection recovery bookkeeping. It
blocks cache misses/downloads, provider/catalog refresh, Library Sync remote
operations, SaveSync upload/download, and update network operations before
they contact remote storage. It never enters or leaves automatically when
connectivity changes. If storage becomes unreachable while NAS Mode is active,
ROMCloud remains in NAS Mode and reports a connectivity failure.

Selecting NAS Mode while Offline Mode is active is an explicit reconnect. The
transition remounts configured storage, validates the read-only ROM source and
any separate writable ROMCloud data location, refreshes the complete catalog,
runs enabled Library Sync, prepares the full proxy/gamelist presentation, and
refreshes EmulationStation before atomically committing NAS Mode. A failure at
any point leaves Offline Mode authoritative and cached/local games visible; no
partial full library is exposed. SaveSync data is validated but never uploaded
or downloaded merely by changing modes.

The transition does not delete catalog rows, cached bytes, cache status, pin
state, local saves, local ROMs, or unrelated `.romcloud` files. Offline Mode
is unavailable in Direct/NAS mode because that access strategy fundamentally
depends on the provider.

The active `nas` or `offline` state is stored atomically in
`/userdata/system/romcloud/data/library-view.json` and remains authoritative
across reboot, relaunch, refresh, repair, and update reconciliation. Switching
successfully to Direct/NAS commits NAS Mode; switching back to Smart Cache
therefore starts with the normal full-library presentation.

The default cache is:

```text
/userdata/romcloud/cache
```

The on-disk layout mirrors each game's system-relative source path. Transfers
stage data below `.partial`, verify expected sizes when known, and promote
completed assets into their final locations. Interrupted game transfers leave
staging data in place. A later attempt reuses complete files/assets, while an
incomplete individual file is copied again from its beginning.

Two limits govern automatic eviction:

- `max_size_gb`: maximum tracked cache usage;
- `min_free_gb`: free space that must remain on the cache filesystem.

Before caching a game, ROMCloud evicts least-recently-used eligible entries
until both limits can be satisfied. It never automatically evicts a game that
is pinned, transferring, or currently launching. If protected entries leave
insufficient capacity, the launch fails with a space diagnostic instead of
deleting them.

Useful cache commands:

```bash
romcloud cache status
romcloud cache add <game-id>
romcloud cache remove <game-id>
romcloud cache pin <game-id>
romcloud cache unpin <game-id>
```

Pinning protects a cached game from ROMCloud's automatic eviction. It does not
copy an uncached game by itself and cannot protect against manual filesystem
deletion or storage failure.

### Offline behavior

A complete cache hit is selected before ROMCloud checks source reachability, so
it can launch while the NAS/share/source is unavailable. This assumes the
local config, proxy, catalog information, complete cache assets, and Batocera
emulator dependencies remain intact. An uncached or incomplete game cannot be
launched until the source becomes reachable again.

ROMCloud does not promise that every cached game is permanently offline:
unpinned entries remain eligible for LRU eviction, and external emulators or
games may have their own network requirements.

## SaveSync

SaveSync v1 is **manual and directional**. It does not automatically sync on
game launch/exit, merge simultaneous changes, or perform bidirectional conflict
resolution.

Both the GUI and CLI support:

- status and last-successful-operation information;
- a complete upload or download preview;
- added, changed, removed, and unchanged counts;
- explicit confirmation before committing;
- staged copying, size and SHA-256 verification, and transactional directory
  replacement;
- conservative recovery of interrupted commits;
- state advancement only after a successful commit.

The remote dataset is always:

```text
<remote_data.root>/saves/
```

The user chooses the general writable data root; ROMCloud owns the `saves`
dataset and its sibling transaction artifacts. For SMB, this is a separate
read/write mount. SaveSync is unavailable when no writable remote-data target
has been configured or when its write/read/cleanup probe fails.

CLI examples:

```bash
romcloud saves status
romcloud saves preview-upload
romcloud saves upload
romcloud saves preview-download
romcloud saves download
```

Selection is deliberately narrower than “everything under
`/userdata/saves`.” Current rules include audited root-level RetroArch `.srm`
files, standalone N64 and N64DD native formats, NDS `.sav`, MAME NVRAM,
DuckStation memory cards, PCSX2 memory cards, PPSSPP savedata, Xbox 360 content,
and validated Yuzu per-account/per-title save descendants. Unlisted systems
and unrelated savestates, firmware, keys, caches, configuration, logs, and
shared disk images are excluded.

Original Xbox is disabled by default because xemu stores progress inside the
entire `xbox_hdd.qcow2` virtual drive. Enabling it transfers that whole opaque
file; ROMCloud does not inspect or merge it.

## Updates

ROMCloud has a Git-free updater. Use **Maintenance → Check for Updates** and
**Update ROMCloud**, or run:

```bash
romcloud update --check
romcloud update
```

An update resolves a GitHub commit, downloads and safely extracts its archive,
upgrades the persistent virtual environment, and reconciles installed wrappers,
the graphical payload, the Ports entry, and previously enabled Batocera
integrations. Configuration, credentials, catalog data, cache, logs, proxies,
and SaveSync data are not replaced wholesale.

Required wrapper reconciliation must succeed before the new build metadata is
recorded. Graphical and other optional Batocera integrations remain
best-effort, so an environment without a compatible system Python/pygame can
still update the backend and CLI.

### Automatic GUI restart after an update

For a GUI-initiated update, the old graphical process waits for the updater's
final successful result, which occurs only after installation, reconciliation,
version persistence, and update cleanup. It then:

1. shows `ROMCloud updated successfully. Restarting…`;
2. stops accepting normal application actions;
3. closes GUI diagnostics and Pygame;
4. launches the installer-managed
   `/userdata/system/romcloud/bin/romcloud-ports` wrapper exactly once; and
5. exits so the replacement process imports the newly installed GUI code.

A failed update does not trigger a restart. If the update succeeded but the
launcher cannot be started, the old GUI still exits and writes a diagnostic to
`/userdata/system/romcloud/logs/gui-relaunch.log`; reopen ROMCloud manually
from Batocera's Ports menu.

## Repair, uninstall, and purge

These lifecycle workflows are currently CLI commands:

| Command | Behavior |
| --- | --- |
| `romcloud repair` | Rewrites required launch wrappers, reconciles the installed graphical payload and enabled integrations, and restores missing ROMCloud-owned proxies when retained catalog records permit. It does not delete user data. If the virtual environment is missing, rerun the bootstrap installer. |
| `romcloud uninstall` | Stops the mount worker, unmounts configured SMB targets, removes the service, EmulationStation override, Ports entry/artwork, generated proxies, runtime wrappers, virtual environment, graphical payload, and build metadata. It preserves configuration, canonical credentials, catalog/data, cache, logs, and external SaveSync data so reinstall/repair remains possible. Derived CIFS mount credential files are removed. |
| `romcloud purge` | Performs uninstall and then permanently removes ROMCloud-owned configuration, credentials, catalog/data, cache, logs, and runtime state. It preserves real source ROMs, ordinary local ROMs, unrelated Batocera files, and user-controlled remote SaveSync data. Safety checks refuse broad or overlapping destructive targets. |

Uninstall and purge ask for confirmation unless `--yes` is supplied. If an SMB
target cannot be unmounted, uninstall stops before deleting runtime state so
the operation can be diagnosed and retried.

## Network availability and retries

At Batocera boot, ROMCloud's custom service starts a detached mount worker and
returns immediately; network, DNS, Tailscale, NAS, or CIFS delays therefore do
not block the boot sequence. The worker retries unavailable SMB targets with
bounded attempts and backoff for an overall default budget of five minutes.
It uses a single-instance lock and records its last status.

This is a startup retry window, not a guarantee that a NAS will reconnect at
any future time. If the source becomes available later, use
**Storage → Mount / Reconnect** or:

```bash
romcloud mount start
```

Expected behavior when storage is unavailable:

- EmulationStation and ordinary local games continue to work;
- complete cached ROMCloud games can still launch;
- uncached/incomplete games fail with a source-unreachable diagnostic;
- catalog refresh fails without deleting the existing catalog merely because
  the source root could not be scanned;
- SaveSync refuses to proceed unless its configured data target is actually
  reachable and writable.

## CLI reference

The installer intentionally does not add ROMCloud to `PATH`. Examples below
use `romcloud` for readability; the default full path is
`/userdata/system/romcloud/bin/romcloud`.

```text
romcloud configure                 Guided source/cache/synchronization setup
romcloud status                    Catalog and cache summary
romcloud healthcheck               Source, cache, integration, and SaveSync checks
romcloud refresh [--system NAME]   Refresh catalog and EmulationStation registration
romcloud launch PROXY              Resolve/cache a proxy manually
romcloud cache ...                 Status, add, remove, pin, and unpin
romcloud library ...               Cached-only/full Smart Cache presentation
romcloud library-sync ...          Metadata/media status, pull, push, sync, removal
romcloud saves ...                 SaveSync status, previews, upload/download, Xbox opt-in
romcloud mount ...                 Install/start/stop/status/remove SMB mount service
romcloud es ...                    Install/refresh/status/remove ES integration
romcloud update [--check]          Check for or install an update
romcloud repair                    Reconcile installed runtime artifacts
romcloud uninstall                Remove runtime/integration; preserve recoverable data
romcloud purge                    Remove all ROMCloud-owned persistent local state
```

Run `romcloud <command> --help` for command-specific options.

## Configuration example

The graphical setup flow normally writes this file. A representative SMB
source plus independent SMB SaveSync target looks like:

```toml
[source]
provider = "local"
rom_root = "/userdata/romcloud/source"

[game_access]
# Use "direct_nas" to play from the source without local game caching.
mode = "smart_cache"

[cache]
path = "/userdata/romcloud/cache"
max_size_gb = 50.0
min_free_gb = 5.0

[logging]
level = "INFO"
path = "/userdata/system/romcloud/logs"

[local_roms]
path = "/userdata/roms"

[data]
path = "/userdata/system/romcloud/data"

[smb]
server = "rom-nas"
share = "Roms"
username = "reader"
port = 445
# remote_path = "optional/folder/inside/share"

[remote_data]
provider = "smb"
root = "/userdata/romcloud/remote"

[remote_data.smb]
server = "backup-nas"
share = "ROMCloud"
username = "writer"
port = 445
# remote_path = "optional/data/folder"

[saves]
local_path = "/userdata/saves"
xbox_enabled = false

[library_sync]
enabled = false
```

`source.provider = "local"` is correct for both ordinary directories and SMB:
the current SMB architecture mounts the share first and accesses that mount
through the local-filesystem provider.

## Paths and logs

Default paths:

```text
/userdata/system/romcloud/
├── bin/                         CLI and Batocera wrappers
├── config/romcloud.toml         Main configuration
├── config/credentials.toml      Passwords, mode 0600
├── data/catalog.db              Catalog/cache/proxy ownership database
├── data/direct-links.json       Verified Direct/NAS symlink ownership manifest
├── data/library-view.json       Active cached-only Smart Cache presentation
├── data/library/library.json    Local canonical Library Sync working copy
├── data/library-sync-state.json Last Library Sync result
├── data/savesync-state.json     Last successful SaveSync records and device ID
├── logs/romcloud.log            Rotating application log
├── logs/mount-worker.log        Detached SMB worker output
├── logs/gui-relaunch.log        Automatic GUI relaunch failures
├── logs/gui-display.log         Ports-to-Pygame display handoff timings
├── run/mount-worker.status.json Last mount-worker status
├── ports-gfx/                   Installed graphical payload
└── venv/                        Isolated backend Python environment

/userdata/romcloud/
├── source/                      Default read-only SMB source mount
├── remote/                      Default read/write SMB data mount
└── cache/                       Local managed game cache
```

Optional controller diagnostics are written to
`/userdata/system/romcloud/logs/controller-debug.log` only when
`ROMCLOUD_PORTS_GFX_INPUT_DEBUG=1` is set in the graphical launcher
environment. The file records controller events and identity details, not text
input or backend credentials, and is truncated at each enabled launch.

## Troubleshooting

Start with:

```bash
/userdata/system/romcloud/bin/romcloud healthcheck
/userdata/system/romcloud/bin/romcloud status
/userdata/system/romcloud/bin/romcloud mount status
```

### ROMCloud is missing from Ports

- Update EmulationStation's game lists or restart EmulationStation.
- Confirm `/userdata/roms/ports/ROMCloud.sh` exists.
- The GUI is best-effort and requires a system Python that can import pygame;
  the CLI remains usable if it was skipped.
- Run `romcloud repair` to reconcile a damaged Ports entry or wrapper.

### The NAS is unavailable after boot

- Check `run/mount-worker.status.json` and `logs/mount-worker.log`.
- Confirm the server address, share, optional `remote_path`, and credentials.
- Run `romcloud mount start` after network connectivity is available.
- If only one cached game is needed, try launching it: a complete cache hit
  does not require the source mount.

### A game transfer was interrupted

Launch the game again. ROMCloud preserves `.partial` staging and reuses files
that already match their expected complete size. An incomplete individual file
is copied again from its beginning, while completed assets of a multi-asset
game are not downloaded again. A size mismatch or missing companion remains an
incomplete cache rather than being launched as if valid.

### New or removed games are not reflected

Run a catalog refresh and then update EmulationStation's game lists. Remember
that completely disappeared source games are retained conservatively; refresh
does not treat temporary source absence as permission to delete catalog data.

### A GUI update completed but did not reopen

Review `logs/gui-relaunch.log`, then reopen **Ports → ROMCloud** manually. The
update may still have completed successfully even though starting the
replacement GUI failed.

### The display flickers before ROMCloud appears

Review `logs/gui-display.log`. It records monotonic timestamps for the Ports
entry, launcher wrapper, Python/Pygame initialization, display probing and
creation, selected display path, SDL driver, and relevant X11/Wayland
environment. Compare a fresh **Ports → ROMCloud** launch with an update-driven
restart, then preserve these Batocera logs from the same run:

```text
/userdata/system/logs/es_launch_stdout.log
/userdata/system/logs/es_launch_stderr.log
/userdata/system/logs/display.log
```

The GUI trace includes both UTC and monotonic time once Python starts, allowing
the shell events and Batocera log entries to be aligned with a slow-motion
recording of the display.

## Known limitations

- This is beta software; keep independent backups of important saves and
  configuration.
- Full end-to-end hardware evidence is centered on Batocera 42. Do not assume
  every Batocera release, architecture, controller, display mode, or bundled
  Python/pygame combination has been validated.
- SFTP and direct/native SMB providers are not implemented.
- The graphical UI is optional and depends on Batocera's system Python/pygame.
- Catalog refresh is automatic during setup but not periodic afterward.
- A source game that disappears is not automatically removed from the catalog.
- Offline launch applies only to complete cached games with intact local
  metadata and emulator dependencies.
- SaveSync is manual, directional, whole-dataset replacement with an explicit
  audited selection policy; it is not continuous sync or conflict merging.
- Library Sync is opt-in beta functionality. Its XML merge/render behavior and
  media-path compatibility still require clean-state validation on real
  Batocera/EmulationStation hardware in both access modes.
- Original Xbox SaveSync transfers the complete xemu virtual disk and is
  opt-in. Some structured emulator save layouts remain unsupported until they
  are validated.
- `.m3u` and CCD/IMG/SUB companion parsing are not implemented. BIN/CUE is.
- EmulationStation is not automatically restarted after setup or catalog
  refresh; update its game lists or restart it manually.
- GUI update relaunch behavior still requires confirmation across real
  Batocera/SDL environments.

## Development

ROMCloud targets Python 3.10 or newer. From a development checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The test suite uses temporary filesystem layouts and mocked process/network
boundaries; automated tests do not mount real SMB shares, restart
EmulationStation, or relaunch the test runner.

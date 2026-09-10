# ROMCloud

> **Your Batocera library, without keeping your entire ROM collection on
> the device.**

ROMCloud makes games stored on a NAS, another PC, or external storage
appear in Batocera's normal EmulationStation library. Use the source
directly, cache games locally as you play them, or take selected cached
games offline.

> [!IMPORTANT]
> ROMCloud is currently beta software. Keep independent backups of important
> saves and configuration.

## Install on Batocera

Press F1 on the EmulationStation system select screen and open Applications from the sidebar.
From there, open xterm.

Run:

```bash
curl -fsSL https://romcloud.app/install | bash
```

This installs the stable channel. Development/test machines can instead use:

```bash
curl -fsSL https://romcloud.app/install | bash -s -- --channel develop
```

After installation, refresh EmulationStation's game list if needed and open
**Ports → ROMCloud**. The graphical setup wizard handles normal configuration;
typical users do not need to edit configuration files or use the CLI.

## Why ROMCloud?

A large ROM collection can easily exceed the storage available on a handheld
or small Batocera device. ROMCloud lets Batocera keep behaving like Batocera
while the bulk of the library lives somewhere else.

- **Browse your library normally.** Remote games appear in EmulationStation
  alongside ordinary local games.
- **Cache games automatically.** In Cached Storage, launching an uncached game
  transfers and verifies it before handing it to Batocera.
- **Keep storage bounded.** Configure a cache size and minimum free-space
  reserve; ROMCloud uses LRU eviction and supports pinning.
- **Play cached games without the source.** Complete cached games can launch
  without the NAS or remote server being reachable.
- **Use filesystem-backed sources directly.** Direct launches from a configured
  local or mounted source instead of duplicating games into the cache.
- **Use SFTP without mounting it.** SFTP sources are accessed through a native
  protocol provider and use Cached Storage.
- **Take a smaller library offline.** Offline exposes only games whose required
  assets are already cached.
- **Keep Batocera in control.** ROMCloud does not replace EmulationStation,
  emulator configuration, or `emulatorlauncher`.
- **Keep local games local.** Ordinary ROMs already on the Batocera device
  remain untouched.
- **Optional metadata and save synchronization.** Library Sync and SaveSync add
  cross-device continuity without being required for normal ROM access.
- **Controller-friendly management.** Setup, storage, modes, catalog refresh,
  synchronization, diagnostics, and updates are available through the Ports UI.

## Three operating modes

ROMCloud has one explicit operating mode at a time. Losing network connectivity
does **not** silently change the selected mode.

- **Direct**
  - What you see: Managed source library.
  - How games launch: Directly from the configured filesystem-backed source.
  - Source required: Yes.
- **Cached Storage**
  - What you see: Full managed library.
  - How games launch: Games are copied into ROMCloud-managed local storage as
    needed, then launched locally.
  - Source required: Only when a game is missing locally.
- **Offline**
  - What you see: Only complete cached games.
  - How games launch: Entirely from local cache.
  - Source required: No.

### Direct

Use this when the NAS, PC, or external source is available through a local
filesystem path and you want to launch games directly from it. Existing cache
files and pins are preserved but are not the normal launch path. Emulator saves
still use Batocera's local save paths; Direct applies only to ROM access.

Protocol-only providers such as SFTP do not expose paths that Batocera's
emulators can open directly, so SFTP ROM sources use Cached Storage instead.

### Cached Storage

Use this when you want the full library visible but prefer games to run from
local storage. Cached games launch immediately. Uncached games are transferred,
verified, and added to the managed cache before launch.

### Offline

Use this when the source will not be available. ROMCloud exposes only managed
games with complete local cache assets. Ordinary local Batocera games remain
available.

> [!NOTE]
> Mode switching changes ROMCloud's presentation of the existing catalog; it
> does not rescan the source library. A real mode change updates the managed
> presentation and requests an EmulationStation restart. Selecting the already
> active mode is a lightweight no-op. Run **Refresh Catalog** when the source
> library itself changes.

## Quick start

### 1. Prepare your ROM source

A normal ROM source contains folders using Batocera system identifiers:

```text
Roms/
├── dreamcast/
├── gamecube/
├── ps2/
├── psx/
├── snes/
└── xbox360/
```

Only folders matching systems known to the installed Batocera system
definitions are cataloged. You do not need to have every system.

If you only want SaveSync for games that already live locally on the Batocera
device, the setup wizard also offers **SaveSync only (local games)** and does
not require a managed ROM source.

### 2. Install ROMCloud

```bash
curl -fsSL https://romcloud.app/install | bash
```

The installer creates an isolated ROMCloud environment under
`/userdata/system/romcloud`. It does not install packages into Batocera's system
Python or modify `/userdata/system/custom.sh`.

### 3. Open ROMCloud

Launch **Ports → ROMCloud**.

The first-run wizard can configure:

- an SMB source, including server/share discovery and credentials;
- an already mounted local or external directory;
- an SFTP source with explicit host-key review and remote-folder browsing; or
- SaveSync only for ordinary local games.

It validates the selected source, detects recognizable systems when applicable,
lets you choose managed systems, configures shared ROMCloud data storage and
cache limits, and builds the initial catalog.

### 4. Choose how you want to play

For filesystem-backed sources, choose **Direct** to launch from the source or
**Cached Storage** to fetch local copies on demand. Choose **Offline** when you
want to expose only complete cached games. SFTP ROM sources use **Cached
Storage** because they do not provide local filesystem semantics.

### 5. Refresh after changing the source library

When ROMs are added or reorganized on the source, use **Library → Refresh
Catalog**. Catalog refresh and operating-mode changes are intentionally
separate operations.

## How ROMCloud works

In Cached Storage:

```text
NAS / PC / SFTP / external storage
            │
            │ complete ROM library
            ▼
     ROMCloud catalog
            │
            │ tiny .romcloud proxies
            ▼
 Batocera EmulationStation
            │
       launch game
            │
      ┌─────┴─────┐
      │           │
   cached      not cached
      │           │
      │      transfer + verify
      │           │
      └─────┬─────┘
            ▼
      local game copy
            │
            ▼
 Batocera emulatorlauncher
```

ROMCloud proxy files are small managed records, not ROM files. A proxy
identifies a cataloged game. On launch, ROMCloud resolves that identity, makes
sure required assets are available locally when Cached Storage requires them,
and hands the real game path to Batocera's normal launcher.

Direct instead exposes a filesystem-backed source through verified
ROMCloud-owned links and launches directly from the configured source.

ROMCloud never owns an entire `/userdata/roms/<system>` directory. It tracks
the files, links, and records it creates and refuses to overwrite unrelated
content.

## Storage support

| Source or destination | Status | Notes |
| --- | --- | --- |
| SMB ROM source | **Supported** | Mounted read-only with CIFS, then accessed through ROMCloud's local-filesystem provider. Supports Direct and Cached Storage. |
| Local/external ROM source | **Supported** | Uses an existing absolute directory. Supports Direct and Cached Storage. |
| SFTP ROM source | **Supported** | Native protocol provider with explicit SSH host-key trust. No local mount; uses Cached Storage only. |
| SMB ROMCloud data | **Supported** | Separate writable target for SaveSync and Library Sync. |
| Local/external ROMCloud data | **Supported** | Explicit writable directory for SaveSync and Library Sync. |
| SFTP ROMCloud data | **Limited** | Can be configured and read through the provider abstraction, but protocol-only SFTP does not provide ROMCloud's required durable transaction semantics, so write-dependent SaveSync/Library Sync operations are unavailable. |
| Google Drive SaveSync | **Experimental / parked** | Phase 1 code is retained, but normal beta setup hides it and synchronization is unsupported. |
| Native/userspace SMB provider | **Not implemented** | Current SMB support uses Batocera's CIFS mount support. |

ROMCloud separates two trust boundaries: the **ROM source** is read-only, while
optional **ROMCloud data storage** used for synchronization is an independent
target with its own validation and credentials. The locations may use different
servers, shares, paths, provider types, and accounts. ROMCloud rejects unsafe
overlapping source/data locations.

SFTP connections are host-key pinned. Setup shows the server fingerprint before
credentials are trusted, and a later mismatch fails closed rather than silently
accepting a changed server identity.

Credentials are kept separately from `romcloud.toml`, written atomically with
restrictive permissions, and excluded from normal logs and process arguments.
The credential store uses versioned authenticated-encryption envelopes when
cryptography support is available, reports its protection level, and migrates
recognized legacy plaintext formats. CIFS mount attempts use short-lived
credentials files that are removed after the mount attempt.

## Smart local cache

The default cache is `/userdata/romcloud/cache`.

ROMCloud tracks complete game assets rather than treating a partially
transferred game as playable. Transfers are staged below `.partial`, verified,
and promoted into their final cache locations. Interrupted transfers retain
useful staging data so a later attempt can reuse already completed assets.

Two settings govern automatic eviction:

- `max_size_gb` — maximum tracked cache usage;
- `min_free_gb` — free space ROMCloud must preserve on the cache filesystem.

When space is required, ROMCloud evicts eligible least-recently-used entries.
It does not automatically evict games that are pinned, transferring, or
currently launching. If protected data leaves insufficient capacity, the
operation fails with a space diagnostic rather than deleting protected content.

Pinning expresses that a game should be kept locally. An already-cached game is
protected from automatic eviction immediately; a remote-only game remains a
zero-byte pinned request until **Download Pinned** runs. Pinning is not a
backup and does not protect against manual deletion or storage failure.

## Browser Library/Cache Manager

ROMCloud includes a browser-based Library/Cache Manager backed by one detached
`romcloud manager` process. The installed Batocera startup service keeps that
manager available after boot, while the native **Library → Library Manager**
screen can recover it on demand if needed.

The manager is lazy: merely running it does not scan the catalog, resolve game
descriptors, poll remote storage, or transfer files. Browser operations request
that work explicitly.

**Open Here** launches the manager in a compatible local Chromium runtime using
a dedicated kiosk profile and loopback-only session. **Pair Another Device**
provides a stable HTTPS URL plus a short-lived single-use pairing code for
another device on the network. Trusted devices can later be revoked
individually or all at once.

The browser is system-first and server-paginated. **Full Library** shows
eligible catalog entries while the source is available; **On This Device**
shows cached, pinned, transferring, failed, or incomplete entries. Offline mode
exposes only the local view. Search, state filters, sorting, multi-select
pin/unpin, downloads, and local-copy removal are available without loading the
entire catalog into the browser.

Controller navigation is supported in the local kiosk session. ROMCloud's
native Ports UI and browser share the same logical actions, while raw SDL
controller mappings remain separate from browser Gamepad API identities. Local
browser controller diagnostics are written to
`/userdata/system/romcloud/logs/browser-controller.log` when enabled by that
session.

**Download Pinned** performs a dependency-aware storage preflight before
starting its background job. The estimate deduplicates shared physical members
and uses the same persisted playlist/XBLA closure, ownership, cache-size limit,
and minimum-free-space reserve as launching and cache removal. A transfer that
would breach either limit is blocked.

The foreground diagnostic command remains available:

```bash
romcloud manager
```

It starts the HTTPS manager and prints the local URL plus an explicitly labeled
Advanced manual token. HTTPS is the default because remote controller access
uses browser secure-context APIs. The server listens on port `8765` by default.

## Catalog and EmulationStation integration

ROMCloud catalogs known games and maintains ownership records for its generated
presentation. In Cached Storage, managed entries look like:

```text
/userdata/roms/psx/Alundra (USA).romcloud
/userdata/roms/ps2/Some Game.romcloud
```

ROMCloud refuses to overwrite or remove unrelated files. Existing local ROMs
can coexist in the same system directories.

The EmulationStation override is stored at:

```text
/userdata/system/configs/emulationstation/es_systems_romcloud.cfg
```

ROMCloud does not modify Batocera's stock
`/usr/share/emulationstation/es_systems.cfg`. The generated integration
preserves Batocera's normal launcher arguments and extensions while adding
ROMCloud's managed launch path.

### Refreshing the catalog

Use **Library → Refresh Catalog** or:

```bash
/userdata/system/romcloud/bin/romcloud refresh
```

Refresh is intentionally conservative: known system directories are scanned
independently; existing logical games should retain stable identities;
temporary source unavailability is not treated as permission to delete the
catalog; and only ROMCloud-owned presentation files may be rewritten or
removed. A mode switch does **not** perform catalog discovery.

## Library Sync

Library Sync is an **optional beta feature** for synchronizing scraped game
metadata and media. It is disabled by default. Its purpose is to maintain
provider-neutral canonical metadata while rendering paths appropriate to the
current operating mode.

Library Sync can synchronize names, descriptions, ratings, release dates,
developer/publisher/genre/player metadata, artwork, and supported media. Source
`gamelist.xml` files are read-only to ROMCloud and are never rewritten.

Canonical data lives under the configured ROMCloud data target:

```text
<remote_data.root>/library/library.json
<remote_data.root>/library/media/sha256/...
```

ROMCloud uses stable game identity independent of the local mount root or
current access mode. Media is content-addressed. Routine `pull`, `push`, and
`sync` operations use Quick reconciliation: missing payloads are copied, while
an existing ordinary destination is skipped without comparing its size,
timestamps, hash, or contents. Canonical metadata is still merged and updated
on every operation.

Pass `--full` to `pull`, `push`, or `sync` for the explicit expensive repair
path. Full reconciliation validates existing payloads and can replace a corrupt
file or changed media stored under an existing logical source path.

The merge policy is additive and conservative: missing information may be
filled; blank values do not delete canonical data; conflicting non-empty values
preserve the existing canonical value and report the conflict; and unrelated
local EmulationStation entries are preserved.

Library Sync does not run merely because you switch operating modes or refresh
the ROM catalog.

```bash
romcloud library-sync status
romcloud library-sync enable
romcloud library-sync disable
romcloud library-sync pull
romcloud library-sync push
romcloud library-sync sync
romcloud library-sync sync --full
romcloud library-sync remove-local
```

Remote Library Sync operations are unavailable while Offline is active.
Write-dependent operations also require a remote-data provider with ROMCloud's
durable transaction guarantee.

## SaveSync

SaveSync is an **optional beta synchronization system** for eligible
game-progress data.

> [!WARNING]
> Keep independent backups of important saves while SaveSync remains beta.
> Save synchronization should never be the only copy of irreplaceable progress.

ROMCloud keeps Batocera's emulator-facing save tree local. SaveSync reconciles
eligible data with a canonical dataset at `<remote_data.root>/saves/`. The
shared data location is separate from the read-only ROM source.

### Safety model

SaveSync is designed around a positive layout allowlist and transactional
replacement:

- manual, periodic, and game-lifecycle triggers enter the same verified
  reconciliation path;
- Offline does not poll or modify remote saves;
- conflicts are reported instead of guessed or overwritten;
- eligible saves from ROMCloud-managed and ordinary local games receive the
  same protection; catalog membership is not an eligibility gate;
- replacements are staged and verified before promotion;
- previous known-good content is retained through bounded per-item history
  rather than cloning the entire `/userdata/saves` tree;
- unknown roots and unsupported nested content are not traversed, copied, or
  deleted; and
- per-group dirty/conflict evidence survives GUI sessions and reboots, and
  acknowledging a conflict does not resolve it.

SaveSync deliberately does **not** mean "synchronize everything under
`/userdata/saves`." Discovery starts only at audited layout roots. Ambiguous
emulator-wide data, generated/cache content, firmware/keys, and other
unsupported content are ignored unless a specific supported workflow says
otherwise.

The GUI provides SaveSync status, preview, upload, download, Quick Sync, and
conflict-resolution workflows. CLI examples:

```bash
romcloud saves status
romcloud saves reconcile
romcloud saves preview-upload
romcloud saves upload-all
romcloud saves preview-download
romcloud saves download-all
```

The SaveSync dashboard renders local/configured state immediately. Remote-data
availability is checked separately with a bounded background probe so Back and
application Exit stay responsive when storage is missing.

### Auto SaveSync lifecycle

When Auto SaveSync is enabled in Direct or Cached Storage, ROMCloud's Batocera
lifecycle integration records a crash-safe game session at `gameStart`. On an
eligible `gameStop`, ROMCloud shows a short progress overlay, scopes work to the
audited layouts associated with that system/emulator, observes the actual save
content until it is stable, records verified local changes, and completes Quick
Sync synchronously before the lifecycle operation reports success.

In practical terms, the normal automatic path is:

```text
emulator exits
    ↓
scoped save observation + stability check
    ↓
local dirty-state persistence
    ↓
Quick Sync reconciliation
    ↓
remote transaction/journal + local baseline/cursor update
    ↓
return to EmulationStation
```

Quick Sync uses durable dirty/group state plus the remote change journal to
avoid rescanning every eligible save on every trigger. The lifecycle path still
verifies the layouts belonging to the game that actually stopped rather than
trusting stale hints alone.

If both local and remote content changed from the same known baseline, ROMCloud
records a durable conflict and queues it for explicit resolution instead of
overwriting either side. While EmulationStation is idle, bounded periodic and
reconnect Quick Sync passes provide receive-side discovery and can repair a
missing local materialization. Offline keeps local dirty work pending until an
online mode is restored.

There is not yet a hard remote Quick Sync barrier immediately before every game
launch, so a remote conflict discovered only after the last receive-side check
may still be surfaced later rather than blocking that launch.

The audited registry covers common root-level RetroArch save/state formats and
structured emulator layouts including Azahar title saves, Dolphin GameCube/Wii
saves and states, Cemu title saves, PPSSPP savedata/states, Vita3K title saves,
RPCS3 savedata, Flycast VMU images, and Ymir backup RAM/save states. Equivalent
Yuzu-derived account/title save trees use one canonical remote namespace. On
conventional Batocera layouts, explicitly detected Eden or Citron NAND save
roots and Ymir's separate persistent-state root are mapped back to their
emulator-visible physical locations; keys, firmware/system NAND, caches,
shaders, logs, configuration, unrelated NAND content, Ymir dumps/exports, and
Ymir's non-save SMPC state remain excluded.

These newer mappings are unit-tested but still require emulator-on-hardware
qualification before they should be treated as fully validated. See the
[popular-system coverage matrix and hardware plan](docs/savesync-coverage.md).

### Memory-card merging

SaveSync can reconcile independent saves inside structurally valid 128 KiB raw
PS1 cards and marker-verified PCSX2 Folder Memory Cards. PS1 files are grouped
conservatively by their documented commercial game namespace. A PCSX2 folder
with multiple structural entries remains opaque unless trustworthy grouping
metadata can completely classify it. Invalid, unsupported, or ambiguous cards
retain opaque whole-card behavior, as do monolithic PCSX2 `.ps2` cards.
Batocera/DuckStation/PCSX2 hardware qualification remains pending.

### Platform-specific notes

**Original Xbox:** xemu stores progress inside the complete
`xbox_hdd.qcow2` virtual disk. Support is disabled by default because
synchronizing it means transferring the whole opaque file.

**RPCS3 installed games:** installed titles, patches, firmware, caches,
configuration, and logs are never SaveSync content. Only explicitly registered
RPCS3 save-data, trophy, virtual-memory-card, and savestate layouts participate.

### Google Drive foundation

Google Drive is not a beta-supported provider and is omitted from normal setup.
The retained Phase 1 implementation includes the controller device-authorization
UX, `drive.file` scope, app-owned root discovery/readiness checks, secure token
state, and Drive object primitives. Full SaveSync synchronization is not
implemented.

The feature is parked because Google's limited-input token exchange requires an
OAuth client secret. ROMCloud will not distribute that credential through Git,
a public URL, a release artifact, portable configuration, or a Batocera runtime
file, and the beta will not depend on a ROMCloud-operated authentication
service. Developer-only exposure does not make Google Drive a supported beta
provider.

## Diagnostics

ROMCloud writes structured diagnostics to `<data.path>/diagnostics.db`. This is
the primary support/audit log; rotating text logs remain as a fail-open fallback.
The database is bounded and uses correlated operation IDs so GUI, CLI,
lifecycle, and worker events belonging to one workflow can be inspected as a
single chain.

Open **Maintenance → Diagnostics / Logs** to browse newest operations and raw
events, filter by subsystem/level/time/search, and inspect an operation in
chronological order. SaveSync operations include scoped observations,
classification, reconciliation decisions, physical mutations, remote journal
commits, baseline/cursor advancement, and final outcome without recording save
file contents.

For Batocera lifecycle troubleshooting, start with:

```text
/userdata/system/romcloud/logs/auto-savesync-lifecycle.log
```

The corresponding structured operation can then be inspected in Diagnostics by
its operation ID. See [docs/diagnostics.md](docs/diagnostics.md) for the detailed
schema, retention, redaction, and event model.

## Graphical interface

The Ports interface is the normal way to configure and maintain ROMCloud on
Batocera. It provides operating-mode selection, catalog status/refresh, cache
controls, storage configuration, SaveSync, Library Sync, health checks,
controller diagnostics/remapping, updates, and maintenance operations.

Long-running backend operations run outside the graphical event loop so the UI
can continue processing controller, keyboard, and touch input.

The GUI is installed when the Batocera environment provides a compatible system
Python/Pygame combination. If it cannot be installed, the backend, CLI, cache,
launcher, and storage functionality remain available.

## Updates

ROMCloud includes a Git-free updater. Use **Maintenance → Check for Updates** or:

```bash
romcloud update --check
romcloud update
```

Ordinary updates stay on the machine's persisted channel. To switch an existing
installation, use:

```bash
romcloud update --channel develop
romcloud update --channel stable
```

The channel selection is persisted only after a successful update. Only
`stable` and `develop` are accepted. Stable currently resolves to the `main`
source line; develop resolves to `develop`.

Updates reconcile ROMCloud-owned runtime files and integrations while
preserving configuration, credentials, catalog data, cache, logs, proxies, and
synchronization data. A successful GUI-initiated update can relaunch the
installed ROMCloud GUI; failed updates do not request a relaunch.

## Repair, uninstall, and purge

| Command | Behavior |
| --- | --- |
| `romcloud repair` | Downloads and reconciles ROMCloud-owned runtime artifacts from the configured channel without deleting user data. |
| `romcloud uninstall` | Removes runtime/integration components while preserving recoverable ROMCloud configuration/data/cache. |
| `romcloud purge` | Removes ROMCloud-owned persistent local state as well as the installed runtime. |

`purge` is intentionally much more destructive than `uninstall`. Neither
workflow should delete real source ROMs, ordinary local ROMs, unrelated
Batocera files, or user-controlled remote synchronization data. Safety checks
refuse broad or overlapping destructive targets.

## Network behavior

ROMCloud is designed so unavailable network storage does not make Batocera
itself unavailable. SMB mounts use an independent bounded worker rather than
blocking Batocera startup. SFTP uses bounded direct protocol connections and
does not maintain a kernel mount or persistent reconnect daemon.

Expected behavior when a ROM source is unavailable:

- EmulationStation and ordinary local games continue to work;
- complete cached ROMCloud games can launch in Cached Storage or Offline;
- uncached games cannot be fetched;
- catalog refresh fails without treating temporary source absence as permission
  to erase the catalog; and
- SaveSync/Library Sync remote operations depend on the separately configured
  ROMCloud data target and its validated capabilities.

If an SMB source becomes available after startup, reconnect from the Storage
screen or run `romcloud mount start`.

## CLI reference

The installer intentionally does not add ROMCloud to the global `PATH`.
Examples use `romcloud` for readability; the default executable is
`/userdata/system/romcloud/bin/romcloud`.

```text
romcloud configure                  Guided configuration
romcloud status                     Catalog/cache summary
romcloud healthcheck                Source, cache, integration, and sync checks
romcloud refresh [--system NAME]    Refresh the catalog
romcloud library ...                Direct/Cached Storage/Offline mode
romcloud cache ...                  Cache status/add/remove/pin/unpin
romcloud manager                    Browser Library/Cache Manager
romcloud library-sync ...           Metadata/media synchronization
romcloud saves ...                  SaveSync operations
romcloud mount ...                  SMB mount management
romcloud sftp fingerprint HOST      Inspect an SFTP server host-key fingerprint
romcloud es ...                     EmulationStation integration
romcloud update [--check] [--channel stable|develop]
                                    Update ROMCloud on/switch to a channel
romcloud repair                     Repair from the configured channel
romcloud uninstall                  Remove runtime; preserve recoverable data
romcloud purge                      Remove ROMCloud-owned local state
```

The actual operating-mode CLI subcommands remain
`romcloud library connected`, `romcloud library cache`, and
`romcloud library offline`; their user-facing mode names are Direct, Cached
Storage, and Offline.

Run `romcloud <command> --help` for command-specific options.

## Advanced configuration

The graphical setup flow normally writes the configuration. A representative
filesystem/SMB-oriented layout is:

```toml
update_channel = "stable"

[source]
provider = "local"
rom_root = "/userdata/romcloud/source"

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

[remote_data]
provider = "smb"
root = "/userdata/romcloud/remote"

[remote_data.smb]
server = "backup-nas"
share = "ROMCloud"
username = "writer"
port = 445

[saves]
local_path = "/userdata/saves"
xbox_enabled = false

[library_sync]
enabled = false
```

`source.provider = "local"` is also used for the current SMB architecture
because ROMCloud mounts the SMB source first and then accesses that mounted
filesystem through its local-filesystem provider. SFTP instead uses
`source.provider = "sftp"` and a remote POSIX `rom_root`; it is accessed
through the SFTP provider directly and is therefore Cached Storage-only.
Passwords are stored outside this main configuration.

## Important paths

```text
/userdata/system/romcloud/
├── bin/                         CLI and Batocera wrappers
├── config/romcloud.toml         Main configuration
├── config/credentials.toml      Credential store
├── data/catalog.db              Catalog/cache/proxy ownership database
├── data/diagnostics.db          Structured diagnostics/audit database
├── data/direct-links.json       Direct link ownership
├── data/library-view.json       Current operating mode
├── data/library/                Local Library Sync state
├── logs/                        Rotating/fallback logs
├── ports-gfx/                   Installed graphical UI
└── venv/                        Isolated backend Python environment

/userdata/romcloud/
├── source/                      Default read-only SMB source mount
├── remote/                      Default read/write filesystem data mount
└── cache/                       Managed local game cache
```

## Troubleshooting

Start with:

```bash
/userdata/system/romcloud/bin/romcloud healthcheck
/userdata/system/romcloud/bin/romcloud status
```

For SMB-specific mount problems also run:

```bash
/userdata/system/romcloud/bin/romcloud mount status
```

### ROMCloud is missing from Ports

- Refresh EmulationStation's game lists or restart EmulationStation.
- Confirm `/userdata/roms/ports/ROMCloud.sh` exists.
- Run `romcloud repair` to reconcile a damaged Ports entry or wrapper.
- If the GUI could not be installed because Pygame is unavailable, the CLI
  remains usable.

### The NAS or remote source is unavailable

- Check ROMCloud's Storage status and Diagnostics / Logs.
- For SMB, verify the server, share, optional subdirectory, credentials, and
  mount-worker state; run `romcloud mount start` after connectivity returns.
- For SFTP, verify host/port/account/path and confirm the presented host key
  still matches the fingerprint trusted during setup.
- Complete cached games do not require the ROM source merely to read their
  cached assets.

### A transfer was interrupted

Launch the game again. ROMCloud retains useful staging data and can reuse
already completed assets. Incomplete files are not treated as valid games.

### New games are missing

Run **Refresh Catalog**, then refresh/restart EmulationStation if necessary. A
source game disappearing is not automatically treated as permission to delete
its catalog record.

### Auto SaveSync did not move a save

Open **Maintenance → Diagnostics / Logs** and inspect the newest SaveSync
operation. For lifecycle problems, also inspect
`/userdata/system/romcloud/logs/auto-savesync-lifecycle.log`. The correlated
operation records whether the hook arrived, which layouts were eligible, what
was observed, why a candidate was included/excluded, and whether the journal or
cursor allowed an early return.

### An update completed but ROMCloud did not reopen

Reopen **Ports → ROMCloud** manually and inspect
`/userdata/system/romcloud/logs/gui-relaunch.log`.

## Known beta limitations

- ROMCloud is beta software; keep independent backups of important saves and
  configuration.
- Hardware validation is not exhaustive across every Batocera release,
  architecture, controller, display environment, or bundled Python/Pygame
  combination.
- Native/userspace SMB is not implemented; SMB currently relies on Batocera's
  CIFS mount support.
- SFTP ROM sources are Cached Storage-only. Protocol-only SFTP remote-data
  targets cannot provide the durable transaction semantics required for
  SaveSync/Library Sync writes.
- The graphical interface depends on a compatible Batocera system
  Python/Pygame environment.
- Catalog refresh is explicit after setup rather than continuously watching the
  source.
- Source games that disappear are retained conservatively rather than
  automatically deleted.
- Offline play requires complete, intact local cache assets and normal local
  emulator dependencies.
- SaveSync remains beta. Its Batocera lifecycle/periodic reconciliation is
  implemented, but each emulator layout still needs representative hardware
  qualification; data it cannot safely classify or attribute remains excluded.
- Auto SaveSync does not yet impose a hard receive-side Quick Sync barrier
  immediately before every game launch.
- Library Sync remains opt-in beta functionality.
- Google Drive synchronization remains parked and unsupported for the beta.
- MS-DOS library/cache behavior is not yet beta-supported or audited.
- Original Xbox SaveSync requires transferring xemu's complete virtual disk and
  is disabled by default.
- Some multi-file/playlist formats still require additional hardware validation
  or support.

## Safety and design principles

**Batocera stays in control.** ROMCloud integrates with EmulationStation and
`emulatorlauncher`; it does not replace them.

**The ROM source is read-only.** Normal ROM access must not modify the source
library.

**Local games stay local.** ROMCloud tracks and removes only artifacts it owns.

**Offline means offline.** Selecting Offline must not quietly depend on remote
storage.

**Cache state is not catalog identity.** Evicting a game should not make
ROMCloud forget what the game is.

**Synchronization must fail safely.** Ambiguous save ownership, conflicts,
incomplete transfers, unavailable storage, or insufficient provider durability
should result in a clear failure rather than guessed destructive behavior.

**Secrets are not configuration.** Passwords, tokens, private keys/passphrases,
and similar credentials belong in ROMCloud's credential/secret storage rather
than `romcloud.toml`, logs, release artifacts, or portable configuration.

**ROMCloud may fail; Batocera must not.** A broken mount, unavailable NAS,
failed update, or ROMCloud error should not prevent ordinary Batocera use.

## Development

ROMCloud targets Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Automated tests use temporary filesystem layouts and mocked process/network
boundaries. Real Batocera hardware remains necessary for final validation of
platform-specific behavior.

## Project status

ROMCloud is under active development and preparing for wider public beta use.
Bug reports and hardware validation are especially valuable around clean
installs and upgrades, large catalogs, unavailable network storage, cache
recovery/eviction, SFTP, Library Sync, SaveSync, and different Batocera
releases and hardware platforms.

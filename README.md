# ROMCloud

> **Your Batocera library, without keeping your entire ROM collection on
> the device.**

ROMCloud makes games stored on a NAS, another PC, or external storage
appear in Batocera's normal EmulationStation library. Use the source
directly, cache games locally as you play them, or take selected cached
games offline.

> \[!IMPORTANT\] ROMCloud is currently beta software. Keep independent
> backups of important saves and configuration.

## Install on Batocera

Press F1 on the Emulation Station system select screen and open Applications from the side bar.
From there, open xterm.

Run the following command:

``` bash
curl -fsSL https://romcloud.app/install | bash
```

This installs the stable channel. Development/test machines can instead use:

``` bash
curl -fsSL https://romcloud.app/install | bash -s -- --channel develop
```

After installation, refresh EmulationStation's game list if needed and
open **Ports → ROMCloud**. The graphical setup wizard handles normal
configuration; typical users do not need to edit configuration files or
use the CLI.

## Why ROMCloud?

A large ROM collection can easily exceed the storage available on a
handheld or small Batocera device. ROMCloud lets Batocera keep behaving
like Batocera while the bulk of the library lives somewhere else.

-   **Browse your library normally.** Remote games appear in
    EmulationStation alongside ordinary local games.
-   **Cache games automatically.** In Cached Storage, launching an uncached
    game transfers and verifies it before handing it to Batocera.
-   **Keep storage bounded.** Configure a cache size and minimum
    free-space reserve; ROMCloud uses LRU eviction and supports pinning.
-   **Play cached games without the NAS.** Complete cached games can
    launch without the source being reachable.
-   **Use your source directly.** Direct launches from the
    configured source instead of duplicating games into the cache.
-   **Take a smaller library offline.** Offline exposes only games
    whose required assets are already cached.
-   **Keep Batocera in control.** ROMCloud does not replace
    EmulationStation, emulator configuration, or `emulatorlauncher`.
-   **Keep local games local.** Ordinary ROMs already on the Batocera
    device remain untouched.
-   **Optional metadata and save synchronization.** Library Sync and
    SaveSync add cross-device continuity without being required for
    normal ROM access.
-   **Controller-friendly management.** Setup, storage, modes, catalog
    refresh, synchronization, diagnostics, and updates are available
    through the Ports UI.

## Three operating modes

ROMCloud has one explicit operating mode at a time. Losing network
connectivity does **not** silently change the selected mode.

-   **Direct**
        What you see: Managed source library.
        How games launch: Directly from the configured source.
        Source required: Yes.
-   **Cached Storage**
        What you see: Full managed library.
        How games launch: Games are copied into ROMCloud-managed local
        storage as needed, then launched locally.
        Source required: Only when a game is missing locally.
-   **Offline**
        What you see: Only complete cached games.
        How games launch: Entirely from local cache.
        Source required: No.

### Direct

Use this when the NAS, PC, or external source is available and you want
to launch games directly from it. Existing cache files and pins are
preserved but are not the normal launch path.

### Cached Storage

Use this when you want the full library visible but prefer games to run
from local storage. Cached games launch immediately. Uncached games are
transferred, verified, and added to the managed cache before launch.

### Offline

Use this when the source will not be available. ROMCloud exposes only
managed games with complete local cache assets. Ordinary local Batocera
games remain available.

> \[!NOTE\] Mode switching changes ROMCloud's presentation of the
> existing catalog; it does not rescan the source library. Run **Refresh
> Catalog** when the source library itself changes. Depending on the
> current beta build and Batocera behavior, EmulationStation may need a
> game-list refresh after presentation changes.

## Quick start

### 1. Prepare your ROM source

The selected source root should contain folders using Batocera system
identifiers:

``` text
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

### 2. Install ROMCloud

``` bash
curl -fsSL https://romcloud.app/install | bash
```

The installer creates an isolated ROMCloud environment under
`/userdata/system/romcloud`. It does not install packages into
Batocera's system Python or modify `/userdata/system/custom.sh`.

### 3. Open ROMCloud

Launch **Ports → ROMCloud**.

The first-run wizard can configure an SMB source (server, share,
credentials, and optional folder) or an already mounted local/external
directory. It validates the source, detects recognizable systems,
configures cache limits, and builds the initial catalog.

### 4. Choose how you want to play

For most NAS users, choose **Connected** for direct NAS access,
**Cache** for local copies fetched on demand, or **Offline** before
using only already-cached games without the source.

### 5. Refresh after changing the source library

When ROMs are added or reorganized on the source, use **Library →
Refresh Catalog**. Catalog refresh and operating-mode changes are
intentionally separate operations.

## How ROMCloud works

In Cached Storage:

``` text
NAS / PC / external storage
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
identifies a cataloged game. On launch, ROMCloud resolves that identity,
makes sure required assets are available locally when Cached Storage
requires them, and hands the real game path to Batocera's normal
launcher.

Direct instead exposes the known source library through verified
ROMCloud-owned links and launches directly from the configured source.

ROMCloud never owns an entire `/userdata/roms/<system>` directory. It
tracks the files, links, and records it creates and refuses to overwrite
unrelated content.

## Storage support

  -----------------------------------------------------------------------
  Source or destination   Status                  Notes
  ----------------------- ----------------------- -----------------------
  SMB ROM source          **Supported**           Mounted with CIFS
                                                  read-only, then
                                                  accessed through the
                                                  local-filesystem
                                                  provider

  Local/external ROM      **Supported**           Uses an existing
  source                                          absolute directory

  SMB ROMCloud data       **Supported**           Separate read/write
                                                  target for optional
                                                  SaveSync and Library
                                                  Sync

  Local/external ROMCloud **Supported**           Explicit writable
  data                                            directory

  Google Drive SaveSync   **Experimental /        Phase 1 code is retained,
                          parked**                but hidden and unsupported
                                                  for the beta

  SFTP                    **Not implemented**     No current SFTP
                                                  provider/setup flow

  Native/userspace SMB    **Not implemented**     Current SMB support
  provider                                        uses Batocera's CIFS
                                                  mount support
  -----------------------------------------------------------------------

For SMB setups, ROMCloud deliberately separates two trust boundaries:
the **ROM source** is read-only, while optional **ROMCloud data
storage** for synchronization is explicitly read/write. The two
locations may use different servers, shares, paths, and credentials.
ROMCloud rejects unsafe overlapping source/data locations.

Credentials are stored separately from `romcloud.toml`, atomically, with
restrictive permissions, and are not placed in normal logs or process
arguments.

## Smart local cache

The default cache is `/userdata/romcloud/cache`.

ROMCloud tracks complete game assets rather than treating a partially
transferred game as playable. Transfers are staged below `.partial`,
verified, and promoted into their final cache locations. Interrupted
transfers retain useful staging data so a later attempt can reuse
already completed assets.

Two settings govern automatic eviction:

-   `max_size_gb` --- maximum tracked cache usage;
-   `min_free_gb` --- free space ROMCloud must preserve on the cache
    filesystem.

When space is required, ROMCloud evicts eligible least-recently-used
entries. It does not automatically evict games that are pinned,
transferring, or currently launching. If protected data leaves
insufficient capacity, the operation fails with a space diagnostic
rather than deleting protected content.

Pinning expresses that a game should be kept locally. An already-cached
game is protected from automatic eviction immediately; a remote-only
game remains a zero-byte pinned request until **Download Pinned** runs.
Pinning is not a backup and does not protect against manual deletion or
storage failure.

## Browser Library/Cache Manager

The installed Batocera `romcloud_mount` custom service ensures one detached
`romcloud manager` process during boot; the Library menu also recovers it on
demand if it is unexpectedly absent. It performs no catalog scan, descriptor
resolution, network polling, or transfer until a browser explicitly requests
an operation. Emulator and EmulationStation launches do not own or terminate
the process.

Installing, newly enabling, or changing that startup integration records a
pending activation state. ROMCloud can start the manager immediately for the
current session, but it does not describe future-boot availability as active
until Batocera has restarted and the service's next successful `start`
callback clears the marker. The native UI offers **Restart Now** and **Later**;
Later leaves a visible warning on the menu and prompts again on a later
ROMCloud launch. A failed later-boot manager start is instead shown as a
startup failure with a no-reboot retry command; it is never converted back
into another generic restart prompt. Boot-service execution and command
results are persisted in
`/userdata/system/romcloud/logs/startup-service.log`, while the detached
manager's output and readiness events are in `browser-manager.log`. This
restart requirement applies to the startup service only, not to installation
or removal of the optional local browser runtime.

**Open Here** launches the existing manager page in a Chromium-compatible
runtime with `--kiosk`, a dedicated profile, and the loopback URL. ROMCloud
resolves `ROMCLOUD_BROWSER`, PATH-based `chromium`, `chromium-browser`,
`google-chrome`, or `google-chrome-stable`, known persistent Batocera
AppImages (including
`/userdata/system/add-ons/google-chrome/GoogleChrome.AppImage`), and finally a
versioned ROMCloud-managed runtime. Every candidate must successfully identify
itself as Chromium-compatible through a bounded `--version` probe; a broken
candidate is recorded and skipped. The related Ports launcher is not executed
and the user-installed browser remains user-owned.
Open Here uses no token, pairing code, bootstrap credential, or cookie: only
a loopback peer using a loopback Host is admitted. The local launch pins ROMCloud's self-signed certificate
by SPKI, so no certificate prompt is required. Its inherited display
environment, selected executable, UID, and sandbox flags are recorded in
`/userdata/system/romcloud/logs/browser-open.log`. ROMCloud does not add
`--no-sandbox`. Open Here launch is singleton; B first cancels an active
selection or search, then asks the owned launcher to close the kiosk at the top
level and returns to ROMCloud without stopping the manager. If no compatible
runtime exists, the native screen reports every failed probe explicitly.
If a user-installed Chrome explicitly refuses root execution without
`--no-sandbox`, ROMCloud reports the loss of Chromium process isolation and
offers a controller-selectable, per-launch **Open Here Without Sandbox**
fallback. It is never selected automatically and is prohibited for a
ROMCloud-managed runtime.

**Pair Another Device** keeps the reachable HTTPS URL stable and bookmarkable,
preferring the `.local` hostname when available and showing the LAN IP as a
fallback. ROMCloud displays a four-character, single-use code that expires in
two minutes. The remote screen offers session-only trust, 90-day trust, or
trust until revoked. Failed attempts are rate-limited; devices can be revoked
individually or all at once without restarting the manager. The permanent
bearer remains an Advanced/debug fallback only.

**Maintenance → Local Browser Runtime** reports the independent versioned
runtime under `/userdata/system/romcloud/browser`. Automatic Chrome for
Testing installation remains disabled until its Batocera shared-library and
secure-sandbox behavior passes hardware validation; remote access is not
affected when the local runtime is absent.

The foreground diagnostic command remains available:

``` bash
romcloud manager
```

The command starts HTTPS and prints a local URL plus an explicitly labeled
Advanced manual token. The first remote visit may require accepting
ROMCloud's stable
self-signed certificate; `--tls-cert` and `--tls-key` can supply a
trusted certificate instead. HTTPS is the default because Chromium
requires a secure context for controller access through the Gamepad API.
The server listens on port `8765` by default and runs until the
command is stopped. Use `--host`, `--port`, or
`ROMCLOUD_MANAGER_TOKEN` for an explicit deployment. `--http` is
available for localhost diagnostics, where browsers still consider the
origin trustworthy, but should not be used for remote controller access.

Browsing is system-first and server-paginated. **Full Library** shows
eligible catalog entries while the source is available; **On This
Device** shows cached, pinned, transferring, failed, or incomplete
entries. Offline mode exposes only the local view. Search, state filters,
sorting, multi-select pin/unpin, individual downloads, and local-copy
removal are available without loading the full catalog into the browser.

The same manager can be started without the CLI from **Library → Library
Manager** in the native Ports interface. Singleton ownership is enforced by
an advisory lock plus the listening socket. Runtime discovery and the
permanent bearer are stored in a mode-0600 state file under the ROMCloud data
directory; normal status output omits the bearer. Repair and update stop the
exact recorded manager before replacing runtime files and restart it
afterward. Uninstall stops it before removing the runtime. Shutdown uses
SIGTERM with a five-second bound and SIGKILL only as a final fallback.

Controllers exposed by Chromium with the W3C `standard` mapping are
supported directly. D-pad or left stick moves through systems, controls,
and game rows; South/A confirms; East/B backs out or closes a dialog;
Start/Menu opens local Help/Exit; and LB/RB moves by one
50-game server page. Holding a bumper accelerates to two and then five
pages per repeat, and releasing it stops repetition immediately.
The `interaction=controller` mode is added only to the loopback Open Here URL.
It enlarges the focused row's action surface without changing remote desktop
rows and provides a controller-scoped on-screen keyboard for Search, including
cursor movement, space, backspace, delete, explicit Search, and cancel with
value/focus restoration.

The native Ports UI and browser use the same ROMCloud logical actions
(`up`, `down`, `left`, `right`, `confirm`, `back`, `previous_page`,
`next_page`, and `menu`). **Maintenance → Setup Controller Mapping** is
the only ROMCloud mapping UI. It persists per-controller SDL raw mappings
under `ports-gfx-state/controller_mappings.json`. Chromium's standardized
Gamepad slots are translated centrally to the same logical actions; SDL
raw button numbers are intentionally not copied into the browser because
Gamepad API indices and identities are not equivalent, and a remote
browser may use a different controller. A browser pad without a standard
mapping is reported as unavailable instead of being assigned guessed
buttons. Local Open Here sessions also write bounded, mode-0600 controller
diagnostics to `/userdata/system/romcloud/logs/browser-controller.log`
(rotating the previous file to `.log.1`). The log records secure-context and
Gamepad API initialization, every exposed pad identity/layout, connection
events, meaningful raw input changes, logical actions, and focus changes. It
does not enable controller diagnostics for remote browser sessions.

**Download Pinned** performs a dependency-aware storage preflight before
starting its background job. The estimate deduplicates shared physical
members and uses the same persisted playlist/XBLA closure, ownership,
cache-size limit, and minimum-free-space reserve as launching and cache
removal. A transfer that would breach either limit is blocked.

## Catalog and EmulationStation integration

ROMCloud catalogs known games and maintains ownership records for its
generated presentation. In Cached Storage, managed entries look like:

``` text
/userdata/roms/psx/Alundra (USA).romcloud
/userdata/roms/ps2/Some Game.romcloud
```

ROMCloud refuses to overwrite or remove unrelated files. Existing local
ROMs can coexist in the same system directories.

The EmulationStation override is stored at:

``` text
/userdata/system/configs/emulationstation/es_systems_romcloud.cfg
```

ROMCloud does not modify Batocera's stock
`/usr/share/emulationstation/es_systems.cfg`. The generated integration
preserves Batocera's normal launcher arguments and extensions while
adding ROMCloud's managed launch path.

### Refreshing the catalog

Use **Library → Refresh Catalog** or:

``` bash
/userdata/system/romcloud/bin/romcloud refresh
```

Refresh is intentionally conservative: known system directories are
scanned independently; existing logical games should retain stable
identities; temporary source unavailability is not treated as permission
to delete the catalog; and only ROMCloud-owned presentation files may be
rewritten or removed. A mode switch does **not** perform catalog
discovery.

## Library Sync

Library Sync is an **optional beta feature** for synchronizing scraped
game metadata and media. It is disabled by default. Its purpose is to
maintain provider-neutral canonical metadata while rendering paths
appropriate to the current operating mode.

Library Sync can synchronize names, descriptions, ratings, release
dates, developer/publisher/genre/player metadata, artwork, and supported
media. Source `gamelist.xml` files are read-only to ROMCloud and are
never rewritten.

Canonical data lives under the configured writable ROMCloud data target:

``` text
<remote_data.root>/library/library.json
<remote_data.root>/library/media/sha256/...
```

ROMCloud uses stable game identity independent of the local mount root
or current access mode. Media is content-addressed. Routine `pull`,
`push`, and `sync` operations use Quick reconciliation: missing payloads
are copied, while an existing ordinary destination is skipped without
comparing its size, timestamps, hash, or contents. Canonical metadata is
still merged and updated on every operation.

Pass `--full` to `pull`, `push`, or `sync` for the explicit expensive
repair path. Full reconciliation validates existing payloads and can
replace a corrupt file or changed media stored under an existing logical
source path.

The merge policy is additive and conservative: missing information may
be filled; blank values do not delete canonical data; conflicting
non-empty values preserve the existing canonical value and report the
conflict; and unrelated local EmulationStation entries are preserved.

Library Sync does not run merely because you switch operating modes or
refresh the ROM catalog.

``` bash
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

## SaveSync

SaveSync is an **optional beta synchronization system** for eligible
game-progress data.

> \[!WARNING\] Keep independent backups of important saves while
> SaveSync remains beta. Save synchronization should never be the only
> copy of irreplaceable progress.

ROMCloud keeps Batocera's emulator-facing save tree local. SaveSync
reconciles eligible data with a canonical dataset at
`<remote_data.root>/saves/`. The writable data location is separate from
the read-only ROM source.

### Safety model

SaveSync is designed around a positive layout allowlist and transactional
replacement:

-   manual, periodic, and game-lifecycle triggers all enter the same verified
    SaveSync reconciliation path;
-   Offline does not poll or modify remote saves;
-   conflicts are reported instead of guessed or overwritten;
-   eligible saves from ROMCloud-managed and ordinary local games receive
    the same protection; catalog membership is not an eligibility gate;
-   replacements are staged and verified before promotion;
-   a previous known-good generation is retained for recovery;
-   unknown roots and unsupported nested content are not traversed, copied,
    or deleted;
-   per-group dirty/conflict evidence survives GUI sessions and reboots, and
    acknowledging a conflict does not resolve it.

SaveSync deliberately does **not** mean "synchronize everything under
`/userdata/saves`." Discovery starts only at audited layout roots. Ambiguous
emulator-wide data, generated/cache content, firmware/keys, and other
unsupported content are ignored unless a specific supported workflow says
otherwise.

The GUI provides SaveSync status, preview, upload, and download
workflows with confirmation before destructive synchronization. CLI
examples:

``` bash
romcloud saves status
romcloud saves reconcile
romcloud saves preview-upload
romcloud saves upload-all
romcloud saves preview-download
romcloud saves download-all
```

The SaveSync dashboard renders local/configured state immediately. Writable
`[remote_data]` availability is checked separately with a bounded background
probe, so Back and application Exit stay responsive when storage is missing.

When Auto SaveSync is enabled, ROMCloud's Batocera lifecycle hook records
game start and hands game stop to a detached worker. The worker hashes only
the audited layouts associated with that system/emulator, waits for concrete
save-file stability, and runs ordinary Quick Sync. While EmulationStation is
idle, the same Quick Sync path performs bounded periodic pulls and repairs a
missing local materialization. It does not use Download All as the normal
cross-device path.

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
qualification before they should be treated as fully validated. The
[popular-system coverage matrix and hardware plan](docs/savesync-coverage.md)
separate implemented behavior from those remaining device checks.

### Google Drive foundation

Google Drive is not a beta-supported provider and is omitted from the normal
Storage Wizard. The retained Phase 1 implementation includes the controller
device-authorization UX, `drive.file` scope, app-owned root discovery and
readiness checks, and Drive object primitives. SaveSync package/promotion work
has not started.

The feature is parked because Google's limited-input token exchange requires
the OAuth client secret. ROMCloud will not put that credential in Git, a public
URL, a release artifact, portable configuration, or a Batocera runtime file,
and the beta will not depend on a ROMCloud-operated authentication service.
Consequently, install, update, and repair do not retrieve or deploy Google OAuth
metadata. The former `runtime/google-oauth-client.url` production locator has
been removed.

Developer-only setup exposure can be enabled with
`ROMCLOUD_EXPERIMENTAL_GOOGLE_DRIVE=1`, but this does not provision credentials
or make Google Drive a supported provider. The provider/auth modules remain
importable for isolated development and tests. Any already-installed
experimental OAuth metadata and token state is left untouched.

User access and refresh tokens remain outside `romcloud.toml` under the
configured data directory in ROMCloud's versioned AES-256-GCM credential
envelope, with mode `0600` as an additional layer. Existing Phase 1 plaintext
token JSON is migrated atomically on first successful read; there is no
plaintext fallback. The remembered ID of ROMCloud's app-owned folder is not a
token.

**Original Xbox:** xemu stores progress inside the complete
`xbox_hdd.qcow2` virtual disk. Support is disabled by default because
synchronizing it means transferring the whole opaque file.

**Memory-card merging:** SaveSync automatically reconciles independent saves
inside structurally valid 128 KiB raw PS1 cards and marker-verified PCSX2
Folder Memory Cards. PS1 files are grouped conservatively by their documented
commercial game namespace. A PCSX2 folder with multiple structural entries is
kept opaque unless a complete versioned grouping supplied from trustworthy
PCSX2-maintained data is available. Invalid, unsupported, or ambiguous cards
retain opaque whole-card behavior, as do monolithic PCSX2 `.ps2` cards.
Batocera/DuckStation/PCSX2 hardware qualification remains pending.

**RPCS3 installed games:** installed titles, patches, firmware, caches,
configuration, and logs are never SaveSync content. Only the explicitly
registered RPCS3 save-data, trophy, virtual-memory-card, and savestate layouts
participate.

## Graphical interface

The Ports interface is the normal way to configure and maintain ROMCloud
on Batocera. It provides operating-mode selection, catalog
status/refresh, cache controls, storage configuration, SaveSync, Library
Sync, health checks, controller diagnostics/remapping, updates, and
maintenance operations.

The native interface and browser manager share a restrained ROMCloud
design language derived from the bundled icon and splash: midnight-navy
backgrounds, layered blue surfaces, electric-cyan focus, limited violet
accents, silver-white primary text, and consistent green/amber/red status
semantics. Controller focus uses a dedicated high-contrast cyan border and
is intentionally stronger than hover or selected state.

Typography uses the same deliberate UI stack on both surfaces: Inter,
Noto Sans, DejaVu Sans, Liberation Sans/Segoe UI, then Arial and the
platform sans-serif fallback. Headings use the same family at a stronger
weight while metadata and status text retain readable sizes and contrast.
ROMCloud does not currently redistribute a font binary; Pygame and the
browser resolve the first installed family so installation remains clean
across Batocera architectures.

Long-running backend operations are executed outside the graphical event
loop so the UI can continue processing controller, keyboard, and touch
input.

The GUI is installed when the Batocera environment provides a compatible
system Python/Pygame combination. If it cannot be installed, the
backend, CLI, cache, launcher, and storage functionality remain
available.

## Updates

ROMCloud includes a Git-free updater. Use **Maintenance → Check for
Updates** or:

``` bash
romcloud update --check
romcloud update
```

Ordinary updates stay on the machine's persisted channel. To switch an
existing installation (the selection is saved only after a successful
update), use:

``` bash
romcloud update --channel develop
romcloud update --channel stable
```

Only `stable` and `develop` are accepted. Stable currently resolves to the
`main` source line; develop resolves to `develop`. The resolver is centralized
so stable can move to published release artifacts later without changing the
commands or configuration format.

Updates reconcile ROMCloud-owned runtime files and integrations while
preserving configuration, credentials, catalog data, cache, logs,
proxies, and synchronization data. A successful GUI-initiated update can
relaunch the installed ROMCloud GUI; failed updates do not request a
relaunch.

## Repair, uninstall, and purge

  -----------------------------------------------------------------------
  Command                             Behavior
  ----------------------------------- -----------------------------------
  `romcloud repair`                   Downloads and reconciles ROMCloud-owned
                                      runtime artifacts from the configured
                                      channel without deleting user data

  `romcloud uninstall`                Removes runtime/integration
                                      components while preserving
                                      recoverable ROMCloud
                                      configuration/data/cache

  `romcloud purge`                    Removes ROMCloud-owned persistent
                                      local state as well as the
                                      installed runtime
  -----------------------------------------------------------------------

`purge` is intentionally much more destructive than `uninstall`. Neither
workflow should delete real source ROMs, ordinary local ROMs, unrelated
Batocera files, or user-controlled remote synchronization data. Safety
checks refuse broad or overlapping destructive targets.

## Network behavior

ROMCloud is designed so unavailable network storage does not make
Batocera itself unavailable. At boot, the SMB mount worker runs
independently with bounded retries rather than blocking the Batocera
startup sequence.

Expected behavior when the source is unavailable:

-   EmulationStation and ordinary local games continue to work;
-   complete cached ROMCloud games can launch in Cache/Offline
    workflows;
-   uncached games cannot be fetched;
-   catalog refresh fails without treating temporary source absence as
    permission to erase the catalog;
-   SaveSync requires its configured writable data target to be
    reachable and writable.

If an SMB source becomes available after startup, reconnect from the
Storage screen or run `romcloud mount start`.

## CLI reference

The installer intentionally does not add ROMCloud to the global `PATH`.
Examples use `romcloud` for readability; the default executable is
`/userdata/system/romcloud/bin/romcloud`.

``` text
romcloud configure                  Guided configuration
romcloud status                     Catalog/cache summary
romcloud healthcheck                Source, cache, integration, and sync checks
romcloud refresh [--system NAME]    Refresh the catalog
romcloud library ...                Connected/Cache/Offline mode
romcloud cache ...                  Cache status/add/remove/pin/unpin
romcloud manager                    Browser Library/Cache Manager
romcloud library-sync ...           Metadata/media synchronization
romcloud saves ...                  SaveSync operations
romcloud mount ...                  SMB mount management
romcloud es ...                     EmulationStation integration
romcloud update [--check] [--channel stable|develop]
                                    Update ROMCloud on/switch to a channel
romcloud repair                     Repair from the configured channel
romcloud uninstall                  Remove runtime; preserve recoverable data
romcloud purge                      Remove ROMCloud-owned local state
```

Run `romcloud <command> --help` for command-specific options.

## Advanced configuration

The graphical setup flow normally writes the configuration. A
representative layout is:

``` toml
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

`source.provider = "local"` is also used for the current SMB
architecture because ROMCloud mounts the SMB source first and then
accesses that mounted filesystem through its local-filesystem provider.

## Important paths

``` text
/userdata/system/romcloud/
├── bin/                         CLI and Batocera wrappers
├── config/romcloud.toml         Main configuration
├── config/credentials.toml      Credentials
├── data/catalog.db              Catalog/cache/proxy ownership database
├── data/direct-links.json       Direct link ownership
├── data/library-view.json       Current operating mode
├── data/library/                Local Library Sync state
├── logs/                        ROMCloud diagnostics
├── ports-gfx/                   Installed graphical UI
└── venv/                        Isolated backend Python environment

/userdata/romcloud/
├── source/                      Default read-only SMB source mount
├── remote/                      Default read/write ROMCloud data mount
└── cache/                       Managed local game cache
```

## Troubleshooting

Start with:

``` bash
/userdata/system/romcloud/bin/romcloud healthcheck
/userdata/system/romcloud/bin/romcloud status
/userdata/system/romcloud/bin/romcloud mount status
```

### ROMCloud is missing from Ports

-   Refresh EmulationStation's game lists or restart EmulationStation.
-   Confirm `/userdata/roms/ports/ROMCloud.sh` exists.
-   Run `romcloud repair` to reconcile a damaged Ports entry or wrapper.
-   If the GUI could not be installed because Pygame is unavailable, the
    CLI remains usable.

### The NAS is unavailable after boot

-   Check ROMCloud's mount-worker status and logs.
-   Verify the server, share, optional subdirectory, and credentials.
-   Run `romcloud mount start` after connectivity is available.
-   Complete cached games do not require the ROM source merely to read
    their cached assets.

### A transfer was interrupted

Launch the game again. ROMCloud retains useful staging data and can
reuse already completed assets. Incomplete files are not treated as
valid games.

### New games are missing

Run **Refresh Catalog**, then refresh EmulationStation's game list if
necessary. A source game disappearing is not automatically treated as
permission to delete its catalog record.

### An update completed but ROMCloud did not reopen

Reopen **Ports → ROMCloud** manually and inspect
`/userdata/system/romcloud/logs/gui-relaunch.log`.

## Known beta limitations

-   ROMCloud is beta software; keep independent backups of important
    saves and configuration.
-   Hardware validation is not exhaustive across every Batocera release,
    architecture, controller, display environment, or bundled
    Python/Pygame combination.
-   SFTP and a native/userspace SMB provider are not implemented.
-   The graphical interface depends on a compatible Batocera system
    Python/Pygame environment.
-   Catalog refresh is explicit after setup rather than continuously
    watching the source.
-   Source games that disappear are retained conservatively rather than
    automatically deleted.
-   Offline play requires complete, intact local cache assets and normal
    local emulator dependencies.
-   SaveSync remains beta. Its Batocera lifecycle/periodic reconciliation is
    implemented, but each emulator layout still needs representative hardware
    qualification; data it cannot safely classify or attribute remains
    excluded.
-   Library Sync remains opt-in beta functionality.
-   MS-DOS library/cache behavior is not yet beta-supported or audited.
-   Original Xbox SaveSync requires transferring xemu's complete virtual
    disk and is disabled by default.
-   Some multi-file/playlist formats still require additional validation
    or support.
-   EmulationStation presentation changes may require a game-list
    refresh while mode-transition behavior continues to be stabilized.

## Safety and design principles

**Batocera stays in control.** ROMCloud integrates with EmulationStation
and `emulatorlauncher`; it does not replace them.

**The ROM source is read-only.** Normal ROM access must not modify the
source library.

**Local games stay local.** ROMCloud tracks and removes only artifacts
it owns.

**Offline means offline.** Selecting Offline Mode must not quietly
depend on remote storage.

**Cache state is not catalog identity.** Evicting a game should not make
ROMCloud forget what the game is.

**Synchronization must fail safely.** Ambiguous save ownership,
conflicts, incomplete transfers, and unavailable storage should result
in a clear failure rather than guessed destructive behavior.

**ROMCloud may fail; Batocera must not.** A broken mount, unavailable
NAS, failed update, or ROMCloud error should not prevent ordinary
Batocera use.

## Development

ROMCloud targets Python 3.10 or newer.

``` bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Automated tests use temporary filesystem layouts and mocked
process/network boundaries. Real Batocera hardware remains necessary for
final validation of platform-specific behavior.

## Project status

ROMCloud is under active development and preparing for wider public beta
use. Bug reports and hardware validation are especially valuable around
clean installs and upgrades, large catalogs, operating-mode transitions,
unavailable network storage, cache recovery/eviction, Library Sync,
SaveSync, and different Batocera releases and hardware platforms.

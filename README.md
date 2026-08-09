# ROMCloud

**Browse your remote ROM library in Batocera like it is installed locally.**

ROMCloud lets Batocera users keep large ROM collections on a NAS, network share, external drive, or other storage while still browsing and launching games normally through EmulationStation.

Instead of copying every game to the device ahead of time, ROMCloud creates tiny `.romcloud` proxy files in Batocera's normal ROM folders. When you launch one, ROMCloud fetches the real ROM into a local cache and then hands the cached file to Batocera's normal `emulatorlauncher`.

The result:

- Your full remote library appears in EmulationStation.
- Games download only when you actually launch them.
- Cached games launch immediately on later runs.
- Existing local ROMs continue to work normally.
- Batocera remains the frontend — ROMCloud is the storage layer underneath it.

> ROMCloud is currently under active development. The core launch/cache pipeline is working on real Batocera 42 hardware, but setup and UI are still being polished.

---

## How it works

```text
Remote ROM library
        │
        │  SMB / mounted filesystem / USB / local storage
        ▼
ROMCloud catalog
        │
        │  generates lightweight proxy files
        ▼
/userdata/roms/<system>/*.romcloud
        │
        │  shown normally in EmulationStation
        ▼
romcloud-run
        │
        ├── cache hit ───────────────► launch immediately
        │
        └── cache miss
                │
                ▼
        /userdata/romcloud-cache
                │
                ▼
        Batocera emulatorlauncher
                │
                ▼
              Game
```

ROMCloud does **not** replace EmulationStation and does **not** replace Batocera's emulator configuration.

It intercepts only the ROM path during launch. Once a cloud game is cached, Batocera receives the real local file path and launches the game normally.

---

## Current status

The following has been tested successfully on a Steam Deck running **Batocera 42**:

- EmulationStation discovery of `.romcloud` proxy files
- Multiple Batocera systems from one remote library
- First-launch transfer from a NAS
- Local cache creation
- Instant repeat launches from cache
- Normal local-ROM passthrough
- SMB/CIFS source mounting
- Tailscale connectivity to a remote NAS
- Persistent EmulationStation override generation
- Cache status and management
- Health checks
- Git-free self-updating with `romcloud update`

Systems tested/generated in the current hardware setup include:

- Dreamcast
- GameCube
- PlayStation
- PlayStation 2
- PSP
- Saturn
- Wii
- Xbox
- Xbox 360

ROMCloud does not require every system to exist in the remote library. Only systems discovered in the catalog are managed.

---

## Release note (v0.7.0)

- New capability: a fullscreen graphical progress screen now appears
  during real Batocera cache-miss game launches (previously only a
  curses/terminal progress screen existed, which could never actually
  render when EmulationStation launches a game — Batocera's launch path
  has no controlling terminal for curses to attach to). The new screen
  runs under Batocera's system Python via a small pygame-based subprocess,
  `romcloud-launch-progress`, driven by the backend over a narrow
  stdin/stdout event protocol.
- Cache hits are unaffected: they continue to launch immediately with no
  ROMCloud screen at all.
- A missing/never-installed graphical component (e.g. no system Python
  with `pygame`) falls back automatically to the existing terminal
  progress bar (interactive sessions) or a silent transfer — a broken or
  absent graphical piece never blocks a real game launch.
- `romcloud update` and a fresh `scripts/install.sh` both install/refresh
  this new artifact automatically (shared reconciliation logic) — no
  manual re-run of `install.sh` is required after updating.
- No change to cache/transfer semantics, `.partial` file handling,
  resumability, LRU, catalog/proxy resolution, or the `emulatorlauncher`
  argv contract (still only the `-rom` value is ever replaced).
  `ports_gfx` still never imports `romcloud`.

---

## Release note (v0.6.0)

- Improvement: the graphical Ports UI and the related `romcloud uidata`
  health/status endpoints now report the configured user-facing source
  type (`SMB` vs `Local filesystem`) instead of exposing only the
  internal provider implementation. For SMB configurations the UI now
  also carries safe metadata (`server`, `share`, and mount point) without
  any credentials.
- The direct `romcloud status` / `romcloud healthcheck` commands now use
  the same source label, so the CLI and graphical UI present the same
  user-facing terminology.
- No provider semantics changed: SMB-backed sources still use the
  mounted local filesystem path and `LocalFilesystemProvider` internally.

---

## Release note (v0.5.0)

- New capability: the graphical Ports UI is now an actionable maintenance
  app instead of a mostly-informational menu. `Refresh Catalog` and the
  new `Check for Updates` action now open a reusable **operation screen**
  that launches the backend subprocess without freezing the UI, streams
  its live stdout/stderr output on screen (auto-scrolling, with a bounded
  history so memory can't grow unbounded), and clearly shows
  starting/running/succeeded/failed state before returning to the
  dashboard — see "Interactive maintenance & the operation screen" below.
- `Refresh Catalog` no longer relies on a fixed subprocess timeout (it had
  recently been raised from 20s to 120s as a stopgap): the operation
  screen owns the process lifecycle directly, so a legitimately long
  refresh is never reported as failed just for taking a while, while a
  truly failed refresh still reports failure via its real exit code.
- `Health Check`'s dashboard result now distinguishes a soft warning (the
  configured source isn't currently reachable) from an outright failure,
  instead of coloring both the same way.
- The operation screen is intentionally generic (title + subprocess argv)
  so later maintenance actions (`romcloud update`, repair, diagnostics,
  mount/reconnect, future sync operations) can reuse it directly.
- No backend functionality was invented for this: every action the UI now
  drives (`refresh`, `update --check`, `healthcheck`) is an existing CLI
  command already safe to run directly. No change to catalog refresh
  semantics, cache/download/launch behavior, or the subprocess/JSON
  `uidata` boundary; `ports_gfx` still never imports `romcloud`.

---

## Release note (v0.4.0)

- New capability: the graphical Ports UI is now usable end-to-end with a
  controller, a touchscreen, or a physical keyboard — normal use no longer
  requires a mouse. See "Controller, touch, and on-screen keyboard input"
  above for the full details.
- Added a unified semantic input layer (`ports_gfx.actions`/
  `input_manager`) so every screen/widget reacts to high-level actions
  (`UP`/`DOWN`/`LEFT`/`RIGHT`/`CONFIRM`/`BACK`/`MENU`/text-entry actions)
  instead of raw pygame key/button constants.
- Added SDL logical game-controller support with GUID-based identity,
  hot-plug/disconnect handling, analog-stick deadzone + held-input repeat
  timing, a raw-button fallback for unrecognized pads, and a new
  Controller Test/diagnostics screen with basic per-action remapping.
- Added a ROMCloud-owned on-screen keyboard foundation (not yet wired into
  a live screen — reusable infrastructure for the upcoming setup wizard).
- No backend/subprocess-JSON boundary changes; `ports_gfx` still never
  imports `romcloud`.

---

## Release note (v0.3.3)

- Bugfix: `romcloud update` now reconciles the *entire* installed
  application, not only the backend package. Previously, updating left the
  graphical Ports UI, wrappers, and any previously-enabled Batocera mount
  service/EmulationStation override files stale — a UI or integration
  change required manually downloading a source archive and re-running
  `scripts/install.sh`. `romcloud update` alone is now sufficient.
- The artifact-writing logic (`romcloud`/`romcloud-run` wrappers, the
  graphical Ports UI payload, the Batocera mount service script, the
  EmulationStation override) is now implemented once, in
  `romcloud.infrastructure.installer`, and shared by both
  `scripts/install.sh` and `romcloud update` — a fresh install and a later
  self-update always produce byte-identical artifacts from the same source
  revision.
- See "Self-updater" above for the full reconciliation/failure-semantics
  details.

---

## Release note (v0.2.1)

- Bugfix: `curses` is now imported lazily. Systems without the `curses`
  module (minimal Batocera images) continue to run the CLI; the TUI will
  fall back to a plain-text progress display or print a clear message.
- Guided SMB discovery/setup (implemented in `SMBDiscoveryService`) was
  added in v0.2.0; it validates server/credentials, enumerates accessible
  shares, detects system folders, and only persists configuration after
  confirmation.

- Versioning: ROMCloud's semantic version is sourced from the installed
  package metadata (the same source used by `romcloud --version` and
  `importlib.metadata`). The updater records build metadata (e.g. commit SHA)
  in `version.json` separately; avoid duplicating hardcoded version strings.

- UI status: a full graphical cache-miss progress/management UI is NOT
  implemented yet. The current terminal/TUI progress is a convenience
  fallback and is not intended to represent the final graphical UX. A
  graphical **Ports** menu is now available — see "Graphical Ports UI"
  below; the cache-miss transfer screen itself is still curses/plain-text
  only.

---

## Graphical Ports UI (v0.3.0)

ROMCloud now ships an optional graphical menu (Catalog Status, Refresh
Catalog, Health Check, Cache Status, Check for Updates) launched as a
Batocera **Port** (`/userdata/roms/ports/ROMCloud.sh`), instead of only the
controller-friendly curses menu.

**Why it runs outside ROMCloud's venv:** Batocera 42 ships pygame/SDL in its
own system Python (confirmed on real hardware: pygame 2.5.2 / SDL 2.32.8),
but ROMCloud's isolated venv deliberately never installs pygame and never
enables `--system-site-packages` (that would leak Batocera's entire system
site-packages into ROMCloud's dependency tree). Instead:

```text
Batocera system Python (pygame/SDL)          ROMCloud's isolated venv
        │                                            │
        ▼                                            ▼
   ports_gfx/  ── subprocess ──►  romcloud uidata <action>  ── JSON ──► catalog / cache / config
   (no romcloud imports)                (hidden CLI command group)
```

- `ports_gfx/` is a small, self-contained, pure-Python + pygame source tree
  that **never imports anything from the `romcloud` package**. It is copied
  (not pip-installed) by `scripts/install.sh` to
  `${ROMCLOUD_HOME}/ports-gfx/ports_gfx` and run directly with the detected
  system Python — completely independent of the venv.
- The only way `ports_gfx` reaches ROMCloud's backend is by shelling out to
  `romcloud uidata <action>` (a hidden CLI command group) and parsing a
  single JSON object from its stdout. This is a deliberate, enforced process
  boundary — see `ports_gfx/client.py` and
  `src/romcloud/cli/commands/uidata.py`.
- The graphical dashboard now shows the configured source type (`SMB` or
  `Local filesystem`) and safe SMB metadata instead of the internal
  provider implementation name.
- Install is entirely best-effort: if no system Python with `pygame`
  importable is found, the graphical Ports UI is simply not installed and
  the rest of ROMCloud (CLI, curses TUI, mount service, etc.) is completely
  unaffected.

**Assumption requiring real hardware validation:** the installer looks for
`/usr/bin/python3` first, then falls back to whatever `python3` resolves to
on `PATH`, and verifies `import pygame` succeeds under it. This has not yet
been re-confirmed against a fresh Batocera 42 image by this exact detection
logic (only the underlying pygame/SDL versions were confirmed present).

---

## Controller, touch, and on-screen keyboard input (v0.4.0)

The graphical Ports UI is now fully usable without a mouse or physical
keyboard:

- **Controller** — D-pad and the left analog stick both navigate; `A`/
  `Enter` confirms, `B`/`Esc` goes back, `Start` opens/returns to the menu.
  Controllers are recognized through SDL's own logical "game controller"
  mapping database (Xbox, PlayStation, Switch-style, and most generic
  SDL-recognized pads work automatically) rather than a hardcoded raw
  button layout; an unrecognized pad falls back to a small default raw
  mapping, itself overridable per-controller. Controller identity for
  saved mappings is the SDL GUID (falling back to the device name) — never
  the transient joystick index, which is reused across hot-plug events.
  Connecting/disconnecting a controller while ROMCloud is running never
  crashes the UI.
- **Touch** — every menu card is directly tappable; a tap both focuses and
  activates it in one step, no D-pad emulation required. Hit-testing always
  uses the actual rendered widget positions for the current resolution.
- **Physical keyboard** — arrow keys/WASD navigate, Enter/Space confirms,
  Escape goes back, unchanged from earlier releases.
- **Controller Test** — a new menu entry showing the detected controller's
  name, GUID, live button/stick activity, and a basic per-action remap
  flow (select an action, press a button on the controller to bind it);
  custom mappings are stored per-controller under
  `${ROMCLOUD_HOME}/ports-gfx-state/`, a location a reinstall/update never
  wipes (unlike `ports-gfx/`, which is recopied wholesale on every
  install/update).
- **On-screen keyboard foundation** — a ROMCloud-owned OSK (letters,
  numbers, symbols, space, backspace, shift/caps, confirm, cancel, and a
  masked-password show/hide toggle) usable by controller or touch, with
  physical keyboard text entry still working at the same time. Not yet
  wired into a live screen — it is reusable infrastructure for the
  upcoming graphical setup wizard, not a user-facing feature on its own
  yet.

All input handling lives entirely inside `ports_gfx` — nothing was added
to ROMCloud's backend (`romcloud uidata` contract, subprocess/JSON
boundary, and the venv/system-Python split are all unchanged).

### Temporary hardware diagnostics

For one-off controller troubleshooting on real Batocera hardware, the
Ports UI can write a raw pygame event capture to
`/userdata/system/romcloud/logs/controller-debug.log`.

- Enable it by setting `ROMCLOUD_PORTS_GFX_INPUT_DEBUG=1` in the Batocera
  Ports launcher environment before starting ROMCloud. The generated
  launcher you would edit is `/userdata/system/romcloud/bin/romcloud-ports`
  (or the Batocera Ports entry script it calls at
  `/userdata/roms/ports/ROMCloud.sh`).
- The log is **fresh per launch**: each run truncates the previous file so
  one test session is easy to inspect.
- It records only JOY*/CONTROLLER* input events plus startup controller
  identity details (name, GUID, device index/instance id, button/axis/hat
  fields where present). It does not log text input, backend data, or any
  ROMCloud secrets.
- Remove the environment variable to disable the capture again.

---

## Interactive maintenance & the operation screen (v0.5.0)

The graphical Ports dashboard is now actionable, not just informational:

- **Refresh Catalog** and **Check for Updates** open a reusable
  **operation screen** instead of blocking the dashboard on a single
  subprocess call. **Health Check** and **Cache Status** remain quick,
  synchronous dashboard results (they already return promptly).
- The operation screen (`ports_gfx/operation.py` +
  `ports_gfx/operation_screen.py`) launches the backend as a real
  subprocess via two background reader threads (stdout/stderr), and the
  pygame event loop polls it once per frame — the UI keeps rendering and
  accepting input the entire time the backend is working, however long
  that legitimately takes. No timeout is ever applied to the subprocess
  itself; only an actual non-zero exit (or a failed launch) is reported as
  failure.
- Output is shown live, auto-scrolls to the latest line, wraps to the
  screen width, and is capped to a bounded number of retained lines so a
  very chatty or very long operation can never grow memory unbounded.
  Once an operation finishes, the user can scroll back through its output
  before returning to the dashboard (`A`/`Enter`/`Esc`/tap, or `B`) — the
  dashboard's status message then reflects the just-finished operation's
  outcome.
- `Refresh Catalog` is the first action migrated to this model, replacing
  its previous fixed subprocess timeout entirely: a legitimately long
  refresh over a large remote library is never reported as failed simply
  because it runs past some arbitrary number of seconds, while a genuinely
  failed refresh still reports failure (via its real process exit code).
- The operation screen is deliberately generic (a title plus the backend
  argv to run) rather than a large task/job framework — a later action
  (`romcloud update`, repair, diagnostics, mount/reconnect, future sync
  operations) reuses it by adding one entry, not by changing the screen.
  This is also the presentation/state pattern the upcoming graphical
  cache-miss download screen is expected to build on.
- Fully controller/touch/keyboard accessible, at every supported
  resolution (Steam Deck, 720p, 1080p, 4K, unusual aspect ratios) — same
  responsive layout/input foundation as the rest of the Ports UI.
- No backend/subprocess-JSON boundary changes: every action the operation
  screen runs is an existing `romcloud` CLI command (`refresh`,
  `update --check`); `ports_gfx` still never imports `romcloud`.

---

## Support ROMCloud

ROMCloud is free and open source, but developing and testing it has real
costs. If ROMCloud is useful to you and you'd like to support continued
development, testing, and maintenance, optional donations are appreciated.

Donations are optional — ROMCloud's core functionality remains free and
open source. (PayPal/support link placeholder)

---

## Features

### Transparent EmulationStation integration

ROMCloud generates `.romcloud` proxy files inside Batocera's normal system directories:

```text
/userdata/roms/psx/Alundra (USA).romcloud
/userdata/roms/ps2/Some Game.romcloud
/userdata/roms/xbox/Another Game.romcloud
```

EmulationStation continues to be the normal game browser.

ROMCloud owns a persistent Batocera override file:

```text
/userdata/system/configs/emulationstation/es_systems_romcloud.cfg
```

It does **not** modify Batocera's stock:

```text
/usr/share/emulationstation/es_systems.cfg
```

ROMCloud reads Batocera's current system definitions, preserves the existing extension list and launcher arguments, appends `.romcloud`, and routes launches through `romcloud-run`.

---

### Local cache

Downloaded ROMs are stored outside EmulationStation's scanned ROM directories:

```text
/userdata/romcloud-cache
```

The cache mirrors the Batocera system namespace:

```text
/userdata/romcloud-cache/psx/Alundra (USA).chd
/userdata/romcloud-cache/ps2/Some Game.iso
```

ROMCloud preserves the original filename so Batocera's per-game configuration continues to work correctly.

Current cache behavior includes:

- Cache hits
- Cache misses
- Resumable `.partial` transfers
- LRU-style eviction
- Pinning
- Configurable cache size
- Minimum free-space reserve
- Safe removal of individual cached games

ROMCloud never intentionally evicts:

- pinned games
- games currently transferring
- games currently launching

---

### SMB / NAS support

The current SMB implementation uses the operating system's CIFS mount support.

Example:

```text
//omnivault/Roms
        ↓
/userdata/romcloud-source
        ↓
LocalFilesystemProvider
```

#### Guided setup (default)

`romcloud configure` no longer asks you to type a share name blindly. Instead
it discovers what you can actually access, and only lets you pick from
shares it has already proven it can reach:

```text
Server: omnivault
Username: stryph
Password: ********

Connecting...
Connected.

Select an SMB share:
  Roms
  Media
  Downloads
> Roms

Validating //omnivault/Roms...
Connected to //omnivault/Roms

Detected systems:

  ✓ dreamcast
  ✓ gamecube
  ✓ ps2
  ✓ psp
  ✓ psx
  ✓ saturn
  ✓ wii
  ✓ xbox
  ✓ xbox360

9 systems detected.

Use this library? [Y/n]
```

Core rule: *if ROMCloud lets you select a share, it has already proven it can
access it.* Nothing is written to `romcloud.toml` or the credentials file
until this entire flow succeeds and you confirm — if you cancel at any
point, or a step fails, your existing configuration and credentials are left
completely unchanged.

If share enumeration is unavailable (e.g. the `smbclient` tool isn't
installed), ROMCloud automatically falls back to manual share entry — typed
share names still go through the exact same validation and system-detection
pipeline before anything is saved.

Password entry shows `*` per character on terminals that support it, and
falls back to fully hidden input (never plaintext) otherwise.

The reusable discovery/setup logic lives in
`romcloud.core.services.smb_discovery.SMBDiscoveryService` — it is not tied
to the CLI, so a future graphical setup UI can use it unchanged.

Example configuration:

```toml
[source]
provider = "local"
rom_root = "/userdata/romcloud-source"

[smb]
server = "omnivault"
share = "Roms"
username = "your-user"
port = 445
```

The SMB password is stored separately from `romcloud.toml`, atomically, with
mode 0600.

ROMCloud can install and manage a Batocera mount service:

```bash
romcloud mount install
romcloud mount start
romcloud mount stop
romcloud mount status
```

**Mount service details**

- Canonical service path: `/userdata/system/services/romcloud_mount`.
- The service `start` routes to `romcloud mount boot-start`.
- `boot-start` launches a detached background worker that performs SMB
  waiting and mounting; the service itself does not block Batocera's boot.
- Principle: "ROMCloud may fail; Batocera must not." The installer and
  boot service avoid interfering with Batocera's critical startup path.

**Installer `custom.sh` behavior**

- ROMCloud does not modify, source, or depend on `/userdata/system/custom.sh`.
- This is intentional to avoid affecting unrelated user or system startup logic.

A native direct-SMB provider is planned for the future. The current architecture intentionally keeps mounting separate from the storage-provider layer so direct SMB can be added later without rewriting catalog/cache logic.

**Assumption requiring real hardware validation:** share discovery/validation
shells out to the `smbclient` CLI (Samba client tools), the same family as
the `cifs-utils` package already relied on for mounting. This has not yet
been confirmed as present on a stock Batocera 42 image — if absent, ROMCloud
falls back to manual share entry automatically.

---

### Tailscale

ROMCloud does not require Tailscale, but Tailscale works well for accessing a NAS outside the local network.

Tailscale is treated as connectivity, not as a ROM storage provider.

For example:

```text
Batocera device
      │
      │ Tailscale
      ▼
Remote NAS
      │
      │ SMB
      ▼
ROMCloud
```

ROMCloud does not manage or modify your Tailscale configuration.

---

### Self-updater

Batocera does not include `git`, so ROMCloud includes a git-free updater.

Check for an update:

```bash
romcloud update --check
```

Install the latest build:

```bash
romcloud update
```

The updater:

- resolves the latest GitHub commit
- downloads an archive pinned to that exact commit
- safely extracts it
- upgrades the existing persistent Python environment
- reconciles every ROMCloud-managed runtime artifact against that same
  archive — the `romcloud`/`romcloud-run` wrappers, the graphical Ports UI
  (`ports_gfx`, `romcloud-ports`, the Batocera Port entry script), and, only
  if already enabled, the Batocera mount service script and ROMCloud's
  EmulationStation override
- records the installed build (version + exact commit) in `version.json`

This means `romcloud update` keeps the *entire* installed application
current, not only the backend package — after a UI/wrapper/integration
change ships, running `romcloud update` is enough; there is no need to
manually download a source archive to `/tmp` or re-run `scripts/install.sh`.
The `romcloud`/`romcloud-run` wrappers are required: if reconciling them
fails, the whole update is reported as failed and the previous install
stays authoritative. Everything else (the graphical Ports UI, the mount
service, the EmulationStation override) is reconciled best-effort — a
missing/incompatible system Python or an unconfigured integration is a
normal state, not a failure ("ROMCloud may fail; Batocera must not").

The updater does not replace your ROMCloud home directory wholesale.

It preserves:

- configuration
- credentials
- catalog database
- cache
- logs
- proxies

> Upgrading from a version older than this reconciliation behavior may
> require running `romcloud update` twice: the first run upgrades the
> backend package (using that older version's own updater logic), and the
> second run — now using the upgraded updater — performs the full artifact
> reconciliation described above.

---

## Installation

ROMCloud installation is still being streamlined.

The current Batocera installation uses a persistent Python virtual environment under:

```text
/userdata/system/romcloud/venv
```

ROMCloud itself lives under:

```text
/userdata/system/romcloud
```

Typical layout:

```text
/userdata/system/romcloud/
├── bin/
├── config/
├── data/
├── logs/
├── venv/
└── version.json
```

Default paths:

```text
ROMCloud home:
  /userdata/system/romcloud

Configuration:
  /userdata/system/romcloud/config/romcloud.toml

Catalog database:
  /userdata/system/romcloud/data/catalog.db

Logs:
  /userdata/system/romcloud/logs

Cache:
  /userdata/romcloud-cache

Local Batocera ROMs:
  /userdata/roms

Default mounted source:
  /userdata/romcloud-source
```

---

## Configuration

Run the interactive configuration wizard:

```bash
romcloud configure
```

Current source types:

- Local / USB
- SMB network share

For SMB, the current implementation mounts the share locally and then uses the local filesystem provider internally.

This distinction is intentional:

```text
SMB source
   ↓
CIFS mount
   ↓
local filesystem path
   ↓
ROMCloud catalog/cache/launch services
```

---

## Refresh the library

After configuration:

```bash
romcloud healthcheck
```

Then scan the source and generate proxies:

```bash
romcloud refresh
```

ROMCloud expects the remote library to use Batocera system folder names:

```text
Roms/
├── psx/
├── ps2/
├── psp/
├── dreamcast/
├── gamecube/
├── wii/
├── xbox/
└── xbox360/
```

System directories are optional.

If only `psx`, `ps2`, and `dreamcast` exist, only those systems are managed.

Existing local ROMs are left alone.

---

## EmulationStation integration

Install or regenerate ROMCloud's EmulationStation override:

```bash
romcloud es install
```

Refresh it after catalog/system changes:

```bash
romcloud es refresh
```

Check status:

```bash
romcloud es status
```

Remove only ROMCloud's own override:

```bash
romcloud es remove
```

ROMCloud does not overwrite unrelated user override files.

---

## Cache management

View cache status:

```bash
romcloud cache status
```

ROMCloud distinguishes between:

- **Cached** — available locally and eligible for automatic eviction
- **Pinned** — available locally and protected from automatic eviction

Cache limits are configurable.

The cache is intended to behave more like a working set than a permanent duplicate of the remote library.

---

## Health check

Run:

```bash
romcloud healthcheck
```

The health check verifies things such as:

- source reachability
- Batocera ROM directory availability
- cache writability
- minimum free disk space
- ROMCloud data directory writability

The long-term goal is for failures to be reported in user-friendly language rather than exposing raw Linux errors.

---

## CLI

Current top-level commands include:

```text
romcloud cache
romcloud configure
romcloud es
romcloud healthcheck
romcloud launch
romcloud mount
romcloud refresh
romcloud saves
romcloud status
romcloud update
```

Run:

```bash
romcloud --help
```

for the current command list.

---

## Design principles

### Batocera stays in control

ROMCloud should not become another emulator frontend.

Batocera remains responsible for:

- EmulationStation
- emulator selection
- controllers
- per-game configuration
- metadata
- scraping
- emulator launch behavior

ROMCloud is responsible for making remotely stored ROMs available locally when needed.

### Local games stay local

ROMCloud does not move or cache ordinary local ROMs.

If a normal game file is launched through the ROMCloud wrapper, it is passed through to Batocera unchanged.

### ROMCloud may fail; Batocera must not

This is a core project rule.

A disconnected NAS, bad SMB password, unavailable Tailscale connection, full cache, or broken ROMCloud configuration should make cloud games unavailable — it should **not** prevent Batocera from booting or break unrelated local games.

Boot/startup isolation is an active area of hardening.

### ROMCloud owns only ROMCloud files

Generated configuration and services should be deterministic, identifiable, and removable without damaging unrelated user configuration.

---

## Known limitations

ROMCloud is still early software.

Current limitations include:

### Graphical launch progress

When launching an uncached game from EmulationStation, a fullscreen ROMCloud
progress screen now appears while the game transfers, showing:

- game title and system
- progress bar
- transferred / total size
- current phase (connecting / downloading / launching)
- cancel support

It runs under Batocera's system Python via a small pygame-based subprocess
(`romcloud-launch-progress`, installed alongside the graphical Ports UI),
driven by the backend over a narrow stdin/stdout event protocol — the same
subprocess boundary the Ports UI already uses, just in the other direction.
If that subprocess isn't available (e.g. no system Python with `pygame`
was found at install time), ROMCloud falls back to a terminal progress bar
when run from an interactive session, or a silent transfer otherwise — a
missing graphical component never blocks a game launch.

Cached games continue to launch immediately with no ROMCloud screen at all.

### SMB is currently mount-based

The current implementation requires CIFS mounting.

A future native `SMBProvider` should remove the need for users to understand or configure mount points.

### Multi-disc / multi-asset games — BIN/CUE

BIN/CUE disc images are now fully supported: ROMCloud parses the `FILE` lines
inside a `.cue` sheet, treats every referenced track as a required companion
asset of that one logical game, and caches all of them (`.cue` + every
`.bin`/`.wav`/etc track) before launch. Only the `.cue` is ever registered as
a playable catalog entry / EmulationStation proxy — its referenced tracks are
never catalogued as separate, independently-launchable games.

- Both flat layouts (`psx/Game.cue` + `psx/Game (Track 1).bin` side by side)
  and directory-scoped layouts (`psx/Game A/Game A.cue` + `psx/Game A/Track
  01.bin`) are supported. A directory-scoped set is cached into the cache
  root preserving its exact subdirectory structure — companion assets are
  never flattened into the system root, and two different games may safely
  contain identically-named tracks in their own directories without
  colliding (asset identity is the full relative path, never the bare
  filename).
- A cache hit only counts once the `.cue` **and every required track** are
  present and size-valid; a `.cue` with one missing/corrupt track is treated
  as incomplete, and only the missing tracks are re-fetched on repair (the
  rest is left untouched).
- The graphical (and terminal) transfer progress screen aggregates bytes and
  percentage across the whole logical game, not per track — one launch opens
  one progress session, regardless of how many files it contains.
- Existing catalogs migrate automatically: on the next `romcloud refresh`,
  any `.bin` track that used to be catalogued independently and is now
  referenced by a `.cue` has its now-stale proxy/catalog entry removed
  (never the real ROM file on disk). Migration does not even require a
  `romcloud refresh` first — launching a game catalogued before cue
  companion tracking existed re-derives its required assets from the
  current `.cue` at launch time too, so a legacy single-file cache is
  correctly treated as incomplete and repaired (only the missing companion
  tracks are fetched) the first time it's launched after upgrading.
- A cue-referenced file that's missing from the source is still catalogued
  (so `romcloud refresh`'s warnings and `romcloud healthcheck` reflect it
  clearly) but caching/launching that game fails safely — ROMCloud never
  hands emulatorlauncher a `.cue` with a missing track.

**Not yet implemented:** `.m3u` multi-disc playlists and CCD/IMG/SUB-style
descriptor+companion groups. The underlying data model (one launch asset +
zero or more required companion assets) is generic enough to support them
without another architectural change, but the format-specific dependency
parsing for those has not been written yet.

### Save sync

Save synchronization is planned but not complete.

The intended design keeps save synchronization separate from ROM caching and avoids silently overwriting conflicting saves.

---

## Planned UI

ROMCloud is currently CLI-first, but the long-term goal is a controller-friendly Batocera management UI.

Planned screens include:

### Home

```text
Source       Connected
Cache        18.4 GB / 50 GB
Games        5,069
Integration  Enabled
```

### Sources

- Local / USB sources
- SMB network shares
- connection test
- credential setup
- reachability status

### Cache

- cache usage
- cache limit
- free-space reserve
- cached games
- pin / unpin
- remove cached game
- clear unpinned cache

### Library

System-level summary without replacing EmulationStation:

```text
PlayStation      623 games
PlayStation 2    412 games
Dreamcast        184 games
```

### Health

Human-readable status such as:

```text
✓ Network available
✓ NAS reachable
✓ SMB authenticated
✓ ROM source mounted
✓ EmulationStation integration active
✓ Cache writable
```

---

## Project direction

ROMCloud's goal is simple:

> Keep the giant ROM library somewhere else, but make using it feel local.

The ideal end-user flow is eventually:

```text
Install ROMCloud
→ choose ROM source
→ enter network credentials if needed
→ choose cache size
→ enable integration
→ refresh library
→ launch games normally from EmulationStation
```

No manual XML editing.

No shell mount commands.

No separate ROM browser.

No copying an entire multi-terabyte library to every Batocera device.

---

## Development status

ROMCloud is being actively developed and tested against real Batocera hardware.

The project is currently focused on:

1. boot and installer safety
2. launch-time graphical transfer progress
3. streamlined setup
4. clean-machine testing
5. management UI
6. direct SMB support
7. additional multi-disc formats (`.m3u`, CCD/IMG/SUB) — BIN/CUE is supported today
8. save synchronization

Contributions, testing, hardware reports, and ideas are welcome.

---

## License

License information will be added here once the project license is finalized.

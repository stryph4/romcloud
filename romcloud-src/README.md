# ROMCloud

Browse and launch ROMs stored on remote or external sources from Batocera — without keeping the full library locally.

ROMCloud creates lightweight `.romcloud` proxy files in your normal Batocera ROM directories. When you select a game in EmulationStation, ROMCloud transfers it from the remote source into a local cache and hands the cached path to Batocera's normal launcher. Games that are already cached launch immediately.

---

## How it works

```
EmulationStation
  → selects Final Fantasy X.romcloud
  → invokes romcloud launch <proxy>
  → ROMCloud checks cache
  → if not cached: show progress, transfer from source
  → hand cached ROM path to batocera-run
  → emulator launches normally
```

ROMCloud feels like infrastructure. You browse in EmulationStation as normal.

---

## Requirements

- Batocera (tested on v40+, Python 3.10+)
- ROMs organized in Batocera system folder convention at the remote source:

```
/your/rom/root/
├── nes/
├── snes/
├── ps2/
└── ...
```

---

## Installation

```bash
# Clone or extract ROMCloud
cd /userdata/system/romcloud

# Run the installer (idempotent — safe to re-run)
bash scripts/install.sh
```

The installer:
- Creates `/userdata/system/romcloud/{app,bin,config,data,logs}/`
- Creates `/userdata/romcloud-cache/` as default cache
- Installs the `romcloud` CLI to `/userdata/system/romcloud/bin/`
- Does **not** overwrite existing configuration

---

## Quick start

```bash
# Interactive setup wizard
romcloud configure

# Scan your ROM source and generate proxy files
romcloud refresh

# Check everything looks healthy
romcloud healthcheck

# See what is cached
romcloud cache status
```

---

## CLI reference

| Command | Description |
|---|---|
| `romcloud configure` | Interactive configuration wizard |
| `romcloud refresh` | Scan source, create/update proxy files |
| `romcloud status` | Show catalog and cache summary |
| `romcloud healthcheck` | Verify source reachability, cache space, etc. |
| `romcloud launch <proxy>` | Resolve proxy, cache if needed, launch via Batocera |
| `romcloud cache status` | List cached games and sizes |
| `romcloud cache add <game_id>` | Pre-cache a game |
| `romcloud cache remove <game_id>` | Remove cached copy |
| `romcloud cache pin <game_id>` | Pin game (never auto-evicted) |
| `romcloud cache unpin <game_id>` | Unpin game |
| `romcloud saves sync` | Sync save data to/from remote |
| `romcloud saves status` | Show save sync state |
| `romcloud update` | Update ROMCloud to latest version |

---

## Configuration

Located at `/userdata/system/romcloud/config/romcloud.toml`:

```toml
[source]
provider = "local"           # "local" or "smb"
rom_root = "/mnt/nas/ROMs"

[cache]
path = "/userdata/romcloud-cache"
max_size_gb = 50.0
min_free_gb = 5.0

[local_roms]
path = "/userdata/roms"

[logging]
level = "INFO"
```

SMB credentials are stored separately in `config/credentials.toml` (mode 600):

```toml
[smb]
password = "your-password"
```

---

## Cache behavior

| State | Meaning |
|---|---|
| **Cached** | File is local; may be evicted by policy |
| **Pinned** | File is local; never auto-evicted |

- Eviction uses least-recently-played order based on ROMCloud timestamps
- Eviction never removes pinned games, games in-transfer, or currently launching games
- A pinned game can be explicitly removed with `cache remove`
- Interrupted transfers are resumed from where they left off

---

## Architecture

```
src/romcloud/
├── cli/           — Click commands; no business logic
├── ui/            — Progress UI and maintenance TUI
├── core/
│   ├── models/    — Domain: Game, CacheEntry, ProxyRecord
│   ├── services/  — CatalogService, CacheService, TransferService
│   ├── providers/ — StorageProvider ABC, LocalFilesystemProvider, SMBProvider
│   └── exceptions.py
├── infrastructure/
│   ├── database.py
│   ├── repositories/
│   ├── config.py
│   ├── logging.py
│   └── credentials.py
├── integrations/
│   └── batocera/  — SPIKE: ES config, launch wrapper
└── bootstrap/     — Dependency wiring (Container)
```

The core has zero dependency on Batocera. Batocera-specific behavior lives behind the `integrations/` layer.

---

## What ROMCloud does NOT do

- Distribute ROMs
- Scrape metadata
- Act as a game frontend
- Preconfigure access to any ROM library
- Modify or delete files it did not create

---

## License

MIT

# Changelog

ROMCloud is currently beta software. This changelog focuses on user-visible
changes that are candidates for the next stable promotion. Earlier beta history
remains available in Git history.

## Unreleased — next stable

These changes are currently on `develop` and are not yet part of the stable
channel.

### Highlights

- Added a durable Download Manager for browser-initiated downloads, including a
  persistent queue, pause/resume, retry, cancellation, restart recovery, and
  retained partial progress where it is safe to reuse.
- Added Troubleshoot and Quick Repair flows for diagnosing ROMCloud and applying
  only explicitly whitelisted fixes. Diagnostics are available through the UI
  and CLI, with structured output available for support/debugging.
- Hardened ROMCloud lifecycle handling with clearer `repair`, `uninstall`, and
  `purge` boundaries. Uninstall preserves recoverable ROMCloud data, while purge
  removes ROMCloud-owned persistent state without intentionally deleting original
  ROMs, emulator saves, external keys, or remote-provider data.
- Improved SFTP performance and lifecycle behavior with bounded session reuse
  during catalog scans and logical game transfers, while keeping SFTP read-only,
  host-key pinned, and free of persistent background connections.
- Fixed SMB sources whose Batocera system folders live below the share root,
  including RetroNAS-style layouts such as `batocera/ROMS`.
- Strengthened Library Sync provider handling so read-only providers can still
  read existing metadata/media while write-dependent publishing remains gated by
  durable transaction capabilities.

### Downloads and cache

- Added persistent download records and staging metadata so interrupted work can
  be recovered more safely after restart.
- Added cross-process cache asset locking and storage reservations to coordinate
  browser downloads, game launches, cache promotion, and eviction decisions.
- Improved staging cleanup and recovery so verified partial work can be retained
  when possible instead of always restarting from zero.
- Improved cache accounting around in-progress transfers and promotion/recovery
  states.
- Coordinated explicit Offline mode with the Download Manager so source-backed
  work reaches a safe boundary before the mode transition completes.
- Reduced unnecessary browser download polling/DOM work while keeping active
  transfer status responsive.

### SMB and setup

- Fixed SMB subdirectory source mounting when the selected Batocera library is
  inside a share rather than at the share root. ROMCloud now mounts the selected
  share-relative directory directly instead of relying on `prefixpath` behavior.
- Added CLI directory selection inside SMB shares so `romcloud configure` can
  choose the folder that directly contains Batocera system directories, matching
  the graphical setup flow.
- Tightened SMB mount identity and health checks to verify the expected share,
  access mode, and subdirectory view, while retaining compatibility with legacy
  mounts that report an exact matching `prefixpath`.
- Hardened SMB rollback and unmount paths so ROMCloud verifies mount ownership
  before detaching a configured location.

### SFTP

- Reworked SFTP around an explicitly read-only provider policy.
- Reuse one bounded SFTP session for each catalog system scan, including a
  per-scan directory/stat cache, then close the connection when the scan ends.
- Reuse one bounded SFTP session for a logical game transfer instead of opening a
  fresh SSH/SFTP connection for each individual operation.
- Keep SSH host-key verification fail-closed and continue to require the pinned
  fingerprint established during setup.
- Keep SFTP mount-free: ROMCloud continues to access it directly at the protocol
  level rather than presenting it as a local filesystem.

### Library Sync

- Improved provider-neutral Library Sync handling and remote capability checks.
- Allow existing remote Library Sync metadata/media to remain readable from
  read-only providers while rejecting publish operations that cannot meet
  ROMCloud's durable write requirements.
- Improved pull/import progress and reporting in the graphical interface.
- Hardened canonical metadata/media handling and atomic local writes.

### Maintenance and recovery

- Added controller-safe graphical Uninstall/Purge flows with hold-to-confirm
  protection for destructive actions.
- Hardened lifecycle ownership checks so ROMCloud cleanup targets verified
  ROMCloud-managed files, links, proxies, runtime state, and integration artifacts
  rather than unrelated Batocera content.
- Improved graphical lifecycle handoff so uninstall/purge work can continue only
  after the ROMCloud UI exits cleanly.
- Improved installer, updater, repair, and reconciliation behavior around missing
  or partially damaged runtime state.
- Expanded diagnostics for EmulationStation integration, runtime health, active
  operations, configuration, storage, and repairable presentation issues.

### Reliability and performance

- Strengthened atomic file replacement and verification for persistent ROMCloud
  state.
- Reduced repeated database/progress work in transfer paths and batched staging
  status reads used by the browser.
- Added additional ownership metadata and recovery bookkeeping for generated
  presentation/cache state.
- Improved mount, game-access, proxy, and Offline-library reconciliation behavior.

### Project and development

- Added GitHub Actions CI for pushes and pull requests targeting `main` and
  `develop`, using the minimum supported Python version and excluding explicitly
  hardware-marked tests.
- Added Python 3.10 `tomli` compatibility for TOML parsing.
- Added a root MIT license and a separate legal/branding notice.

### Notes

- SaveSync remains beta. Keep independent backups of important saves while
  testing cross-device synchronization and save-state support.
- SFTP remains a read-only ROMCloud provider. Write-dependent SaveSync and
  Library Sync publishing operations require storage with ROMCloud's supported
  durable write semantics.
- A final version number and release date will be assigned when this set of
  changes is promoted to the stable channel.

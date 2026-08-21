"""User-facing source labels for config-backed maintenance/status views.

The backend's internal provider model stays exactly as-is: SMB sources are
mounted locally and still use ``LocalFilesystemProvider`` over
``source.rom_root``. These helpers only derive a user-facing label and
safe diagnostic details from the already-loaded config, so the UI can show
"SMB" versus "Local filesystem" without exposing implementation details
as the primary label.
"""

from __future__ import annotations

from romcloud.infrastructure.config import AppConfig


def source_display_summary(config: AppConfig) -> dict[str, str]:
    """Return a user-facing source summary for dashboard/health output.

    Keys are intentionally presentation-oriented: the UI should display
    ``source_type`` first and only treat ``source_internal_provider`` as a
    diagnostic hint if it wants to show one at all.
    """
    if not config.source.enabled:
        return {
            "source_type": "None (local games only)",
            "source_mount_point": "",
            "source_internal_provider": "none",
            "source_description": "ROMCloud game management is disabled",
        }
    summary = {
        "source_type": "SMB" if config.smb is not None else "Local filesystem",
        "source_mount_point": config.source.rom_root,
        "source_internal_provider": config.source.provider,
    }
    if config.smb is not None:
        summary.update(
            {
                "source_server": config.smb.server,
                "source_share": config.smb.share,
                "source_description": (
                    f"{config.smb.server}:{config.smb.share}"
                    + (
                        f"/{config.smb.remote_path}"
                        if config.smb.remote_path
                        else ""
                    )
                ),
            }
        )
    else:
        summary["source_description"] = config.source.rom_root
    return summary

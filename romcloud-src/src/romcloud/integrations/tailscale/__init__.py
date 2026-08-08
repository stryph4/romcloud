"""Tailscale integration placeholder.

Tailscale is NOT a storage provider.  It is optional connectivity that may
make an SMB share or SFTP server reachable from outside the local network.

ROMCloud does not need to know whether Tailscale is in use.  If the SMB/SFTP
source is reachable at its configured address, everything works normally.

This module reserves the integration slot for future work such as:
- Detecting whether the Tailscale interface is up before a transfer.
- Reporting connectivity status in the health check.
"""

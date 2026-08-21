"""Strict update-channel definitions and source resolution.

Every application-side update consumer resolves a channel here.  The stable
mapping intentionally points at ``main`` for now; a future tagged-release
resolver can change :func:`resolve_channel` without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class UpdateChannel(str, Enum):
    STABLE = "stable"
    DEVELOP = "develop"


DEFAULT_UPDATE_CHANNEL = UpdateChannel.STABLE


@dataclass(frozen=True)
class ChannelSource:
    channel: UpdateChannel
    ref: str


_CHANNEL_REFS = {
    UpdateChannel.STABLE: "main",
    UpdateChannel.DEVELOP: "develop",
}


def parse_channel(value: object | None) -> UpdateChannel:
    """Return an allowlisted channel.

    Missing values are the sole fallback case and resolve to ``stable`` for
    compatibility with installs created before update channels existed.
    Present but unknown values always fail closed.
    """
    if value is None:
        return DEFAULT_UPDATE_CHANNEL
    if isinstance(value, UpdateChannel):
        return value
    if not isinstance(value, str):
        raise ValueError("update channel must be 'stable' or 'develop'")
    try:
        return UpdateChannel(value.strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"invalid update channel {value!r}; expected 'stable' or 'develop'"
        ) from exc


def resolve_channel(value: object | None = None) -> ChannelSource:
    channel = parse_channel(value)
    return ChannelSource(channel=channel, ref=_CHANNEL_REFS[channel])


def channel_label(value: object | None = None) -> str:
    return parse_channel(value).value.capitalize()

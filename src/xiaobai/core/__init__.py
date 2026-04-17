"""Core abstractions — Channel protocol, registry, canonical message model, auth."""

from .channel import Channel, Capabilities, OnMessageCallback
from .message import ChannelAddress, Message
from .registry import ChannelRegistry
from .media import MediaRegistry
from .auth import TokenProvider

__all__ = [
    "Channel",
    "Capabilities",
    "OnMessageCallback",
    "ChannelAddress",
    "Message",
    "ChannelRegistry",
    "MediaRegistry",
    "TokenProvider",
]

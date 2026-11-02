"""Every error the library raises of its own: event_connect's."""
from event_connect import (
    AuthenticationError,
    CannotConnectError,
    ClientError,
    InvalidValueError,
    ModelError,
    NotConnectedError,
    ReadOnlyError,
    UnsupportedDeviceError,
)

__all__ = [
    "AuthenticationError",
    "CannotConnectError",
    "ClientError",
    "InvalidValueError",
    "ModelError",
    "NotConnectedError",
    "ReadOnlyError",
    "UnsupportedDeviceError",
]

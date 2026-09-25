"""The errors the library raises about a device or its own state, rather than about a value."""


class ClientError(Exception):
    """Base for the errors the client raises about its own state."""


class CannotConnectError(ClientError):
    """The device could not be reached, or did not answer every request during the scan."""


class UnsupportedDeviceError(ClientError):
    """The device answered, but no model matches it."""


class NotConnectedError(ClientError):
    """The operation needs a device that has been scanned."""


class ReadOnlyError(ClientError):
    """A write was asked of a read-only client."""


class AuthenticationError(ClientError):
    """The device refused the credentials it was given."""

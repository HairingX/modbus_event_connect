"""Every error the library raises of its own."""


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


class InvalidValueError(ValueError):
    """A value a point cannot take, or registers that are not a value of it."""


class ModelError(ValueError):
    """A model does not resolve cleanly against an identity; lists every problem found."""

"""Declarative device models over Modbus and micro_nabto, read and written through one client."""
from importlib.metadata import version as _installed_version

from ._client import Client, PointsCallback
from ._clock import Clock
from ._data_type import ByteOrder, DataType, DataTypeKind, WordOrder
from ._device import Identity
from ._errors import (
    AuthenticationError,
    CannotConnectError,
    ClientError,
    InvalidValueError,
    ModelError,
    NotConnectedError,
    ReadOnlyError,
    UnsupportedDeviceError,
)
from ._events import ValueCallback
from ._key import Key
from ._model import InstanceScanStep, Model, ModelSelector, RepeatedSection, Scan, ScanStep, Section
from ._point import (
    Change,
    Labels,
    Limits,
    Point,
    PollRate,
    Pulse,
    Refresh,
    Selector,
    Transform,
    Transforms,
    WriteKind,
)
from ._resilience import (
    NO_RETRY,
    AnyOf,
    Attempts,
    CircuitBreaker,
    Exponential,
    Fixed,
    Jitter,
    Retry,
    Steps,
    Stop,
    Wait,
    Within,
)
from ._unit import Unit
from ._value import DataValue, Quality
from event_connect import Status, StatusCallback, Write

__version__ = _installed_version("modbus_event_connect")
__all__ = [
    "AnyOf",
    "Attempts",
    "AuthenticationError",
    "ByteOrder",
    "CannotConnectError",
    "Change",
    "CircuitBreaker",
    "Client",
    "ClientError",
    "Clock",
    "DataType",
    "DataTypeKind",
    "DataValue",
    "Exponential",
    "Fixed",
    "Identity",
    "InstanceScanStep",
    "InvalidValueError",
    "Jitter",
    "Key",
    "Labels",
    "Limits",
    "Model",
    "ModelError",
    "ModelSelector",
    "NO_RETRY",
    "NotConnectedError",
    "Point",
    "PointsCallback",
    "PollRate",
    "Pulse",
    "Quality",
    "ReadOnlyError",
    "Refresh",
    "RepeatedSection",
    "Retry",
    "Scan",
    "ScanStep",
    "Section",
    "Selector",
    "Status",
    "StatusCallback",
    "Steps",
    "Stop",
    "Transform",
    "Transforms",
    "Unit",
    "UnsupportedDeviceError",
    "ValueCallback",
    "Wait",
    "Within",
    "WordOrder",
    "Write",
    "WriteKind",
]

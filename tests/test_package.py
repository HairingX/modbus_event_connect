"""The package as it is installed: what a type checker and a user of it see."""
import importlib.metadata
import importlib.resources
import re

import modbus_event_connect


def test_it_ships_its_types() -> None:
    """PEP 561: without py.typed, a type checker treats every import of the package as untyped."""
    assert importlib.resources.files(modbus_event_connect).joinpath("py.typed").is_file()


def test_its_version_is_the_one_it_was_installed_as() -> None:
    assert modbus_event_connect.__version__ == importlib.metadata.version("modbus_event_connect")


def test_every_link_in_its_description_works_where_the_description_is_shown() -> None:
    """PyPI shows the README without the repository's files, so a relative link there is dead."""
    description = importlib.metadata.metadata("modbus_event_connect").json["description"]
    assert isinstance(description, str) and description
    links = re.findall(r"\]\(([^)]+)\)", description)
    assert links and all(link.startswith(("https://", "#")) for link in links), links


SHARED = ("AuthenticationError", "CannotConnectError", "Certainty", "Change", "ClientError", "Clock", "DataValue",
          "DeviceClient", "Identity", "InvalidValueError", "Key", "Labels", "Limits", "ModelError", "ModelInfo",
          "NotConnectedError", "PointsCallback", "PollRate", "Quality", "ReadOnlyError", "Selector", "Status",
          "StatusCallback", "Unit", "UnsupportedDeviceError", "ValueCallback", "Write", "WriteKind", "WriteRange")
"""What every library of a device exports alike, so that each is used the same way."""


def test_what_event_connect_gives_is_the_same_object_here() -> None:
    import event_connect

    shared = (*SHARED, "Retry", "CircuitBreaker")
    assert all(getattr(modbus_event_connect, name) is getattr(event_connect, name) for name in shared)
    assert set(shared) <= set(modbus_event_connect.__all__)


def test_how_a_model_is_timed_is_its_own_and_no_part_of_the_package() -> None:
    assert not {"Timing", "Profile", "DEFAULT_TIMING", "DEFAULT_INTERVALS"} & set(dir(modbus_event_connect))


def test_no_module_of_its_own_only_passes_event_connect_on() -> None:
    import importlib.util

    shells = ("_clock", "_events", "_key", "_scheduler", "_unit", "_value", "_resilience", "_errors",
              "testing._clock")
    assert [name for name in shells if importlib.util.find_spec(f"modbus_event_connect.{name}")] == []

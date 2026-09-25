"""The package as it is installed: what a type checker and a user of it see."""
import importlib.metadata
import importlib.resources

import modbus_event_connect


def test_it_ships_its_types() -> None:
    """PEP 561: without py.typed, a type checker treats every import of the package as untyped."""
    assert importlib.resources.files(modbus_event_connect).joinpath("py.typed").is_file()


def test_its_version_is_the_one_it_was_installed_as() -> None:
    assert modbus_event_connect.__version__ == importlib.metadata.version("modbus_event_connect")

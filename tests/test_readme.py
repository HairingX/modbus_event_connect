"""The README's example of a model file, its package and its tests, run as the README gives them."""
import inspect
import re
import sys
from pathlib import Path

import pytest

README = Path(__file__).parent.parent / "README.md"


def _block(marker: str) -> str:
    """The README's Python example that contains `marker`."""
    found = [block for block in re.findall(r"```python\n(.*?)```", README.read_text(), re.S) if marker in block]
    assert len(found) == 1, marker
    return found[0]


async def test_the_model_file_its_package_and_its_tests_work_as_the_readme_gives_them(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "my_devices"
    package.mkdir()
    (package / "_thermostat.py").write_text(_block('"""my_devices/_thermostat.py'))
    (package / "__init__.py").write_text(_block('"""my_devices/__init__.py"""'))
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    tests: dict[str, object] = {}
    try:
        exec(_block("def test_models_are_valid"), tests)
        valid, read = tests["test_models_are_valid"], tests["test_temperature_is_read"]
        assert callable(valid) and callable(read)
        valid()
        reading = read()
        assert inspect.isawaitable(reading)
        await reading
    finally:
        for name in [name for name in sys.modules if name.split(".")[0] == "my_devices"]:
            del sys.modules[name]

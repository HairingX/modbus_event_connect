import logging

from src.modbus_event_connect.modbus_models import Read
_LOGGER = logging.getLogger(__name__)

def test_read_flags():
    # STARTUP should not match the same as ALWAYS
    search = Read.STARTUP
    assert bool(search & (Read.ALWAYS | Read.STARTUP))
    assert not bool(search & (Read.ALWAYS))
    assert bool(search & (Read.STARTUP))
    # ALWAYS should not match the same as STARTUP
    search = Read.ALWAYS
    assert bool(search & (Read.ALWAYS | Read.STARTUP))
    assert bool(search & (Read.ALWAYS))
    assert not bool(search & (Read.STARTUP))
    # STARTUP_ALWAYS should not match the same as STARTUP
    search = Read.STARTUP_ALWAYS
    assert bool(search & (Read.STARTUP_ALWAYS))
    assert bool(search & (Read.ALWAYS))
    assert bool(search & (Read.STARTUP))
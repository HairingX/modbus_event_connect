import logging
_LOGGER = logging.getLogger(__name__)

def test_test():
    print("hello print")
    _LOGGER.debug("hello _LOGGER")
    assert True
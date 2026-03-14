"""pytest configuration: enable asyncio mode for async tests."""
import pytest

# pytest-asyncio >= 0.21 requires explicit asyncio_mode configuration
pytest_plugins = ["pytest_asyncio"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "asyncio: mark a test as an asyncio coroutine test",
    )

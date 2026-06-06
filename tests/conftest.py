"""
conftest.py

Runs before any test file is imported. Sets the environment variables that
app.py reads at module level so pytest can import it without calling sys.exit.
Also provides fixtures shared across all test modules.
"""

import os
import threading
import time

import pytest

# Must be set before `import app` anywhere in the test suite.
os.environ.setdefault("DO_TOKEN", "test_token_do_not_use")
os.environ.setdefault("DROPLET_ID", "99999999")
os.environ.setdefault("DNS_FQDN", "toronto.jxue.ca")

import app  # noqa: E402


@pytest.fixture
def client():
    """Flask test client."""
    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_state():
    """
    Reset shared mutable state between every test.

    Without this, a test that triggers the cooldown would bleed into the
    next test, making execution order matter.
    """
    app._last_rotation = 0.0
    if app._lock.locked():
        app._lock.release()
    yield
    app._last_rotation = 0.0
    if app._lock.locked():
        app._lock.release()

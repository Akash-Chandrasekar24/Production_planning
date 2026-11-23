import os
import tempfile

# isolated database for the whole test session (set before app modules read settings): tests never touch the real database
_db = os.path.join(tempfile.mkdtemp(), "test.db").replace("\\", "/")
os.environ["DATABASE_URL"] = ""
os.environ["PLANNING_DB"] = _db
os.environ["READONLY_DATABASE_URL"] = "sqlite:///" + _db      # stands in for the agent_ro role (SQLite cannot enforce read-only)
os.environ["DOMO_TOKEN"] = ""
os.environ["DOMO_SUBDOMAIN"] = ""
for _k in ("NEON_API_KEY", "NEON_PROJECT_ID", "NEON_BRANCH_ID", "NEON_DATABASE_NAME", "DOMO_MODEL", "DOMO_FALLBACK_MODEL", "NVIDIA_API_KEY"):
    os.environ[_k] = ""      # tests never reach live services, whatever .env contains
os.environ["AUTH_MODE"] = "dev_headers"         # most tests act as a role through headers; test_login.py covers real sign-in
os.environ["ADMIN_PASSWORD"] = ""
os.environ["AGENT_ENABLED"] = "false"          # no background agent loop in tests; tests call the agent directly
os.environ["VOLUME_GATE"] = "false"            # the demand and capacity gate has its own test (test_volume.py); the others test planning itself
from cryptography.fernet import Fernet  # noqa: E402
os.environ["CONNECTOR_KEY"] = Fernet.generate_key().decode()       # throwaway key for this test session only


import pytest  # noqa: E402


class AllowGuard:
    """Stands in for the NemoGuard models in agent tests (their own tests are in test_nemoguard.py)."""
    from app.nemoguard import Verdict as _V

    def check_input(self, text):
        return self._V(True)

    def check_output(self, question, answer):
        return self._V(True)


@pytest.fixture(scope="session", autouse=True)
def _tables():
    from app import db
    db.init_db()                  # TestClient without a `with` block skips the app's startup, which normally does this


@pytest.fixture(autouse=True)
def _guard_and_limits(request, monkeypatch):
    from app import agent
    agent._hits.clear()
    if "real_guard" not in request.keywords:
        monkeypatch.setattr(agent, "get_guard", lambda: AllowGuard())
    yield


def pytest_configure(config):
    config.addinivalue_line("markers", "real_guard: use the real NemoGuard client (HTTP mocked in the test)")

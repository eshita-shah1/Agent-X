import pytest


@pytest.fixture(autouse=True)
def _clean_database(clean_db):
    """Every integration test starts from empty tables (the test database only; see conftest.settings)."""
    yield

import uuid

import pytest

from judge.settings import Settings
from sandbox.testing import running_sandbox


@pytest.fixture(scope="session")
def sandbox_url(tmp_path_factory):
    with running_sandbox(tmp_path_factory.mktemp("sandbox") / "sandbox.db") as url:
        yield url


@pytest.fixture
def settings(sandbox_url, tmp_path):
    return Settings.from_env(backend="sandbox", sandbox_url=sandbox_url, trial_id=None, var_dir=tmp_path)


@pytest.fixture
def uid():
    return uuid.uuid4().hex[:8]

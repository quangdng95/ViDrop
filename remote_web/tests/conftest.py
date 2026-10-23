import os

import pytest

from remote_web import config as config_module


@pytest.fixture
def isolated_state_file(tmp_path, monkeypatch):
    # config.py persists the trust token + signing key to a real file - point
    # every test at a throwaway path so tests never read/write the real
    # deployment's own .state.json.
    state_file = tmp_path / ".state.json"
    monkeypatch.setattr(config_module, "STATE_FILE", str(state_file))
    return state_file


@pytest.fixture(autouse=True)
def no_threads_page_fetch(monkeypatch):
    # backend.threads reads a post's public page first (network). A test must never reach threads.com, so by
    # default that page "does not show the post" and the login path (stubbed per test) runs, as it did before the
    # page path existed. Tests of the page reader itself replace the session again.
    from backend import threads as threads_module

    class NoNetwork:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            raise OSError("no network in tests")

    monkeypatch.setattr(threads_module.curl_requests, "Session", NoNetwork)

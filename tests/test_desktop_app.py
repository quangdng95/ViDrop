import desktop_app


def test_second_lock_attempt_fails_while_first_is_held(monkeypatch, tmp_path):
    # Regression test for the Dock-icon-pile-up bug: every extra launch must
    # fail to acquire the lock while a real instance already holds it, so
    # desktop_app.py's __main__ guard can exit before creating any window.
    monkeypatch.setattr(desktop_app, "_LOCK_PATH", str(tmp_path / "vidrop.lock"))
    monkeypatch.setattr(desktop_app.paths, "APP_SUPPORT_DIR", str(tmp_path))

    assert desktop_app._acquire_singleton_lock() is True
    first_lock_file = desktop_app._lock_file
    assert first_lock_file is not None

    assert desktop_app._acquire_singleton_lock() is False
    # A failed attempt must not clobber the first instance's held lock, and
    # must clean up its own (unlocked) file handle rather than leaking it.
    assert desktop_app._lock_file is None

    first_lock_file.close()


def test_lock_is_released_after_the_holder_closes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_app, "_LOCK_PATH", str(tmp_path / "vidrop.lock"))
    monkeypatch.setattr(desktop_app.paths, "APP_SUPPORT_DIR", str(tmp_path))

    assert desktop_app._acquire_singleton_lock() is True
    desktop_app._lock_file.close()
    desktop_app._lock_file = None

    # Once the holder is gone (the real app quit), a new launch must succeed.
    assert desktop_app._acquire_singleton_lock() is True
    desktop_app._lock_file.close()

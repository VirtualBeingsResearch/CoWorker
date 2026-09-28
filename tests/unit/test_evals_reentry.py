from coworker.api import app as api_app


def test_clear_shutdown_allows_another_main() -> None:
    api_app._shutting_down = True
    api_app.clear_shutdown()
    assert not api_app._shutting_down

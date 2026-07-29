from coworker.relay.policy import relay_route_allowed


def test_relay_route_policy_only_exposes_desktop_and_published_updates():
    allowed = {
        ("GET", "/status"),
        ("GET", "/api/communicate/register"),
        ("POST", "/api/communicate/register"),
        ("DELETE", "/api/communicate/register/registration"),
        ("POST", "/messages"),
        ("GET", "/sse/participant"),
        ("GET", "/api/desktop-updates/darwin/aarch64/1.0.0"),
        ("GET", "/api/desktop-updates/assets/1.1.0/app.tar.gz"),
    }
    for method, path in allowed:
        assert relay_route_allowed(method, path), (method, path)
    for method, path in {
        ("GET", "/api/admin/config"),
        ("POST", "/api/desktop-updates/releases/1.0.0/publish"),
        ("GET", "/logs"),
        ("GET", "/api/desktop-updates/feed/v1/releases"),
        ("GET", "/sse/participant/extra"),
        ("GET", "/api/desktop-updates/assets/../secret"),
        ("CONNECT", "/anything"),
    }:
        assert not relay_route_allowed(method, path), (method, path)

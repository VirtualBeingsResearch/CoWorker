from coworker.relay.policy import relay_route_allowed


def test_relay_route_policy_exposes_desktop_updates_and_openai_v1():
    allowed = {
        ("GET", "/status"),
        ("GET", "/api/communicate/register"),
        ("POST", "/api/communicate/register"),
        ("DELETE", "/api/communicate/register/registration"),
        ("POST", "/messages"),
        ("GET", "/sse/participant"),
        ("GET", "/api/desktop-updates/darwin/aarch64/1.0.0"),
        ("GET", "/api/desktop-updates/assets/1.1.0/app.tar.gz"),
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
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
        ("POST", "/v1/models"),
        ("GET", "/v1/chat/completions"),
        ("DELETE", "/v1/models"),
    }:
        assert not relay_route_allowed(method, path), (method, path)

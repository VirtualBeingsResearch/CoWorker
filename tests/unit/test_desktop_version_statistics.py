from coworker.core.types import CommunicateRegistration
from coworker.desktop_updates import build_desktop_version_statistics


def _registration(
    participant_id: str,
    desktop_id: str,
    version: str | None,
    registered_at: str,
    *,
    kind: str = "coworker-desktop",
) -> CommunicateRegistration:
    metadata = {"desktop_id": desktop_id}
    if version is not None:
        metadata["desktop_version"] = version
    return CommunicateRegistration(
        registration_id=f"registration-{participant_id}",
        participant_id=participant_id,
        kind=kind,
        client_id=f"{desktop_id}:local:coworker",
        display_name=desktop_id,
        created_at=registered_at,
        last_registered_at=registered_at,
        metadata=metadata,
    )


def test_statistics_deduplicate_desktop_actors_and_prefer_active_version():
    registrations = [
        _registration("old-local", "desk-a", "0.1.0", "2026-01-01T00:00:00"),
        _registration("current-local", "desk-a", "0.2.0", "2026-02-01T00:00:00"),
        _registration("current-codex", "desk-a", "0.2.0", "2026-02-01T00:00:00"),
        _registration("desk-b", "desk-b", "0.1.0", "2026-02-01T00:00:00"),
        _registration(
            "web-client",
            "not-a-desktop",
            "9.0.0",
            "2026-02-01T00:00:00",
            kind="web",
        ),
    ]

    statistics = build_desktop_version_statistics(
        registrations,
        {"current-local", "current-codex"},
        "0.2.0",
    )

    assert statistics == {
        "latest_version": "0.2.0",
        "total_desktops": 2,
        "active_desktops": 1,
        "outdated_desktops": 1,
        "unknown_version_desktops": 0,
        "versions": [
            {
                "version": "0.2.0",
                "desktops": 1,
                "active_desktops": 1,
                "outdated": False,
            },
            {
                "version": "0.1.0",
                "desktops": 1,
                "active_desktops": 0,
                "outdated": True,
            },
        ],
    }


def test_statistics_report_unknown_and_invalid_versions_without_guessing():
    registrations = [
        _registration("unknown", "desk-unknown", None, "2026-01-01T00:00:00"),
        _registration("invalid", "desk-invalid", "nightly", "2026-01-01T00:00:00"),
    ]

    statistics = build_desktop_version_statistics(registrations, set(), "1.0.0")

    assert statistics == {
        "latest_version": "1.0.0",
        "total_desktops": 2,
        "active_desktops": 0,
        "outdated_desktops": 0,
        "unknown_version_desktops": 1,
        "versions": [
            {
                "version": "nightly",
                "desktops": 1,
                "active_desktops": 0,
                "outdated": None,
            },
            {
                "version": None,
                "desktops": 1,
                "active_desktops": 0,
                "outdated": None,
            },
        ],
    }


def test_statistics_support_empty_registration_store():
    assert build_desktop_version_statistics([], set(), None) == {
        "latest_version": None,
        "total_desktops": 0,
        "active_desktops": 0,
        "outdated_desktops": 0,
        "unknown_version_desktops": 0,
        "versions": [],
    }

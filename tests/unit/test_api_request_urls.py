from __future__ import annotations

import unittest

from coworker.api.request_urls import desktop_update_asset_base_url


class DesktopUpdateAssetBaseURLTests(unittest.TestCase):
    def test_authenticated_relay_uses_its_public_instance_base(self) -> None:
        self.assertEqual(
            desktop_update_asset_base_url(
                "http://coworker-relay-e2ee:0/",
                {
                    "coworker_relay": {
                        "authenticated_tunnel": True,
                        "public_base_url": "https://relay.example/i/cw_abcdefgh/",
                    }
                },
            ),
            "https://relay.example/i/cw_abcdefgh",
        )

    def test_untrusted_relay_state_cannot_override_the_request_base(self) -> None:
        self.assertEqual(
            desktop_update_asset_base_url(
                "https://updates.example/",
                {
                    "coworker_relay": {
                        "authenticated_tunnel": False,
                        "public_base_url": "https://attacker.example/i/cw_forged",
                    }
                },
            ),
            "https://updates.example",
        )


if __name__ == "__main__":
    unittest.main()

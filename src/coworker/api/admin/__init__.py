"""Authenticated administration HTTP API."""

from coworker.api.admin import router as router_module

admin_router = router_module.router
setup_admin = router_module.setup_admin
setup_channel_admin = router_module.setup_channel_admin

__all__ = ["admin_router", "setup_admin", "setup_channel_admin"]

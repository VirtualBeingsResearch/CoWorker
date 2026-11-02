"""Exercise the committed Web bundle against real admin routes with fake providers."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.async_api import Route, async_playwright, expect

from coworker.api.admin import router_module as admin
from coworker.brain.base import BaseLLMProvider
from coworker.core.model_config import RuntimePreferenceState
from tests.unit.test_admin_api import _client
from tests.unit.test_model_routing import make_brain

WEB_ROOT = Path(__file__).resolve().parents[2] / "src/coworker/web"


@pytest.mark.parametrize("language", ["zh", "en"])
async def test_pending_admin_can_repair_policy_and_confirm_preference(tmp_path, monkeypatch, language):
    runtime_path = tmp_path / "runtime.json"
    test_client, _ = _client(tmp_path, llm={"runtime_config_file": str(runtime_path)})
    brain, _, _, _ = make_brain()
    for provider in brain.router.providers.values():
        BaseLLMProvider.__init__(provider, provider.provider_name)
    brain.require_preference_confirmation(
        RuntimePreferenceState(status="pending", source="unresolved"), persistence_path=runtime_path,
    )
    monkeypatch.setattr(admin, "_brain", brain)
    calls: list[tuple[str, str, int]] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_client.app), base_url="http://coworker.test",
    ) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context()
            await context.add_init_script(
                "localStorage.setItem('coworker-admin-token', 'secret');"
                f"localStorage.setItem('coworker-language', {json.dumps(language)});"
            )

            async def serve(route: Route) -> None:
                request = route.request
                url = urlsplit(request.url)
                if url.netloc != "coworker.test":
                    await route.abort()
                elif url.path == "/api/admin/overview":
                    await route.fulfill(json={"status": {"is_running": False}})
                elif url.path.startswith("/api/"):
                    response = await api.request(
                        request.method, url.path, content=request.post_data,
                        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                    )
                    calls.append((request.method, url.path, response.status_code))
                    await route.fulfill(
                        status=response.status_code, body=response.content,
                        content_type=response.headers.get("content-type", "application/json"),
                    )
                else:
                    asset = WEB_ROOT / (url.path.lstrip("/") if url.path.startswith("/assets/") else "index.html")
                    await route.fulfill(
                        body=asset.read_bytes(),
                        content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream",
                    )

            await context.route("**/*", serve)
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            # An explicit settings link stays usable to repair provider connections.
            await page.goto("http://coworker.test/admin?section=settings")
            await expect(page.get_by_role("heading", name="运行设置" if language == "zh" else "Runtime settings", exact=True)).to_be_visible()
            assert page.url.endswith("section=settings")
            # The default entry leads to confirmation, never the clean-start wizard.
            await page.goto("http://coworker.test/admin")
            await expect(page.get_by_text("请确认主线首选模型" if language == "zh" else "Confirm the preferred main model", exact=True)).to_be_visible()
            assert page.url.endswith("section=models")
            await expect(page.locator(".admin-bootstrap")).to_have_count(0)

            initial = page.get_by_role("spinbutton", name="初始冷却（秒）" if language == "zh" else "Initial cooldown (seconds)", exact=True)
            maximum = page.get_by_role("spinbutton", name="最大冷却（秒）" if language == "zh" else "Maximum cooldown (seconds)", exact=True)
            save = page.get_by_role("button", name="保存并热更新" if language == "zh" else "Save & hot apply", exact=True)
            await initial.fill("1200")
            await expect(page.get_by_role("alert")).to_have_text("最大冷却不得小于初始冷却。" if language == "zh" else "Maximum cooldown must be greater than or equal to initial cooldown.")
            await expect(save).to_be_disabled()
            assert not any(method == "PATCH" for method, _, _ in calls)
            await maximum.fill("1800")
            await expect(save).to_be_enabled()
            await save.click()
            await expect(save).to_be_disabled()
            saved = json.loads(runtime_path.read_text(encoding="utf-8"))
            assert saved["routing"]["cooldown_seconds"] == 1200
            assert saved["routing"]["max_cooldown_seconds"] == 1800
            assert saved["preferred"] is None and brain.preference_pending

            await page.get_by_role("combobox", name="选择 Provider" if language == "zh" else "Select provider", exact=True).select_option("primary")
            await page.locator("#switch-model-input").fill("primary-model")
            await page.get_by_role("button", name="切换模型" if language == "zh" else "Switch model", exact=True).click()
            await expect(page.get_by_text("首选已保存，正在重启恢复模型服务。" if language == "zh" else "Preference saved. Restarting to restore model services.", exact=True)).to_be_visible()
            saved = json.loads(runtime_path.read_text(encoding="utf-8"))
            assert saved["preferred"] == {"provider": "primary", "model": "primary-model"}
            assert saved["preference"]["source"] == "explicit"
            assert not brain.preference_pending
            assert ("POST", "/api/admin/bootstrap", 202) not in calls
            assert ("POST", "/api/admin/model/switch", 200) in calls
            assert all(status < 400 for _, _, status in calls)
            assert errors == []
        finally:
            await browser.close()

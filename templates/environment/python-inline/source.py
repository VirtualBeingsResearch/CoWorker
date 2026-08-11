"""Environment source template (inline Python mode).

Copy this directory to .coworker/environment/<your-name>/ and customize.

The framework calls ``poll(ctx)`` on each trigger.  ``ctx`` provides:
- ctx.config     — params dict from SOURCE.md frontmatter
- ctx.http       — shared httpx.AsyncClient
- ctx.logger     — loguru logger bound to this source
- ctx.emit_signal(title, content, fingerprint, url?, severity?) — emit a signal
- ctx.get_cursor() / ctx.set_cursor() — incremental cursor
- ctx.is_known(fingerprint) — dedup check
"""

from __future__ import annotations


async def poll(ctx):
    url = ctx.config.get("url")
    if not url:
        ctx.logger.warning("no url configured")
        return

    # Example: fetch data and emit signals for new items.
    resp = await ctx.http.get(url)
    if resp.status_code != 200:
        ctx.logger.warning(f"request failed: {resp.status_code}")
        return

    data = resp.json()
    for item in data.get("items", []):
        item_id = str(item["id"])
        fingerprint = f"item:{item_id}"

        # emit_signal returns False if this fingerprint was already pushed
        # (dedup).  The framework tracks this automatically.
        ctx.emit_signal(
            title=item.get("title", f"Item {item_id}"),
            content=item.get("body", ""),
            fingerprint=fingerprint,
            url=item.get("url"),
            severity="info",
        )

    # Save cursor for incremental polling (e.g. last-seen timestamp or ETag).
    cursor = resp.headers.get("etag")
    if cursor:
        ctx.set_cursor(cursor)

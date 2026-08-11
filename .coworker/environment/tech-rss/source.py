"""RSS/Atom feed environment source (inline mode).

Polls an RSS or Atom feed for new entries, emitting a signal for each unseen
item.  Uses ``ctx.http`` and persists seen entry IDs as the cursor.

Override ``params.url`` in SOURCE.md frontmatter to track your feed.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET


async def poll(ctx):
    url = ctx.config.get("url", "")
    if not url:
        ctx.logger.warning("tech-rss: no url configured")
        return

    max_items = int(ctx.config.get("max_items", 5))

    resp = await ctx.http.get(url)
    if resp.status_code != 200:
        ctx.logger.warning(f"tech-rss: feed returned {resp.status_code}")
        return

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        ctx.logger.warning(f"tech-rss: failed to parse feed: {exc}")
        return

    # Determine feed format: RSS 2.0 (<rss><channel><item>) or Atom (<feed><entry>).
    entries = _extract_entries(root)
    if not entries:
        return

    # Emit newest-first, capped at max_items.
    count = 0
    for entry in entries:
        if count >= max_items:
            break

        fingerprint = entry["id"]
        if ctx.is_known(fingerprint):
            continue

        ctx.emit_signal(
            title=entry["title"],
            content=entry.get("summary", "")[:2000],
            fingerprint=fingerprint,
            url=entry.get("link"),
            severity="info",
        )
        count += 1


def _extract_entries(root: ET.Element) -> list[dict[str, str]]:
    """Parse RSS 2.0 and Atom feeds into a uniform entry list."""
    tag = root.tag.lower()

    # Atom: <feed xmlns=...><entry>...</entry></feed>
    if "feed" in tag:
        entries: list[dict[str, str]] = []
        for entry_elem in root.iter():
            if not entry_elem.tag.lower().endswith("entry"):
                continue
            entry = _parse_atom_entry(entry_elem)
            if entry:
                entries.append(entry)
        return entries

    # RSS 2.0: <rss><channel><item>...</item></channel></rss>
    items: list[dict[str, str]] = []
    for item_elem in root.iter():
        if not item_elem.tag.lower().endswith("item"):
            continue
        entry = _parse_rss_item(item_elem)
        if entry:
            items.append(entry)
    return items


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _parse_rss_item(elem: ET.Element) -> dict[str, str] | None:
    entry: dict[str, str] = {}
    for child in elem:
        name = _strip_ns(child.tag).lower()
        text = (child.text or "").strip()
        if name == "title":
            entry["title"] = text
        elif name == "link":
            entry["link"] = text
        elif name == "description":
            entry["summary"] = text
        elif name == "guid":
            entry["id"] = text
    if "id" not in entry:
        entry["id"] = entry.get("link") or entry.get("title") or ""
    if not entry.get("title"):
        return None
    return entry


def _parse_atom_entry(elem: ET.Element) -> dict[str, str] | None:
    entry: dict[str, str] = {}
    for child in elem:
        name = _strip_ns(child.tag).lower()
        text = (child.text or "").strip()
        if name == "title":
            entry["title"] = text
        elif name == "id":
            entry["id"] = text
        elif name == "summary" or name == "content":
            if "summary" not in entry:
                entry["summary"] = text
        elif name == "link":
            href = child.attrib.get("href", "")
            if href:
                entry["link"] = href
    if "id" not in entry:
        entry["id"] = entry.get("link") or entry.get("title") or ""
    if not entry.get("title"):
        return None
    return entry

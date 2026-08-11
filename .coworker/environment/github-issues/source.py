"""GitHub issues/comments environment source (inline mode).

Polls a GitHub repository for new issues and comments, emitting a signal for
each unseen item.  Uses ``ctx.http`` (shared httpx client) and persists the
last-seen timestamp as the cursor.

Override ``params.repository`` in SOURCE.md frontmatter to track your repo.
"""

from __future__ import annotations

import json


async def poll(ctx):
    repo = ctx.config.get("repository", "")
    if not repo:
        ctx.logger.warning("github-issues: no repository configured")
        return

    state = ctx.config.get("state", "open")
    include_comments = ctx.config.get("include_comments", True)
    per_page = int(ctx.config.get("per_page", 10))
    token = ctx.config.get("token", "")

    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Use the cursor (last-seen updated_at) to fetch only new/changed items.
    cursor = ctx.get_cursor()
    params: dict[str, str | int] = {
        "state": state,
        "sort": "updated",
        "direction": "asc",  # oldest first, so newest are at the end
        "per_page": per_page,
    }
    if cursor:
        params["since"] = cursor

    base = f"https://api.github.com/repos/{repo}"

    # --- Issues ---
    resp = await ctx.http.get(f"{base}/issues", params=params, headers=headers)
    if resp.status_code != 200:
        ctx.logger.warning(f"github-issues: API returned {resp.status_code}")
        return

    issues = resp.json()
    latest_updated = cursor

    for issue in issues:
        # Skip pull requests (GitHub's issues endpoint includes them).
        if "pull_request" in issue:
            continue

        number = issue["number"]
        updated = issue["updated_at"]
        fingerprint = f"{repo}:issue:{number}:{updated}"

        if ctx.is_known(fingerprint):
            continue

        labels = ", ".join(l["name"] for l in issue.get("labels", []))
        body_preview = (issue.get("body") or "")[:2000]

        ctx.emit_signal(
            title=f"#{number} {issue['title']}",
            content=body_preview,
            fingerprint=fingerprint,
            url=issue.get("html_url"),
            severity="info",
        )

        if not latest_updated or updated > latest_updated:
            latest_updated = updated

    # --- Comments (optional) ---
    if include_comments:
        comment_params: dict[str, str | int] = {"per_page": per_page, "sort": "created"}
        if cursor:
            comment_params["since"] = cursor
        resp2 = await ctx.http.get(
            f"{base}/issues/comments", params=comment_params, headers=headers
        )
        if resp2.status_code == 200:
            for comment in resp2.json():
                cid = comment["id"]
                created = comment["created_at"]
                fingerprint = f"{repo}:comment:{cid}"

                if ctx.is_known(fingerprint):
                    continue

                issue_url = comment.get("issue_url", "")
                body_preview = (comment.get("body") or "")[:2000]

                ctx.emit_signal(
                    title=f"评论 by {comment['user']['login']}",
                    content=body_preview,
                    fingerprint=fingerprint,
                    url=comment.get("html_url"),
                    severity="info",
                )

                if not latest_updated or created > latest_updated:
                    latest_updated = created

    # Update cursor for next poll.
    if latest_updated:
        ctx.set_cursor(latest_updated)

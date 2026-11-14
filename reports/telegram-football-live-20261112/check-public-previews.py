"""Recheck anonymous public Telegram previews; never authenticate, join, or post.

Run only inside Docker. Usage: python check-public-previews.py sources.json output.json
Requires the existing API image's pinned httpx dependency. Output is metadata only.
"""
import asyncio
import hashlib
import html
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import httpx


async def main():
    catalogue = json.loads(pathlib.Path(sys.argv[1]).read_text())
    handles = [row["handle"] for row in catalogue["rows"]]
    if len(handles) != len(set(h.lower() for h in handles)):
        raise ValueError("Duplicate handles")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z_0-9]{4,31}", h) for h in handles):
        raise ValueError("Invalid public handle")
    semaphore = asyncio.Semaphore(3)

    async def fetch(client, handle):
        async with semaphore:
            try:
                response = await client.get("https://t.me/s/" + handle)
                page = response.text
                title = re.search(r'<meta property="og:title" content="([^"]*)', page)
                timestamps = re.findall(r'<time[^>]*datetime="([^"]+)"', page)
                posts = re.findall(r'data-post="([^"]+)"', page)
                return {
                    "handle": handle,
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "http_status": response.status_code,
                    "title": html.unescape(title[1]) if title else None,
                    "visible_post_count": len(posts),
                    "latest_visible_post_utc": max(timestamps) if timestamps else None,
                    "recent_public_post_urls": ["https://t.me/" + p for p in posts[-3:]],
                    "html_sha256": hashlib.sha256(response.content).hexdigest(),
                }
            except httpx.HTTPError as error:
                return {"handle": handle, "error_category": type(error).__name__}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        rows = await asyncio.gather(*(fetch(client, handle) for handle in handles))
    output = {"unauthenticated_get_only": True, "rows": rows}
    pathlib.Path(sys.argv[2]).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checked": len(rows), "errors": sum("error_category" in row for row in rows)}))


if __name__ == "__main__":
    asyncio.run(main())

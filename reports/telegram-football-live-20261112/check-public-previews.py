"""Recheck anonymous public Telegram previews; never authenticate, join, or post.

Run only inside Docker. Usage: python check-public-previews.py sources.json output.json [since_utc]
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
from html.parser import HTMLParser
from itertools import combinations

import httpx


class PreviewPosts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.posts = {}
        self.current_post = None
        self.text_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get('class', '').split()
        if tag == 'div' and 'data-post' in attributes:
            self.current_post = {'posted_at': None, 'text': [], 'forwarded': False}
            self.posts[attributes['data-post']] = self.current_post
            self.text_depth = 0
        if self.current_post is None:
            return
        if tag == 'time' and self.current_post['posted_at'] is None and attributes.get('datetime'):
            try:
                posted_at = datetime.fromisoformat(attributes['datetime'])
            except ValueError:
                return
            if posted_at.utcoffset() is not None:
                self.current_post['posted_at'] = posted_at
        if any(value.endswith('_forwarded_from') for value in classes):
            self.current_post['forwarded'] = True
        if tag == 'div' and 'tgme_widget_message_text' in classes:
            self.text_depth = 1
        elif tag == 'div' and self.text_depth:
            self.text_depth += 1

    def handle_endtag(self, tag):
        if tag == 'div' and self.text_depth:
            self.text_depth -= 1

    def handle_data(self, data):
        if self.current_post is not None and self.text_depth:
            self.current_post['text'].append(data)

    def summarize(self, since):
        recent = [post for post in self.posts.values()
                  if post['posted_at'] is not None and post['posted_at'] >= since]
        fingerprints = []
        for post in recent:
            text = ' '.join(' '.join(post['text']).casefold().split())
            if len(text) >= 20:
                fingerprints.append((post['posted_at'], hashlib.sha256(text.encode()).digest()))
        return {
            'visible_posts_since_cutoff': len(recent),
            'visible_text_posts_since_cutoff': len(fingerprints),
            'visible_forwarded_posts_since_cutoff': sum(post['forwarded'] for post in recent),
        }, fingerprints


def matching_previews(fingerprints_by_handle):
    pairs = []
    for (first, a), (second, b) in combinations(fingerprints_by_handle.items(), 2):
        matching = {digest for _, digest in a} & {digest for _, digest in b}
        simultaneous = sum(left_digest == right_digest and abs((left_at - right_at).total_seconds()) <= 15
                           for left_at, left_digest in a for right_at, right_digest in b)
        if matching:
            pairs.append({'handles': [first, second], 'distinct_matching_texts': len(matching),
                          'matching_posts_within_15_seconds': simultaneous})
    return pairs


async def main():
    if len(sys.argv) not in (3, 4):
        raise SystemExit('Usage: check-public-previews.py sources.json output.json [since_utc]')
    catalogue = json.loads(pathlib.Path(sys.argv[1]).read_text())
    handles = [row["handle"] for row in catalogue["rows"]]
    if len(handles) != len(set(h.lower() for h in handles)):
        raise ValueError("Duplicate handles")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z_0-9]{4,31}", h) for h in handles):
        raise ValueError("Invalid public handle")
    since = datetime.fromisoformat(sys.argv[3]) if len(sys.argv) == 4 else None
    if since is not None and since.utcoffset() is None:
        raise ValueError('The cutoff requires a timezone')
    semaphore = asyncio.Semaphore(3)
    fingerprints_by_handle = {}

    async def fetch(client, handle):
        async with semaphore:
            try:
                response = await client.get("https://t.me/s/" + handle)
                page = response.text
                title = re.search(r'<meta property="og:title" content="([^"]*)', page)
                timestamps = re.findall(r'<time[^>]*datetime="([^"]+)"', page)
                posts = re.findall(r'data-post="([^"]+)"', page)
                data = {
                    "handle": handle,
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "http_status": response.status_code,
                    "title": html.unescape(title[1]) if title else None,
                    "visible_post_count": len(posts),
                    "latest_visible_post_utc": max(timestamps) if timestamps else None,
                    "recent_public_post_urls": ["https://t.me/" + p for p in posts[-3:]],
                    "html_sha256": hashlib.sha256(response.content).hexdigest(),
                }
                if since is not None and response.is_success:
                    preview = PreviewPosts()
                    preview.feed(page)
                    counts, fingerprints = preview.summarize(since)
                    fingerprints_by_handle[handle] = fingerprints
                    data.update(counts)
                return data
            except httpx.HTTPError as error:
                return {"handle": handle, "error_category": type(error).__name__}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        rows = await asyncio.gather(*(fetch(client, handle) for handle in handles))
    output = {"unauthenticated_get_only": True, "rows": rows}
    if since is not None:
        output['since_utc'] = since.astimezone(timezone.utc).isoformat()
        output['text_overlap'] = matching_previews({h: fingerprints_by_handle.get(h, []) for h in handles})
    pathlib.Path(sys.argv[2]).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"checked": len(rows), "errors": sum("error_category" in row for row in rows)}))


if __name__ == "__main__":
    asyncio.run(main())

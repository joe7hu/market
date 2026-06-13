"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations


BIRDCLAW_TWEETS_SOURCE_ID = "birdclaw_primary_tweets"
BROWSER_CAPTURES_SOURCE_ID = "browser_primary_captures"
RAW_SOURCE_IDS = (BIRDCLAW_TWEETS_SOURCE_ID, BROWSER_CAPTURES_SOURCE_ID)

TWITTER_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com"}
URL_KEYS = {
    "url",
    "sourceUrl",
    "source_url",
    "canonicalUrl",
    "expandedUrl",
    "originalUrl",
    "activeTabUrl",
    "locationHref",
}
URL_KEY_ORDER = ("url", "sourceUrl", "source_url", "canonicalUrl", "expandedUrl", "originalUrl", "activeTabUrl", "locationHref")

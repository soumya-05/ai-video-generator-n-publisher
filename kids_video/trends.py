"""Trend + seasonal signals used to seed original script generation.

Deliberately returns *keywords and themes*, never a ready-made topic string.
An earlier version fed raw YouTube titles straight into the script prompt, which
produced garbage titles full of hashtag soup. Signals here are inspiration only;
Claude picks the actual subject from them.
"""

import html
import re
import unicodedata
from datetime import date
from typing import Dict, List

import requests

SEARCH_QUERIES = {
    "how_everyday_machines_work": "how it works hindi मशीन कैसे काम करती है",
    "physics_and_energy": "physics explained hindi भौतिकी ऊर्जा समझाया",
    "space_and_astronomy": "space astronomy hindi अंतरिक्ष ब्रह्मांड विज्ञान",
    "technology_and_computing": "technology explained hindi कंप्यूटर टेक्नोलॉजी",
    "biology_and_the_human_body": "human body biology hindi शरीर जीव विज्ञान",
    "medical_science": "medical science hindi चिकित्सा विज्ञान बीमारी",
    "engineering_and_infrastructure": "engineering megastructure hindi इंजीनियरिंग निर्माण",
}

# Noise that shows up in nearly every science-channel title and carries no
# meaning on its own.
STOPWORDS = {
    "hindi", "video", "videos", "shorts", "short", "new", "best", "full",
    "episode", "part", "hd", "official", "explained", "explain", "facts",
    "fact", "science", "how", "what", "why", "works", "work", "does", "did",
    "for", "and", "the", "with", "of", "in", "on", "you", "your", "this",
    "that", "ka", "ki", "ke", "hai", "kya", "kaise", "kyu", "kyun",
    "विज्ञान", "हिंदी", "नई", "वीडियो", "क्या", "कैसे", "क्यों", "है", "हैं",
    "का", "की", "के", "और", "में", "एक", "से", "को", "पर", "तथ्य",
}

SEASONS = {
    1: "winter (सर्दी)", 2: "late winter", 3: "spring (बसंत)",
    4: "spring turning hot", 5: "summer (गर्मी)", 6: "early monsoon",
    7: "monsoon (बरसात)", 8: "monsoon", 9: "post-monsoon",
    10: "autumn (शरद)", 11: "early winter", 12: "winter (सर्दी)",
}


def _clean_title(raw: str) -> str:
    """Strip HTML entities, hashtags, URLs, emoji and separator clutter."""
    text = html.unescape(html.unescape(raw))  # titles are often double-encoded
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"#\S+", " ", text)
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"[|\-–—_/\\*•:;!?.,\"'()\[\]{}]", " ", text)
    # Drop emoji and other symbol/pictograph codepoints.
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"So", "Cs", "Sk"})
    return re.sub(r"\s+", " ", text).strip()


def _keywords(titles: List[str], limit: int) -> List[str]:
    counts: Dict[str, int] = {}
    for title in titles:
        for word in _clean_title(title).split():
            token = word.strip().lower()
            if len(token) < 3 or token in STOPWORDS or token.isdigit():
                continue
            counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:limit]]


class TrendSignals:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("trends")
        self.api_key = config.key("youtube_data_api_key")

    def collect(self, content_type: str, today: date) -> dict:
        return {
            "date": today.isoformat(),
            "weekday": today.strftime("%A"),
            "month": today.strftime("%B"),
            "season": SEASONS[today.month],
            "trending_keywords": self._trending_keywords(content_type),
        }

    def _trending_keywords(self, content_type: str, limit: int = 12) -> List[str]:
        """Top themes currently doing well in Hindi science content on YouTube."""
        if not self.api_key:
            self.logger.info("No YouTube Data API key; skipping trend signals")
            return []

        query = SEARCH_QUERIES.get(content_type, "science explained hindi")
        try:
            resp = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet",
                    "q": query,
                    "regionCode": "IN",
                    "relevanceLanguage": "hi",
                    "type": "video",
                    "order": "viewCount",
                    "publishedAfter": "2026-01-01T00:00:00Z",
                    "maxResults": 25,
                    "key": self.api_key,
                },
                timeout=20,
            )
            resp.raise_for_status()
            titles = [
                item["snippet"]["title"]
                for item in resp.json().get("items", [])
                if item.get("snippet", {}).get("title")
            ]
        except (requests.RequestException, ValueError, KeyError) as exc:
            # Trends are a nice-to-have; never fail the run over them.
            self.logger.warning("Trend fetch failed, continuing without: %s", exc)
            return []

        keywords = _keywords(titles, limit)
        self.logger.info("Trending themes: %s", ", ".join(keywords) or "(none)")
        return keywords

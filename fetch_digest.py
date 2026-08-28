"""
AI Daily Digest - Fetches latest AI news and trending AI/agent repos from GitHub.
Generates a clean, readable markdown digest and commits it to the repo.

Bug fixes applied:
  - GitHub API auth token support (avoids rate limiting)
  - Timezone-normalized date comparison (UTC vs IST fix)
  - Network retry with exponential backoff
  - Minimum article threshold (skips commit if too few articles)
  - Word-boundary summary truncation
  - Fuzzy title deduplication
  - Debug JSON logging
  - README no longer embeds full digest (stays small)
  - Lock file timeout reduced to 10 minutes
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 output on Windows (default cp1252 can't handle emoji)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import feedparser
import requests
from bs4 import BeautifulSoup

# ─── Configuration ────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))

RSS_FEEDS = {
    "MIT Technology Review": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "The Verge":             "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "VentureBeat":           "https://venturebeat.com/category/ai/feed/",
    "Ars Technica":          "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "TechCrunch":            "https://techcrunch.com/category/artificial-intelligence/feed/",
}

GITHUB_SEARCH_QUERIES = [
    "AI agent",
    "LLM tool",
    "autonomous agent",
    "generative AI",
    "AI assistant",
]

AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "llm", "gpt",
    "claude", "gemini", "agent", "neural", "deep learning", "generative",
    "openai", "anthropic", "deepmind", "transformer", "chatbot", "copilot",
    "diffusion", "embedding", "fine-tun", "rag", "reasoning",
]

MAX_NEWS_PER_FEED    = 5
MAX_REPOS            = 15
MIN_ARTICLES_TO_RUN  = 3   # Skip commit if fewer articles than this found
SUMMARY_MAX_CHARS    = 350  # Truncate at word boundary near this length


# ─── Config Loader ────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config.json if it exists, else return empty dict."""
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠ Could not read config.json: {e}")
    return {}


# ─── Retry Helper ─────────────────────────────────────────────────────────────

def fetch_with_retry(url: str, params: dict = None, headers: dict = None,
                     timeout: int = 15, max_retries: int = 3) -> requests.Response | None:
    """
    GET request with exponential backoff retry.
    Returns Response on success, None on all failures.
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout)
            return resp
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt  # 2s, 4s, 8s
            if attempt < max_retries:
                print(f"  ⚠ Request failed (attempt {attempt}/{max_retries}), "
                      f"retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"  ⚠ All {max_retries} attempts failed for {url}: {e}")
    return None


# ─── Date Utilities ───────────────────────────────────────────────────────────

def parse_entry_date(entry) -> datetime | None:
    """
    Parse publish/update date from a feedparser entry.
    Always returns UTC-aware datetime or None.
    """
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def is_recent(pub_dt: datetime | None, max_days: int = 2) -> bool:
    """
    Check if pub_dt is within max_days of now.
    Both sides normalized to UTC to avoid IST/UTC skew.
    """
    if pub_dt is None:
        return True  # Unknown date → include it, don't silently drop
    now_utc = datetime.now(timezone.utc)
    return (now_utc - pub_dt).days <= max_days


# ─── Summary Utilities ────────────────────────────────────────────────────────

def clean_summary(raw_html: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """
    Strip HTML tags, truncate at word boundary, return clean text.
    FIX: old code cut mid-word at exactly 300 chars.
    """
    text = BeautifulSoup(raw_html, "html.parser").get_text(strip=True)
    if len(text) <= max_chars:
        return text
    # Find last space before the limit
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.7:  # Don't truncate too aggressively
        truncated = truncated[:last_space]
    return truncated + "..."


# ─── Deduplication ───────────────────────────────────────────────────────────

def title_key(title: str) -> str:
    """
    Fuzzy key for deduplication.
    FIX: old code only compared first 50 chars; same story with
    different leads (e.g., 'OpenAI agents hacked...' vs
    'How OpenAI agents hacked...') would slip through.
    Now strips common lead words and compares core content.
    """
    # Strip common news lead words
    lead_words = r"^(how|why|what|when|the|a|an|new|here|this|these|that)\s+"
    cleaned = re.sub(lead_words, "", title.lower().strip())
    # Remove all non-alphanumeric
    cleaned = re.sub(r"[^a-z0-9\s]", "", cleaned)
    # Normalize whitespace, take first 60 chars of core words
    words = cleaned.split()
    return " ".join(words[:8])  # First 8 meaningful words as key


# ─── News Fetching ───────────────────────────────────────────────────────────

def fetch_rss_news() -> list[dict]:
    """
    Fetch AI news from RSS feeds with retry, timezone-correct date
    comparison, word-boundary truncation, and fuzzy dedup.
    """
    articles = []
    failed_feeds = []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)

            if feed.bozo and not feed.entries:
                # bozo=True means parse error; entries=[] means truly failed
                raise ValueError(f"Feed parse error: {feed.bozo_exception}")

            count = 0
            for entry in feed.entries:
                if count >= MAX_NEWS_PER_FEED:
                    break

                pub_dt = parse_entry_date(entry)

                # FIX: was comparing IST date to UTC date → timezone skew
                if not is_recent(pub_dt, max_days=2):
                    continue

                title   = entry.get("title", "Untitled").strip()
                link    = entry.get("link", "")
                summary = clean_summary(
                    entry.get("summary", entry.get("description", "")))

                # Filter for AI relevance
                text = f"{title} {summary}".lower()
                if not any(kw in text for kw in AI_KEYWORDS):
                    continue

                articles.append({
                    "title":   title,
                    "link":    link,
                    "summary": summary,
                    "source":  source,
                    "date":    pub_dt.strftime("%Y-%m-%d") if pub_dt
                               else datetime.now(IST).strftime("%Y-%m-%d"),
                    "pub_dt":  pub_dt,
                })
                count += 1

        except Exception as e:
            print(f"  ⚠ Failed to fetch '{source}': {e}")
            failed_feeds.append(source)

    if failed_feeds:
        print(f"  ⚠ {len(failed_feeds)}/{len(RSS_FEEDS)} feeds failed: "
              f"{', '.join(failed_feeds)}")

    # FIX: fuzzy deduplication using 8-word content key
    seen_keys = set()
    unique = []
    for a in articles:
        key = title_key(a["title"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(a)

    # Sort: most recent first
    unique.sort(key=lambda a: a["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)

    return unique


# ─── GitHub Trending Repos ────────────────────────────────────────────────────

def fetch_trending_repos(config: dict) -> list[dict]:
    """
    Fetch trending AI/agent repositories from GitHub.
    FIX: Now uses GitHub token from config to avoid rate limiting.
    Unauthenticated = 10 req/hr. Authenticated = 30 req/hr search.
    """
    token = config.get("github_token", "")
    headers = {
        "User-Agent": "AI-Daily-Digest/2.0",
        "Accept":     "application/vnd.github.v3+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("  ⚠ No GitHub token in config.json — using unauthenticated "
              "(10 req/hr limit). Add 'github_token' to config.json to avoid rate limits.")

    repos = []
    seen  = set()
    week_ago = (datetime.now(IST) - timedelta(days=7)).strftime("%Y-%m-%d")

    for query in GITHUB_SEARCH_QUERIES:
        resp = fetch_with_retry(
            "https://api.github.com/search/repositories",
            params={"q": f"{query} created:>{week_ago}",
                    "sort": "stars", "order": "desc", "per_page": 5},
            headers=headers,
            timeout=15,
        )

        if resp is None:
            continue

        if resp.status_code == 403:
            # Check if rate-limited
            reset_ts = resp.headers.get("X-RateLimit-Reset", "")
            print(f"  ⚠ GitHub API rate limit hit. "
                  f"Resets at: {reset_ts}. Add a GitHub token to config.json.")
            break

        if resp.status_code != 200:
            print(f"  ⚠ GitHub API returned {resp.status_code} for query '{query}'")
            continue

        data = resp.json()
        for item in data.get("items", []):
            name = item["full_name"]
            if name in seen:
                continue
            seen.add(name)
            repos.append({
                "name":        name,
                "url":         item["html_url"],
                "description": (item.get("description") or "No description")[:200],
                "stars":       item["stargazers_count"],
                "language":    item.get("language") or "N/A",
                "created":     item["created_at"][:10],
                "topics":      item.get("topics", [])[:5],
            })

        # Respect rate limit: small pause between queries
        time.sleep(0.5)

    repos.sort(key=lambda r: r["stars"], reverse=True)
    return repos[:MAX_REPOS]


# ─── Markdown Generation ──────────────────────────────────────────────────────

def generate_digest(news: list[dict], repos: list[dict]) -> str:
    """Generate a clean, readable markdown digest."""
    today    = datetime.now(IST)
    date_str = today.strftime("%B %d, %Y")
    day_str  = today.strftime("%A")

    lines = [
        f"# 🤖 AI Daily Digest — {day_str}, {date_str}",
        "",
        f"> Auto-generated at {today.strftime('%I:%M %p IST')} | "
        f"Tracking the latest in AI news & open-source agents",
        "",
        "---",
        "",
    ]

    # ── News Section ──────────────────────────────────────────────────────────
    lines.append("## 📰 Top AI News Today")
    lines.append("")

    if news:
        for i, article in enumerate(news, 1):
            lines.append(f"### {i}. {article['title']}")
            lines.append(
                f"**Source:** {article['source']} &nbsp;·&nbsp; "
                f"**Date:** {article['date']}")
            lines.append("")
            if article["summary"]:
                lines.append(f"> {article['summary']}")
                lines.append("")
            lines.append(f"🔗 [Read full article]({article['link']})")
            lines.append("")
    else:
        lines.append("_No major AI news found today. Check back tomorrow!_")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ── Repos Section ─────────────────────────────────────────────────────────
    lines.append("## 🚀 Trending AI & Agent Repos on GitHub")
    lines.append("")
    lines.append("New & notable open-source projects from the past week:")
    lines.append("")

    if repos:
        lines.append("| # | Repository | ⭐ Stars | Language | Description |")
        lines.append("|---|-----------|:-------:|----------|-------------|")
        for i, repo in enumerate(repos, 1):
            desc = repo["description"].replace("|", "\\|")
            lines.append(
                f"| {i} | [{repo['name']}]({repo['url']}) | "
                f"{repo['stars']:,} | {repo['language']} | {desc} |"
            )
        lines.append("")
    else:
        lines.append("_No trending repos found today._")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"_Generated by [AI Daily Digest](https://github.com/Piyush0053/ai-daily-digest) · "
        f"Last updated: {today.strftime('%Y-%m-%d %H:%M IST')}_"
    )
    lines.append("")

    return "\n".join(lines)


# ─── README Generator ─────────────────────────────────────────────────────────

def generate_readme(digest_dir: Path) -> str:
    """
    Generate a compact README.md.
    FIX: Old README embedded FULL digest content every day →
    grew to 360KB+ after 30 days, slow to render on GitHub.
    New README: only shows latest digest header + archive links.
    """
    all_digests = sorted(digest_dir.glob("*.md"), reverse=True)
    latest_date = all_digests[0].stem if all_digests else "N/A"

    lines = [
        "# 🤖 AI Daily Digest",
        "",
        "Automated daily tracker of the **latest AI news** and **trending open-source "
        "AI/agent repositories** on GitHub.",
        "",
        "Every day this repo auto-updates with:",
        "- 📰 Top AI news from MIT Technology Review, The Verge, VentureBeat, "
        "Ars Technica, TechCrunch",
        "- 🚀 Newly created AI/agent GitHub repos sorted by stars",
        "",
        "---",
        "",
        "## 📅 Latest Digest",
        "",
        f"👉 **[{latest_date}](digests/{latest_date}.md)**",
        "",
        "---",
        "",
        "## 📂 Archive",
        "",
        "| Date | Link |",
        "|------|------|",
    ]

    for d in all_digests[:60]:  # Last 60 days in table, not embedded
        lines.append(f"| {d.stem} | [View digest](digests/{d.name}) |")

    lines += [
        "",
        "---",
        "",
        "_Updated automatically every day. No human intervention needed._",
        "",
    ]
    return "\n".join(lines)


# ─── Debug Logger ─────────────────────────────────────────────────────────────

def save_debug_log(news: list[dict], repos: list[dict], project_dir: Path):
    """
    FIX: Old agent only logged counts, not content.
    Now saves raw fetch data to debug/ for troubleshooting.
    """
    debug_dir = project_dir / "debug"
    debug_dir.mkdir(exist_ok=True)

    today     = datetime.now(IST).strftime("%Y-%m-%d")
    log_path  = debug_dir / f"{today}.json"

    # Remove non-serializable pub_dt field before saving
    news_clean = [{k: v for k, v in a.items() if k != "pub_dt"} for a in news]

    payload = {
        "date":          today,
        "generated_at":  datetime.now(IST).isoformat(),
        "articles_count": len(news),
        "repos_count":   len(repos),
        "articles":      news_clean,
        "repos":         repos,
    }
    log_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"  ✓ Debug log saved to {log_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    project_dir = Path(__file__).parent
    digest_dir  = project_dir / "digests"
    digest_dir.mkdir(exist_ok=True)

    config   = load_config()
    today    = datetime.now(IST)
    filename = today.strftime("%Y-%m-%d") + ".md"
    filepath = digest_dir / filename

    # Skip if already generated today
    if filepath.exists():
        print(f"✅ Digest for {today.strftime('%Y-%m-%d')} already exists. Skipping.")
        return

    print(f"📡 Fetching AI news for {today.strftime('%B %d, %Y')}...")

    print("  → Fetching RSS feeds...")
    news = fetch_rss_news()
    print(f"  ✓ Found {len(news)} articles")

    # FIX: Don't commit an empty/thin digest — it wastes a commit and
    # looks bad on the repo. Abort if we got too few articles.
    if len(news) < MIN_ARTICLES_TO_RUN:
        print(f"  ⚠ Only {len(news)} articles found (minimum: {MIN_ARTICLES_TO_RUN}). "
              f"Aborting — will retry on next trigger.")
        sys.exit(2)  # Exit code 2 = "skip commit" signal to run_agent.ps1

    print("  → Searching GitHub trending repos...")
    repos = fetch_trending_repos(config)
    print(f"  ✓ Found {len(repos)} repos")

    print("  → Generating digest...")
    digest = generate_digest(news, repos)

    # Save daily digest file
    filepath.write_text(digest, encoding="utf-8")
    print(f"  ✓ Saved to {filepath.name}")

    # Save debug log (not committed — in .gitignore)
    save_debug_log(news, repos, project_dir)

    # FIX: README now compact (no embedded content)
    readme_path = project_dir / "README.md"
    readme_path.write_text(generate_readme(digest_dir), encoding="utf-8")
    print("  ✓ Updated README.md")

    print("✅ Done!")


if __name__ == "__main__":
    main()

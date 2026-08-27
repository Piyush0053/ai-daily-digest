"""
AI Daily Digest - Fetches latest AI news and trending AI/agent repos from GitHub.
Generates a clean, readable markdown digest and commits it to the repo.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup

# ─── Configuration ───────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))

RSS_FEEDS = {
    "MIT Technology Review - AI": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "The Verge - AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "VentureBeat - AI": "https://venturebeat.com/category/ai/feed/",
    "Ars Technica - AI": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "TechCrunch - AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
}

GITHUB_SEARCH_QUERIES = [
    "AI agent",
    "LLM tool",
    "autonomous agent",
    "generative AI",
    "AI assistant",
]

HEADERS = {
    "User-Agent": "AI-Daily-Digest/1.0 (https://github.com)",
    "Accept": "application/vnd.github.v3+json",
}

MAX_NEWS_PER_FEED = 5
MAX_REPOS = 15


# ─── News Fetching ──────────────────────────────────────────────────────────

def fetch_rss_news() -> list[dict]:
    """Fetch AI news from RSS feeds."""
    articles = []
    today = datetime.now(IST).date()

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            count = 0
            for entry in feed.entries:
                if count >= MAX_NEWS_PER_FEED:
                    break

                # Try to get publish date
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                # Only include articles from last 2 days
                if published and (today - published.date()).days > 2:
                    continue

                # Clean up summary
                summary = ""
                if hasattr(entry, "summary"):
                    soup = BeautifulSoup(entry.summary, "html.parser")
                    summary = soup.get_text(strip=True)[:300]

                title = entry.get("title", "Untitled")
                link = entry.get("link", "")

                # Filter for AI relevance
                ai_keywords = ["ai", "artificial intelligence", "machine learning",
                               "llm", "gpt", "claude", "gemini", "agent", "neural",
                               "deep learning", "generative", "openai", "anthropic",
                               "google deepmind", "transformer", "chatbot", "copilot"]
                text = f"{title} {summary}".lower()
                if not any(kw in text for kw in ai_keywords):
                    continue

                articles.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source.split(" - ")[0],
                    "date": published.strftime("%Y-%m-%d") if published else str(today),
                })
                count += 1

        except Exception as e:
            print(f"  ⚠ Failed to fetch {source}: {e}")

    # Deduplicate by title similarity
    seen_titles = set()
    unique = []
    for a in articles:
        key = re.sub(r"[^a-z0-9]", "", a["title"].lower())[:50]
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(a)

    return unique


# ─── GitHub Trending Repos ───────────────────────────────────────────────────

def fetch_trending_repos() -> list[dict]:
    """Fetch trending AI/agent repositories from GitHub."""
    repos = []
    seen = set()
    week_ago = (datetime.now(IST) - timedelta(days=7)).strftime("%Y-%m-%d")

    for query in GITHUB_SEARCH_QUERIES:
        try:
            url = "https://api.github.com/search/repositories"
            params = {
                "q": f"{query} created:>{week_ago}",
                "sort": "stars",
                "order": "desc",
                "per_page": 5,
            }
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("items", []):
                    name = item["full_name"]
                    if name in seen:
                        continue
                    seen.add(name)
                    repos.append({
                        "name": name,
                        "url": item["html_url"],
                        "description": (item.get("description") or "No description")[:200],
                        "stars": item["stargazers_count"],
                        "language": item.get("language") or "N/A",
                        "created": item["created_at"][:10],
                    })
            elif resp.status_code == 403:
                print(f"  ⚠ GitHub API rate limit hit for query: {query}")
                break
        except Exception as e:
            print(f"  ⚠ Failed GitHub search for '{query}': {e}")

    # Sort by stars and take top repos
    repos.sort(key=lambda r: r["stars"], reverse=True)
    return repos[:MAX_REPOS]


# ─── Markdown Generation ────────────────────────────────────────────────────

def generate_digest(news: list[dict], repos: list[dict]) -> str:
    """Generate a clean, readable markdown digest."""
    today = datetime.now(IST)
    date_str = today.strftime("%B %d, %Y")
    day_str = today.strftime("%A")

    lines = [
        f"# 🤖 AI Daily Digest — {day_str}, {date_str}",
        "",
        f"> Auto-generated at {today.strftime('%I:%M %p IST')} | "
        f"Tracking the latest in AI news & open-source agents",
        "",
        "---",
        "",
    ]

    # ── News Section ──
    lines.append("## 📰 Top AI News Today")
    lines.append("")

    if news:
        for i, article in enumerate(news, 1):
            lines.append(f"### {i}. {article['title']}")
            lines.append(f"**Source:** {article['source']} · **Date:** {article['date']}")
            lines.append("")
            if article["summary"]:
                lines.append(f"> {article['summary']}")
                lines.append("")
            lines.append(f"🔗 [Read more]({article['link']})")
            lines.append("")
    else:
        lines.append("_No major AI news found today. Check back tomorrow!_")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ── Trending Repos Section ──
    lines.append("## 🚀 Trending AI & Agent Repos on GitHub")
    lines.append("")
    lines.append("New & notable open-source projects from the past week:")
    lines.append("")

    if repos:
        lines.append("| # | Repository | ⭐ Stars | Language | Description |")
        lines.append("|---|-----------|---------|----------|-------------|")
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
        f"_Generated by [AI Daily Digest](https://github.com) · "
        f"Last updated: {today.strftime('%Y-%m-%d %H:%M IST')}_"
    )
    lines.append("")

    return "\n".join(lines)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    project_dir = Path(__file__).parent
    digest_dir = project_dir / "digests"
    digest_dir.mkdir(exist_ok=True)

    today = datetime.now(IST)
    filename = today.strftime("%Y-%m-%d") + ".md"
    filepath = digest_dir / filename

    # Check if already generated today
    if filepath.exists():
        print(f"✅ Digest for {today.strftime('%Y-%m-%d')} already exists. Skipping.")
        return

    print(f"📡 Fetching AI news for {today.strftime('%B %d, %Y')}...")

    print("  → Fetching RSS feeds...")
    news = fetch_rss_news()
    print(f"  ✓ Found {len(news)} articles")

    print("  → Searching GitHub trending repos...")
    repos = fetch_trending_repos()
    print(f"  ✓ Found {len(repos)} repos")

    print("  → Generating digest...")
    digest = generate_digest(news, repos)

    # Save the daily digest
    filepath.write_text(digest, encoding="utf-8")
    print(f"  ✓ Saved to {filepath}")

    # Also update README.md with latest digest
    readme_path = project_dir / "README.md"
    readme_content = [
        "# 🤖 AI Daily Digest",
        "",
        "Automated daily tracker of AI news, new agents, and trending repositories.",
        "",
        "Every day, this repo is automatically updated with:",
        "- 📰 **Top AI news** from major tech publications",
        "- 🚀 **Trending AI/agent repos** newly created on GitHub",
        "",
        "## 📅 Latest Digest",
        "",
        digest,
        "",
        "## 📂 Archive",
        "",
    ]

    # List all digests in reverse chronological order
    all_digests = sorted(digest_dir.glob("*.md"), reverse=True)
    for d in all_digests[:30]:  # Show last 30 days
        date_name = d.stem
        readme_content.append(f"- [{date_name}](digests/{d.name})")

    readme_content.append("")
    readme_content.append("---")
    readme_content.append("")
    readme_content.append(
        "_This repo is updated automatically by a local agent. "
        "No human intervention needed!_"
    )
    readme_content.append("")

    readme_path.write_text("\n".join(readme_content), encoding="utf-8")
    print("  ✓ Updated README.md")

    print("✅ Done!")


if __name__ == "__main__":
    main()

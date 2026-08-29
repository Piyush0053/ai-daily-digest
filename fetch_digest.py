"""
AI Daily Digest - Phase 2
Fetches AI news + GitHub repos, enriches them with NVIDIA NIM AI analysis,
and generates a smart, readable markdown digest.

Phase 2 additions:
  - NVIDIA NIM AI summarization (plain-English summaries for non-experts)
  - Importance scoring: Breaking / Important / Interesting
  - Category tagging: Safety / Models / Agents / Tools / Robotics / Business / Research / Policy
  - AI-written one-liner per article (what happened + why it matters)
  - Grouped digest sections by importance
  - Graceful fallback to raw RSS summary if AI enrichment fails
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 output on Windows
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import feedparser
import requests
from bs4 import BeautifulSoup

# ─── Constants ────────────────────────────────────────────────────────────────

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

MAX_NEWS_PER_FEED   = 5
MAX_REPOS           = 15
MIN_ARTICLES_TO_RUN = 3
SUMMARY_MAX_CHARS   = 350

# LLM Engine config (NVIDIA NIM)
NVIDIA_MODEL      = "meta/llama-3.2-11b-vision-instruct"
NVIDIA_ENDPOINT   = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_CHUNK_SIZE = 5               # 5 articles per batch for optimal latency & stability

IMPORTANCE_EMOJI = {
    "breaking":    "🔥",
    "important":   "📌",
    "interesting": "💡",
}

CATEGORY_EMOJI = {
    "Safety":    "🛡️",
    "Models":    "🧠",
    "Agents":    "🤖",
    "Tools":     "🔧",
    "Robotics":  "🦾",
    "Business":  "💼",
    "Research":  "🔬",
    "Policy":    "⚖️",
    "Other":     "📎",
}


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠ Could not read config.json: {e}")
    return {}


# ─── Network Helpers ──────────────────────────────────────────────────────────

def fetch_with_retry(url, params=None, headers=None, timeout=15, max_retries=3):
    """GET with exponential backoff retry. Returns Response or None."""
    for attempt in range(1, max_retries + 1):
        try:
            return requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            if attempt < max_retries:
                print(f"  ⚠ Retry {attempt}/{max_retries} in {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"  ⚠ All retries failed for {url}: {e}")
    return None


# ─── Date Helpers ─────────────────────────────────────────────────────────────

def parse_entry_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def is_recent(pub_dt, max_days=2):
    if pub_dt is None:
        return True
    return (datetime.now(timezone.utc) - pub_dt).days <= max_days


# ─── Text Helpers ─────────────────────────────────────────────────────────────

def clean_summary(raw_html, max_chars=SUMMARY_MAX_CHARS):
    text = BeautifulSoup(raw_html, "html.parser").get_text(strip=True)
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.7:
        truncated = truncated[:last_space]
    return truncated + "..."


def title_key(title):
    lead = r"^(how|why|what|when|the|a|an|new|here|this|these|that)\s+"
    cleaned = re.sub(lead, "", title.lower().strip())
    cleaned = re.sub(r"[^a-z0-9\s]", "", cleaned)
    return " ".join(cleaned.split()[:8])


# ─── Groq AI Enrichment ───────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    """
    Strip <think>...</think> blocks from qwen/reasoning models.
    These models add internal reasoning before the actual output.
    """
    return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()


def call_nvidia(prompt: str, config: dict, max_tokens: int = 1800) -> str | None:
    """
    Call NVIDIA NIM chat completions API.
    Returns clean response text or None on failure.
    """
    api_key = config.get("nvidia_api_key", "")
    if not api_key:
        return None

    for attempt in range(1, 3):
        try:
            resp = requests.post(
                NVIDIA_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": NVIDIA_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.1,
                },
                timeout=45,
            )

            if resp.status_code == 200:
                raw   = resp.json()["choices"][0]["message"]["content"]
                usage = resp.json().get("usage", {})
                content = strip_thinking(raw).strip()

                if not content:
                    print(f"    ⚠ Empty response from NVIDIA NIM")
                    return None

                print(f"    ✓ NVIDIA NIM ({NVIDIA_MODEL.split('/')[-1]}) — "
                      f"{usage.get('prompt_tokens', 0)} in / "
                      f"{usage.get('completion_tokens', 0)} out tokens")
                return content

            elif resp.status_code == 429:
                wait = 3 * attempt
                print(f"    ⚠ NVIDIA rate limited (429), waiting {wait}s... (attempt {attempt}/2)")
                time.sleep(wait)
                continue

            else:
                print(f"    ⚠ NVIDIA error {resp.status_code}: {resp.text[:120]}")
                return None

        except Exception as e:
            print(f"    ⚠ NVIDIA request failed: {e}")
            return None

    return None




def extract_json(text: str):
    """
    Robustly extract a JSON array or object from LLM response text.
    Handles: markdown code fences, extra prose before/after JSON,
    and minor trailing comma issues.
    """
    if not text:
        return None

    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip().strip("`").strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the outermost [ ] or { } block
    for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        match = re.search(pattern, text)
        if match:
            candidate = match.group()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                # Try fixing trailing commas (common LLM mistake)
                fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    pass

    return None


def _build_enrichment_prompt(articles: list[dict], index_offset: int = 0) -> str:
    """Build the enrichment prompt for a batch of articles."""
    article_lines = []
    for i, a in enumerate(articles):
        # Use trimmed summary in the prompt to keep token count low
        short_summary = a["summary"][:120].rsplit(" ", 1)[0] if len(a["summary"]) > 120 else a["summary"]
        article_lines.append(
            f"[{index_offset + i}] Title: {a['title']}\n"
            f"    Source: {a['source']}\n"
            f"    Summary: {short_summary}"
        )
    articles_text = "\n\n".join(article_lines)

    n = len(articles)
    return f"""You are an AI news analyst. Analyze these {n} AI news articles.

For EACH article return a JSON object in an array with these EXACT fields:
- "index": integer (match the [N] shown in input)
- "importance": EXACTLY one of: "breaking", "important", "interesting"
  * breaking = safety incident, legal/regulatory, landmark model release
  * important = new tool/research, significant product, industry shift
  * interesting = niche updates, opinion pieces, minor announcements
- "category": EXACTLY one of: "Safety", "Models", "Agents", "Tools", "Robotics", "Business", "Research", "Policy", "Other"
- "one_liner": max 18 words — what happened AND why it matters, punchy and specific
- "ai_summary": 2-3 plain English sentences. No jargon. Easy for a curious teenager to understand.

IMPORTANT: Return ONLY a valid JSON array. No explanation, no markdown fences, no other text.

Articles:
{articles_text}"""


def enrich_articles(articles: list[dict], config: dict) -> list[dict]:
    """
    Enrich articles with AI using NVIDIA NIM (meta/llama-3.2-11b-vision-instruct).
    Fallback: Raw RSS summaries.
    """
    has_nvidia = bool(config.get("nvidia_api_key"))
    total = len(articles)

    if not has_nvidia or not articles:
        print("  ⚠ No NVIDIA API key configured — skipping AI enrichment")
        return articles

    enrichments: dict[int, dict] = {}

    print(f"  → Enriching {total} articles via NVIDIA NIM (chunks of {NVIDIA_CHUNK_SIZE})...")
    chunks = [list(range(i, min(i + NVIDIA_CHUNK_SIZE, total)))
              for i in range(0, total, NVIDIA_CHUNK_SIZE)]

    for chunk_num, chunk_indices in enumerate(chunks, 1):
        chunk_articles = [articles[i] for i in chunk_indices]
        start_idx = chunk_indices[0]

        print(f"    → NVIDIA Chunk {chunk_num}/{len(chunks)} (articles {start_idx}–{chunk_indices[-1]})...")

        prompt = _build_enrichment_prompt(chunk_articles, index_offset=start_idx)
        raw    = call_nvidia(prompt, config, max_tokens=1800)
        parsed = extract_json(raw) if raw else None

        if parsed and isinstance(parsed, list):
            for item in parsed:
                try:
                    idx = int(item.get("index", -1))
                    if idx in chunk_indices:
                        enrichments[idx] = item
                except (ValueError, TypeError):
                    continue

    # ── Apply enrichments ────────────────────────────────────────────────────
    enriched_count = 0
    for i, article in enumerate(articles):
        data = enrichments.get(i)
        if data:
            importance = str(data.get("importance", "interesting")).lower()
            article["importance"] = importance if importance in IMPORTANCE_EMOJI else "interesting"
            article["category"]   = data.get("category", "Other")
            article["one_liner"]  = data.get("one_liner", "")
            article["ai_summary"] = data.get("ai_summary", article["summary"])
            enriched_count += 1
        else:
            article["importance"] = "interesting"
            article["category"]   = "Other"
            article["one_liner"]  = ""
            article["ai_summary"] = article["summary"]

    provider_used = "NVIDIA NIM" if enrichments else "Raw RSS"
    print(f"  ✓ Enriched {enriched_count}/{total} articles (Provider: {provider_used})")
    return articles


# ─── RSS Fetching ─────────────────────────────────────────────────────────────

def fetch_rss_news() -> list[dict]:
    articles     = []
    failed_feeds = []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                raise ValueError(f"Feed parse error: {feed.bozo_exception}")

            count = 0
            for entry in feed.entries:
                if count >= MAX_NEWS_PER_FEED:
                    break

                pub_dt = parse_entry_date(entry)
                if not is_recent(pub_dt, max_days=2):
                    continue

                title   = entry.get("title", "Untitled").strip()
                link    = entry.get("link", "")
                summary = clean_summary(
                    entry.get("summary", entry.get("description", "")))

                text = f"{title} {summary}".lower()
                if not any(kw in text for kw in AI_KEYWORDS):
                    continue

                articles.append({
                    "title":      title,
                    "link":       link,
                    "summary":    summary,
                    "source":     source,
                    "date":       pub_dt.strftime("%Y-%m-%d") if pub_dt
                                  else datetime.now(IST).strftime("%Y-%m-%d"),
                    "pub_dt":     pub_dt,
                    # Enrichment fields (filled by enrich_articles)
                    "importance": "interesting",
                    "category":   "Other",
                    "one_liner":  "",
                    "ai_summary": "",
                })
                count += 1

        except Exception as e:
            print(f"  ⚠ Failed to fetch '{source}': {e}")
            failed_feeds.append(source)

    if failed_feeds:
        print(f"  ⚠ {len(failed_feeds)}/{len(RSS_FEEDS)} feeds failed: "
              f"{', '.join(failed_feeds)}")

    # Fuzzy dedup
    seen_keys = set()
    unique    = []
    for a in articles:
        key = title_key(a["title"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(a)

    # Most recent first
    unique.sort(
        key=lambda a: a["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return unique


# ─── GitHub Trending Repos ────────────────────────────────────────────────────

def fetch_trending_repos(config: dict) -> list[dict]:
    token = config.get("github_token", "")
    headers = {
        "User-Agent": "AI-Daily-Digest/2.0",
        "Accept":     "application/vnd.github.v3+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("  ⚠ No GitHub token — using unauthenticated (10 req/hr limit)")

    repos    = []
    seen     = set()
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
            print(f"  ⚠ GitHub rate limit. Add 'github_token' to config.json.")
            break
        if resp.status_code != 200:
            print(f"  ⚠ GitHub API {resp.status_code} for '{query}'")
            continue

        for item in resp.json().get("items", []):
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
        time.sleep(0.5)

    repos.sort(key=lambda r: r["stars"], reverse=True)
    return repos[:MAX_REPOS]


# ─── Digest Generation ────────────────────────────────────────────────────────

def generate_digest(news: list[dict], repos: list[dict]) -> str:
    today    = datetime.now(IST)
    date_str = today.strftime("%B %d, %Y")
    day_str  = today.strftime("%A")

    # Check if AI enrichment was applied (has a one_liner)
    ai_enabled = any(a.get("one_liner") for a in news)

    lines = [
        f"# 🤖 AI Daily Digest — {day_str}, {date_str}",
        "",
        (f"> 🧠 AI-powered digest · Generated at {today.strftime('%I:%M %p IST')}"
         if ai_enabled else
         f"> Auto-generated at {today.strftime('%I:%M %p IST')}"),
        "",
        "---",
        "",
    ]

    # ── News section ──────────────────────────────────────────────────────────
    lines.append("## 📰 Today's AI News")
    lines.append("")

    if news and ai_enabled:
        # Group by importance for smarter layout
        groups = {
            "breaking":    [a for a in news if a.get("importance") == "breaking"],
            "important":   [a for a in news if a.get("importance") == "important"],
            "interesting": [a for a in news if a.get("importance") == "interesting"],
        }

        article_num = 1
        for level, label in [("breaking", "🔥 Breaking News"),
                              ("important", "📌 Important"),
                              ("interesting", "💡 Also Worth Reading")]:
            group = groups[level]
            if not group:
                continue

            lines.append(f"### {label}")
            lines.append("")

            for article in group:
                cat      = article.get("category", "Other")
                cat_emo  = CATEGORY_EMOJI.get(cat, "📎")
                one_liner = article.get("one_liner", "")
                ai_sum    = article.get("ai_summary", article["summary"])

                lines.append(f"#### {article_num}. {article['title']}")
                lines.append(
                    f"`{cat_emo} {cat}` &nbsp;·&nbsp; "
                    f"**{article['source']}** &nbsp;·&nbsp; "
                    f"{article['date']}")
                lines.append("")

                if one_liner:
                    lines.append(f"**{one_liner}**")
                    lines.append("")

                if ai_sum:
                    lines.append(f"> {ai_sum}")
                    lines.append("")

                lines.append(f"🔗 [Read full article]({article['link']})")
                lines.append("")
                article_num += 1

    elif news:
        # Fallback — no AI enrichment, plain format
        for i, article in enumerate(news, 1):
            lines.append(f"### {i}. {article['title']}")
            lines.append(
                f"**{article['source']}** · {article['date']}")
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

    # ── Repos section ─────────────────────────────────────────────────────────
    lines.append("## 🚀 Trending AI & Agent Repos This Week")
    lines.append("")
    lines.append("New open-source projects sorted by GitHub stars (past 7 days):")
    lines.append("")

    if repos:
        lines.append("| # | Repository | ⭐ Stars | Language | What it does |")
        lines.append("|---|-----------|:-------:|----------|--------------|")
        for i, repo in enumerate(repos, 1):
            desc = repo["description"].replace("|", "\\|")
            lines.append(
                f"| {i} | [{repo['name']}]({repo['url']}) | "
                f"**{repo['stars']:,}** | `{repo['language']}` | {desc} |"
            )
        lines.append("")
    else:
        lines.append("_No trending repos found today._")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"_🤖 [AI Daily Digest](https://github.com/Piyush0053/ai-daily-digest) · "
        f"Updated: {today.strftime('%Y-%m-%d %H:%M IST')} · "
        f"{'AI-enriched via NVIDIA NIM' if ai_enabled else 'Raw RSS'}_"
    )
    lines.append("")

    return "\n".join(lines)


# ─── README ───────────────────────────────────────────────────────────────────

def generate_readme(digest_dir: Path) -> str:
    all_digests = sorted(digest_dir.glob("*.md"), reverse=True)
    latest_date = all_digests[0].stem if all_digests else "N/A"

    lines = [
        "# 🤖 AI Daily Digest",
        "",
        "Automated daily tracker of the **latest AI news** and **trending open-source "
        "AI/agent repositories** — powered by NVIDIA NIM.",
        "",
        "Every day this repo auto-updates with:",
        "- 📰 AI news from MIT Tech Review, The Verge, VentureBeat, Ars Technica, TechCrunch",
        "- 🧠 AI-written summaries, importance scores & category tags (via NVIDIA NIM)",
        "- 🚀 Trending AI/agent GitHub repos sorted by stars (past 7 days)",
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
    for d in all_digests[:60]:
        lines.append(f"| {d.stem} | [View](digests/{d.name}) |")

    lines += [
        "",
        "---",
        "_Updated automatically every day. Powered by NVIDIA NIM + GitHub Actions._",
        "",
    ]
    return "\n".join(lines)


# ─── Debug Logger ─────────────────────────────────────────────────────────────

def save_debug_log(news: list[dict], repos: list[dict], project_dir: Path):
    debug_dir = project_dir / "debug"
    debug_dir.mkdir(exist_ok=True)
    today     = datetime.now(IST).strftime("%Y-%m-%d")

    # Remove non-serializable datetime objects
    news_clean = [{k: v for k, v in a.items() if k != "pub_dt"} for a in news]

    payload = {
        "date":            today,
        "generated_at":    datetime.now(IST).isoformat(),
        "ai_enriched":     any(a.get("one_liner") for a in news),
        "articles_count":  len(news),
        "repos_count":     len(repos),
        "articles":        news_clean,
        "repos":           repos,
    }
    log_path = debug_dir / f"{today}.json"
    log_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"  ✓ Debug log → debug/{today}.json")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    project_dir = Path(__file__).parent
    digest_dir  = project_dir / "digests"
    digest_dir.mkdir(exist_ok=True)

    config   = load_config()
    today    = datetime.now(IST)
    filename = today.strftime("%Y-%m-%d") + ".md"
    filepath = digest_dir / filename

    if filepath.exists():
        print(f"✅ Digest for {today.strftime('%Y-%m-%d')} already exists. Skipping.")
        return

    print(f"📡 AI Daily Digest — {today.strftime('%B %d, %Y')}")
    print("=" * 50)

    # Step 1: Fetch news
    print("\n[1/4] Fetching RSS feeds...")
    news = fetch_rss_news()
    print(f"  ✓ Found {len(news)} articles")

    if len(news) < MIN_ARTICLES_TO_RUN:
        print(f"  ⚠ Too few articles ({len(news)} < {MIN_ARTICLES_TO_RUN}). Aborting.")
        sys.exit(2)

    # Step 2: AI enrichment via NVIDIA NIM
    print("\n[2/4] AI enrichment via NVIDIA NIM...")
    news = enrich_articles(news, config)

    # Step 3: Fetch GitHub repos
    print("\n[3/4] Searching GitHub trending repos...")
    repos = fetch_trending_repos(config)
    print(f"  ✓ Found {len(repos)} repos")

    # Step 4: Generate and save
    print("\n[4/4] Generating digest...")
    digest = generate_digest(news, repos)

    filepath.write_text(digest, encoding="utf-8")
    print(f"  ✓ Saved → digests/{filename}")

    save_debug_log(news, repos, project_dir)

    readme_path = project_dir / "README.md"
    readme_path.write_text(generate_readme(digest_dir), encoding="utf-8")
    print(f"  ✓ Updated README.md")

    print("\n✅ Done!")


if __name__ == "__main__":
    main()

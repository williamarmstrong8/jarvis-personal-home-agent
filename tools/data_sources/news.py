"""News via RSS feeds — no API key needed."""
import re
import feedparser


def get_news(sources: list, interests: list, max_per_source: int = 5) -> list:
    keywords = [i.lower() for i in interests]
    articles = []

    for source in sources:
        try:
            feed  = feedparser.parse(source["url"])
            count = 0
            for entry in feed.entries:
                if count >= max_per_source:
                    break
                title   = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                summary = summary[:300] + "..." if len(summary) > 300 else summary

                text  = (title + " " + summary).lower()
                score = sum(1 for kw in keywords if kw in text)

                articles.append({
                    "source":          source["name"],
                    "category":        source["category"],
                    "title":           title,
                    "summary":         summary,
                    "relevance_score": score,
                    "url":             entry.get("link", ""),
                })
                count += 1
        except Exception as exc:
            print(f"[NEWS] Failed {source['name']}: {exc}")

    articles.sort(key=lambda x: x["relevance_score"], reverse=True)
    return articles[:10]


def format_news_for_prompt(articles: list) -> str:
    if not articles:
        return "No news articles available."
    lines = []
    for i, a in enumerate(articles, 1):
        star = "★ " if a["relevance_score"] > 0 else ""
        lines.append(
            f"{i}. [{a['source']}] {star}{a['title']}\n"
            f"   {a['summary']}"
        )
    return "\n\n".join(lines)

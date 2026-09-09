"""
J.A.R.V.I.S. — Web Search via Tavily
Provides real-time web search results for current events, facts, and research.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

GREEN = "\033[92m"
RED   = "\033[91m"
RESET = "\033[0m"

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL     = "https://api.tavily.com/search"


def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web via Tavily and return a clean summary of results.
    Returns formatted text ready for Claude to summarize for voice output.
    """
    if not TAVILY_API_KEY or TAVILY_API_KEY == "your_tavily_key_here":
        return "Web search is not configured, sir. Add a TAVILY_API_KEY to the .env file."

    try:
        payload = {
            "api_key":              TAVILY_API_KEY,
            "query":                query,
            "search_depth":         "basic",
            "max_results":          max_results,
            "include_answer":       True,   # Tavily's own AI summary
            "include_raw_content":  False,
        }

        with httpx.Client(timeout=10) as client:
            resp = client.post(TAVILY_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()

        lines = []

        # Tavily's direct answer (usually a 1-2 sentence summary)
        answer = data.get("answer", "").strip()
        if answer:
            lines.append(f"Summary: {answer}")

        # Individual results
        results = data.get("results", [])
        if results:
            lines.append(f"\nTop {len(results)} results for '{query}':\n")
            for i, r in enumerate(results, 1):
                title   = r.get("title", "")
                url     = r.get("url", "")
                snippet = r.get("content", "")[:200].strip()
                lines.append(f"{i}. {title}\n   {snippet}\n   Source: {url}")

        if not lines:
            return f"No results found for '{query}', sir."

        print(f"{GREEN}[SEARCH] '{query}' → {len(results)} results{RESET}", flush=True)
        return "\n".join(lines)

    except httpx.TimeoutException:
        print(f"{RED}[SEARCH] Timeout searching for '{query}'{RESET}", flush=True)
        return f"Web search timed out for '{query}', sir. Try again in a moment."
    except Exception as exc:
        print(f"{RED}[SEARCH] Error: {exc}{RESET}", flush=True)
        return f"Web search failed, sir: {exc}"

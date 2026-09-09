"""
J.A.R.V.I.S. — Notion Integration
"""

import os

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

GREEN  = "\033[92m"
RED    = "\033[91m"
RESET  = "\033[0m"

_notion: Client | None = None


def _client() -> Client:
    global _notion
    if _notion is None:
        api_key = os.environ.get("NOTION_API_KEY", "")
        if not api_key:
            raise ValueError("NOTION_API_KEY not set in .env")
        _notion = Client(auth=api_key)
    return _notion


def create_page(title: str, content: str) -> str:
    try:
        n = _client()
        default_page = os.environ.get("NOTION_DEFAULT_PAGE_ID", "")

        parent = (
            {"type": "page_id", "page_id": default_page}
            if default_page
            else {"type": "workspace", "workspace": True}
        )

        page = n.pages.create(
            parent=parent,
            properties={
                "title": {
                    "title": [{"type": "text", "text": {"content": title}}]
                }
            },
            children=[
                {
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content}}]
                    },
                }
            ],
        )
        url = page.get("url", "")
        print(f"{GREEN}[NOTION] Created page: {title}{RESET}", flush=True)
        return f"Page '{title}' created, sir. URL: {url}"

    except Exception as exc:
        print(f"{RED}[NOTION] create_page error: {exc}{RESET}", flush=True)
        return f"Could not create Notion page, sir: {exc}"


def search(query: str) -> str:
    try:
        n       = _client()
        results = n.search(query=query, page_size=3)
        pages   = results.get("results", [])

        if not pages:
            return f"No Notion pages found for '{query}', sir."

        lines = []
        for p in pages:
            props = p.get("properties", {})
            title_prop = props.get("title") or props.get("Name") or {}
            title_list = title_prop.get("title", [])
            name = title_list[0]["plain_text"] if title_list else "Untitled"
            url  = p.get("url", "")
            lines.append(f"• {name}: {url}")

        return "Found the following pages, sir:\n" + "\n".join(lines)

    except Exception as exc:
        print(f"{RED}[NOTION] search error: {exc}{RESET}", flush=True)
        return f"Notion search failed, sir: {exc}"


def append_to_page(page_id: str, content: str) -> str:
    try:
        _client().blocks.children.append(
            block_id=page_id,
            children=[
                {
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content}}]
                    },
                }
            ],
        )
        print(f"{GREEN}[NOTION] Appended to page {page_id}{RESET}", flush=True)
        return f"Content appended to the page, sir."

    except Exception as exc:
        print(f"{RED}[NOTION] append error: {exc}{RESET}", flush=True)
        return f"Could not append to Notion page, sir: {exc}"

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

_ID_PREFIX = re.compile(r"^(https?://(export\.)?arxiv\.org/(abs|pdf)/|arxiv:)", re.I)


def normalize_id(raw: str) -> str:
    """'https://arxiv.org/abs/2401.01234v2', 'arXiv:2401.01234', '2401.01234.pdf' -> '2401.01234v2' / '2401.01234'."""
    s = _ID_PREFIX.sub("", raw.strip())
    return re.sub(r"\.pdf$", "", s, flags=re.I)


def _text(el: Optional[ET.Element]) -> str:
    # arXiv wraps titles/abstracts with newlines and double spaces
    return " ".join((el.text or "").split()) if el is not None else ""


class ArxivClient:
    """
    HTTP layer for the arXiv API.

    - Enforces arXiv's rule of ~1 request per 3 seconds (shared across all calls)
    - Retries on 429/503
    - Parses the Atom feed into plain dicts
    """

    BASE_URL = "https://export.arxiv.org/api/query"
    MIN_INTERVAL = 3.0

    def __init__(self):
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def _throttled_get(self, params: Dict[str, Any]) -> httpx.Response:
        async with self._lock:
            headers = {"User-Agent": "arxiv-mcp-server/1.0"}
            for attempt in range(3):
                wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)

                async with httpx.AsyncClient(follow_redirects=True) as client:
                    response = await client.get(
                        self.BASE_URL, params=params, headers=headers, timeout=20.0
                    )
                self._last_request = time.monotonic()

                if response.status_code in (429, 503) and attempt < 2:
                    delay = float(response.headers.get("Retry-After", 5))
                    logger.warning("arXiv returned %s, retrying in %ss", response.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                return response
            return response

    async def query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("ARXIV QUERY: %s", params)
        try:
            response = await self._throttled_get(params)
        except httpx.TimeoutException:
            return {"error": "TIMEOUT", "detail": "arXiv took too long to respond"}
        except Exception as e:
            logger.exception("Request failed")
            return {"error": "REQUEST_FAILED", "detail": str(e)}

        if response.status_code >= 400:
            return {
                "error": "API_ERROR",
                "status_code": response.status_code,
                "detail": response.text[:500],
            }

        try:
            return self._parse_feed(response.text)
        except ET.ParseError as e:
            return {"error": "PARSE_ERROR", "detail": str(e)}

    # ------------------------------------------------------------------
    def _parse_feed(self, xml_text: str) -> Dict[str, Any]:
        root = ET.fromstring(xml_text)
        total = root.findtext("opensearch:totalResults", default="0", namespaces=NS)
        start = root.findtext("opensearch:startIndex", default="0", namespaces=NS)

        papers: List[Dict[str, Any]] = []
        for entry in root.findall("atom:entry", NS):
            paper = self._parse_entry(entry)
            # arXiv reports malformed queries as a single entry titled "Error"
            if paper["title"] == "Error" and "/api/errors" in paper["id"]:
                return {"error": "ARXIV_ERROR", "detail": paper["abstract"]}
            papers.append(paper)

        return {
            "total_results": int(total),
            "start_index": int(start),
            "returned": len(papers),
            "papers": papers,
        }

    def _parse_entry(self, entry: ET.Element) -> Dict[str, Any]:
        abs_url = _text(entry.find("atom:id", NS))
        pdf_url = None
        for link in entry.findall("atom:link", NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")

        primary = entry.find("arxiv:primary_category", NS)
        return {
            "id": normalize_id(abs_url),
            "title": _text(entry.find("atom:title", NS)),
            "authors": [_text(a.find("atom:name", NS)) for a in entry.findall("atom:author", NS)],
            "abstract": _text(entry.find("atom:summary", NS)),
            "published": _text(entry.find("atom:published", NS)),
            "updated": _text(entry.find("atom:updated", NS)),
            "primary_category": primary.get("term") if primary is not None else None,
            "categories": [c.get("term") for c in entry.findall("atom:category", NS)],
            "abs_url": abs_url,
            "pdf_url": pdf_url,
            "comment": _text(entry.find("arxiv:comment", NS)) or None,
            "journal_ref": _text(entry.find("arxiv:journal_ref", NS)) or None,
            "doi": _text(entry.find("arxiv:doi", NS)) or None,
        }

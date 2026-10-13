"""
Static tool definitions for the arXiv MCP server.

arXiv has no OpenAPI spec to introspect, so tools are declared by hand and each
one has a builder that turns validated arguments into arXiv API query params.
"""
import re
from datetime import datetime
from typing import Any, Callable, Dict, List

from arxiv_client import normalize_id

MAX_RESULTS_CAP = 50

SORT_BY = ["relevance", "lastUpdatedDate", "submittedDate"]
SORT_ORDER = ["ascending", "descending"]


def _terms(field: str, text: str) -> str:
    """'graph neural' -> 'ti:graph AND ti:neural'. Quoted input is kept as a phrase."""
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 2:
        return f"{field}:{text}"
    words = re.findall(r"[\w\-\.]+", text)
    return " AND ".join(f"{field}:{w}" for w in words)


def _date(s: str, end: bool) -> str:
    d = datetime.strptime(s, "%Y-%m-%d")
    return d.strftime("%Y%m%d") + ("2359" if end else "0000")


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def build_search(args: Dict[str, Any]) -> Dict[str, Any]:
    parts: List[str] = []
    if args.get("query"):
        parts.append(f"({_terms('all', args['query'])})")
    if args.get("title"):
        parts.append(f"({_terms('ti', args['title'])})")
    if args.get("abstract"):
        parts.append(f"({_terms('abs', args['abstract'])})")
    if args.get("author"):
        parts.append(f'au:"{args["author"].strip()}"')
    if args.get("category"):
        parts.append(f"cat:{args['category'].strip()}")
    if args.get("date_from") or args.get("date_to"):
        lo = _date(args["date_from"], False) if args.get("date_from") else "000001010000"
        hi = _date(args["date_to"], True) if args.get("date_to") else "999912312359"
        parts.append(f"submittedDate:[{lo} TO {hi}]")

    if not parts:
        raise ValueError("Provide at least one of: query, title, abstract, author, category, date_from, date_to")

    sort_by = args.get("sort_by", "relevance")
    sort_order = args.get("sort_order", "descending")
    if sort_by not in SORT_BY or sort_order not in SORT_ORDER:
        raise ValueError(f"sort_by must be one of {SORT_BY}; sort_order one of {SORT_ORDER}")

    return {
        "search_query": " AND ".join(parts),
        "start": _clamp(args.get("start"), 0, 0, 10**6),
        "max_results": _clamp(args.get("max_results"), 10, 1, MAX_RESULTS_CAP),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }


def build_get_paper(args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    return {"id_list": normalize_id(args["arxiv_id"]), "max_results": 1}


def build_get_papers(args: Dict[str, Any]) -> Dict[str, Any]:
    ids = [normalize_id(i) for i in args.get("arxiv_ids", []) if i]
    if not ids:
        raise ValueError("arxiv_ids must be a non-empty list")
    ids = ids[:MAX_RESULTS_CAP]
    return {"id_list": ",".join(ids), "max_results": len(ids)}


def build_latest(args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("category"):
        raise ValueError("category is required (e.g. cs.AI)")
    return {
        "search_query": f"cat:{args['category'].strip()}",
        "start": 0,
        "max_results": _clamp(args.get("max_results"), 10, 1, MAX_RESULTS_CAP),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_papers",
        "description": (
            "Search arXiv papers. Combine any of the filters (they are ANDed together). "
            "Multi-word text matches all words; wrap in double quotes for an exact phrase."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text searched across all fields"},
                "title": {"type": "string", "description": "Words that must appear in the title"},
                "abstract": {"type": "string", "description": "Words that must appear in the abstract"},
                "author": {"type": "string", "description": "Author name, e.g. 'Yann LeCun'"},
                "category": {"type": "string", "description": "arXiv category, e.g. cs.LG, cs.AI, stat.ML, hep-th"},
                "date_from": {"type": "string", "description": "Earliest submission date, YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "Latest submission date, YYYY-MM-DD"},
                "max_results": {"type": "integer", "description": f"1-{MAX_RESULTS_CAP}, default 10"},
                "start": {"type": "integer", "description": "Result offset for pagination, default 0"},
                "sort_by": {"type": "string", "enum": SORT_BY, "description": "Default relevance"},
                "sort_order": {"type": "string", "enum": SORT_ORDER, "description": "Default descending"},
            },
            "required": [],
        },
        "builder": build_search,
    },
    {
        "name": "get_paper",
        "description": "Fetch metadata and abstract for one paper by arXiv ID (e.g. 1706.03762, 2401.01234v2, or an arxiv.org URL).",
        "inputSchema": {
            "type": "object",
            "properties": {"arxiv_id": {"type": "string", "description": "arXiv ID or URL"}},
            "required": ["arxiv_id"],
        },
        "builder": build_get_paper,
    },
    {
        "name": "get_papers",
        "description": f"Fetch metadata for several papers at once by arXiv ID (max {MAX_RESULTS_CAP}).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_ids": {"type": "array", "items": {"type": "string"}, "description": "List of arXiv IDs"}
            },
            "required": ["arxiv_ids"],
        },
        "builder": build_get_papers,
    },
    {
        "name": "get_latest_in_category",
        "description": "List the most recently submitted papers in an arXiv category (e.g. cs.CL).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "arXiv category, e.g. cs.CL"},
                "max_results": {"type": "integer", "description": f"1-{MAX_RESULTS_CAP}, default 10"},
            },
            "required": ["category"],
        },
        "builder": build_latest,
    },
]

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS}

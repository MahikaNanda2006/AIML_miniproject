"""
Tool definitions for the arXiv MCP server.

arXiv has no OpenAPI spec to introspect, so tools are declared by hand. Each tool has:
  - name / description / inputSchema  -> what the model sees
  - handler(client, args)             -> async function that does the work and returns a dict
Handlers raise ValueError for bad arguments; the server turns that into an INVALID_ARGUMENTS error.
"""
import asyncio
import os
import re
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List

from arxiv_client import ArxivClient, normalize_id, strip_version
from categories import CATEGORIES
from formatters import to_bibtex
from pdf_text import extract_text

MAX_RESULTS_CAP = 50
SORT_BY = ["relevance", "lastUpdatedDate", "submittedDate"]
SORT_ORDER = ["ascending", "descending"]

DOWNLOAD_DIR = os.environ.get("ARXIV_DOWNLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "downloads"
)

# Small in-memory cache so paging through a paper's text doesn't re-download the PDF
_TEXT_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_TEXT_CACHE_SIZE = 8

_STOP = {
    "with", "from", "that", "this", "using", "towards", "toward", "based", "into", "over", "under",
    "need", "your", "what", "when", "have", "their", "than", "via", "about", "between", "through", "approach", "novel", "new", "study",
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _terms(field: str, text: str) -> str:
    """'graph neural' -> 'ti:graph AND ti:neural'. Quoted input is kept as a phrase."""
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 2:
        return f"{field}:{text}"
    words = re.findall(r"[\w\-\.]+", text)
    return " AND ".join(f"{field}:{w}" for w in words)


def _date(s: str, end: bool) -> str:
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date '{s}', expected YYYY-MM-DD")
    return d.strftime("%Y%m%d") + ("2359" if end else "0000")


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _keywords(title: str, n: int = 6) -> List[str]:
    seen: List[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", title.lower()):
        if w not in _STOP and w not in seen:
            seen.append(w)
    return seen[:n]


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


async def _get_many(client: ArxivClient, raw_ids: List[str]) -> Dict[str, Any]:
    ids = [normalize_id(i) for i in raw_ids if i and i.strip()][:MAX_RESULTS_CAP]
    if not ids:
        raise ValueError("arxiv_ids must be a non-empty list")
    return await client.query({"id_list": ",".join(ids), "max_results": len(ids)})


async def _load_text(client: ArxivClient, arxiv_id: str) -> Dict[str, Any]:
    key = normalize_id(arxiv_id)
    if key in _TEXT_CACHE:
        _TEXT_CACHE.move_to_end(key)
        return _TEXT_CACHE[key]

    pdf = await client.fetch_pdf(key)
    if "error" in pdf:
        return pdf
    try:
        extracted = await asyncio.to_thread(extract_text, pdf["content"])
    except Exception as e:
        return {"error": "PDF_PARSE_FAILED", "detail": str(e)}

    _TEXT_CACHE[key] = extracted
    while len(_TEXT_CACHE) > _TEXT_CACHE_SIZE:
        _TEXT_CACHE.popitem(last=False)
    return extracted


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
async def handle_search(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    return await client.query(build_search(args))


async def handle_get_paper(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    result = await client.get_paper(args["arxiv_id"])
    return result["paper"] if "paper" in result else result


async def handle_get_papers(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    return await _get_many(client, args.get("arxiv_ids") or [])


async def handle_latest(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("category"):
        raise ValueError("category is required (e.g. cs.AI)")
    return await client.query({
        "search_query": f"cat:{args['category'].strip()}",
        "start": 0,
        "max_results": _clamp(args.get("max_results"), 10, 1, MAX_RESULTS_CAP),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })


async def handle_author(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("author"):
        raise ValueError("author is required")
    search_args = {
        "author": args["author"],
        "category": args.get("category"),
        "max_results": args.get("max_results"),
        "start": args.get("start"),
        "sort_by": "submittedDate",
        "sort_order": args.get("sort_order", "descending"),
    }
    return await client.query(build_search(search_args))


async def handle_related(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    src = await client.get_paper(args["arxiv_id"])
    if "error" in src:
        return src
    paper = src["paper"]

    kws = _keywords(paper["title"])
    if not kws:
        return {"error": "NO_KEYWORDS", "detail": "Could not derive search keywords from the title"}

    query = "(" + " OR ".join(f"all:{k}" for k in kws) + ")"
    if args.get("same_category", True) and paper.get("primary_category"):
        query += f" AND cat:{paper['primary_category']}"

    n = _clamp(args.get("max_results"), 5, 1, 20)
    result = await client.query({
        "search_query": query, "start": 0, "max_results": n + 1,
        "sortBy": "relevance", "sortOrder": "descending",
    })
    if "error" in result:
        return result

    me = strip_version(paper["id"])
    related = [p for p in result["papers"] if strip_version(p["id"]) != me][:n]
    return {
        "source": {"id": paper["id"], "title": paper["title"], "primary_category": paper.get("primary_category")},
        "query_used": query,
        "returned": len(related),
        "papers": related,
    }


async def handle_fulltext(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    extracted = await _load_text(client, args["arxiv_id"])
    if "error" in extracted:
        return extracted

    text = extracted["text"]
    total = len(text)
    start = _clamp(args.get("start_char"), 0, 0, 10**9)
    size = _clamp(args.get("max_chars"), 20000, 1000, 50000)
    end = min(total, start + size)

    result = {
        "id": normalize_id(args["arxiv_id"]),
        "pages": extracted["pages"],
        "total_chars": total,
        "start_char": start,
        "end_char": end,
        "truncated": end < total,
        "next_start_char": end if end < total else None,
        "text": text[start:end],
    }
    if total == 0:
        result["note"] = "No extractable text (the PDF may be scanned images)."
    return result


async def handle_bibtex(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ids = args.get("arxiv_ids") or ([args["arxiv_id"]] if args.get("arxiv_id") else [])
    result = await _get_many(client, ids)
    if "error" in result:
        return result
    if not result["papers"]:
        return {"error": "NOT_FOUND", "detail": "No papers found for the given ids"}
    return {
        "count": len(result["papers"]),
        "bibtex": "\n\n".join(to_bibtex(p) for p in result["papers"]),
    }


async def handle_download(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    key = normalize_id(args["arxiv_id"])
    pdf = await client.fetch_pdf(key)
    if "error" in pdf:
        return pdf

    filename = re.sub(r"[^\w\-.]", "_", key) + ".pdf"  # old-style ids contain '/'
    path = os.path.join(DOWNLOAD_DIR, filename)

    def _write() -> None:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(pdf["content"])

    await asyncio.to_thread(_write)
    return {"id": key, "saved_to": path, "size_bytes": len(pdf["content"])}


async def handle_categories(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    group = (args.get("group") or "").strip().lower()
    search = (args.get("search") or "").strip().lower()
    items = [
        {"id": cid, "name": name}
        for cid, name in CATEGORIES.items()
        if (not group or cid.lower().startswith(group))
        and (not search or search in name.lower() or search in cid.lower())
    ]
    return {"count": len(items), "categories": items}


_ARXIV_REF = re.compile(r"arxiv(?:\.org/abs/|:\s*)(\d{4}\.\d{4,5})", re.I)


def _compact(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: p.get(k) for k in ("id", "title", "authors", "published", "primary_category", "abs_url")}


def _split_name(name: str):
    parts = name.split()
    last = parts[-1] if parts else name
    return last, [x[0] for x in parts[:-1] if x]


def _authors(p: Dict[str, Any], style: str) -> str:
    names = p["authors"]
    if len(names) > 6:
        names = names[:1]
        et_al = True
    else:
        et_al = False
    out = []
    for n in names:
        last, ini = _split_name(n)
        if style == "apa":
            out.append(f"{last}, {' '.join(i + '.' for i in ini)}".strip().rstrip(","))
        else:  # ieee / markdown
            out.append(f"{' '.join(i + '.' for i in ini)} {last}".strip())
    if et_al:
        return out[0] + " et al."
    if len(out) > 1:
        if len(out) == 2:
            sep = ", & " if style == "apa" else " and "
        else:
            sep = ", & " if style == "apa" else ", and "
        return ", ".join(out[:-1]) + sep + out[-1]
    return out[0] if out else ""


async def handle_by_title(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    title = (args.get("title") or "").replace('"', "").strip()
    if not title:
        raise ValueError("title is required")
    return await client.query({
        "search_query": f'ti:"{title}"', "start": 0,
        "max_results": _clamp(args.get("max_results"), 3, 1, 10),
        "sortBy": "relevance", "sortOrder": "descending",
    })


async def handle_compare(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    ids = args.get("arxiv_ids") or []
    if len(ids) < 2:
        raise ValueError("Provide at least two arxiv_ids")
    result = await _get_many(client, ids)
    if "error" in result:
        return result
    papers = result["papers"]
    if len(papers) < 2:
        return {"error": "NOT_FOUND", "detail": "Fewer than two of the given papers were found"}

    shared_authors = sorted(set.intersection(*[set(p["authors"]) for p in papers]))
    shared_cats = sorted(set.intersection(*[set(p["categories"]) for p in papers]))
    ordered = sorted(papers, key=lambda p: p["published"])
    return {
        "papers": [
            {k: p.get(k) for k in ("id", "title", "authors", "published", "primary_category",
                                   "categories", "abstract", "journal_ref", "doi")}
            for p in papers
        ],
        "shared_authors": shared_authors,
        "shared_categories": shared_cats,
        "chronological_order": [p["id"] for p in ordered],
        "years_between_first_and_last": int(ordered[-1]["published"][:4]) - int(ordered[0]["published"][:4]),
    }


async def handle_analyze_author(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    author = (args.get("author") or "").strip()
    if not author:
        raise ValueError("author is required")
    result = await client.query(build_search({
        "author": author, "max_results": MAX_RESULTS_CAP,
        "sort_by": "submittedDate", "sort_order": "descending",
    }))
    if "error" in result:
        return result
    papers = result["papers"]
    if not papers:
        return {"error": "NOT_FOUND", "detail": f"No papers found for author '{author}'"}

    me = author.lower()
    coauthors = Counter(a for p in papers for a in p["authors"] if a.lower() != me)
    cats = Counter(c for p in papers for c in p["categories"])
    years = Counter(p["published"][:4] for p in papers)
    return {
        "author": author,
        "total_papers_on_arxiv": result["total_results"],
        "papers_analyzed": len(papers),
        "note": "Statistics are based on the most recent papers analyzed (max 50).",
        "top_coauthors": [{"name": n, "papers": c} for n, c in coauthors.most_common(10)],
        "top_categories": [{"category": c, "papers": n} for c, n in cats.most_common(8)],
        "papers_per_year": dict(sorted(years.items())),
        "most_recent": [_compact(p) for p in papers[:5]],
    }


async def handle_activity(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    category = (args.get("category") or "").strip()
    if not category:
        raise ValueError("category is required (e.g. cs.AI)")
    days = _clamp(args.get("days"), 7, 1, 30)
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    result = await client.query({
        "search_query": f"cat:{category} AND submittedDate:[{since:%Y%m%d}0000 TO {today:%Y%m%d}2359]",
        "start": 0, "max_results": MAX_RESULTS_CAP,
        "sortBy": "submittedDate", "sortOrder": "descending",
    })
    if "error" in result:
        return result
    papers = result["papers"]
    per_day = Counter(p["published"][:10] for p in papers)
    return {
        "category": category,
        "window_days": days,
        "total_submitted_in_window": result["total_results"],
        "per_day_in_sample": dict(sorted(per_day.items())),
        "sample_size": len(papers),
        "latest": [_compact(p) for p in papers[:10]],
    }


async def handle_search_in_paper(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    keyword = (args.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("keyword is required")
    extracted = await _load_text(client, args["arxiv_id"])
    if "error" in extracted:
        return extracted

    text = extracted["text"]
    ctx = _clamp(args.get("context_chars"), 150, 50, 500)
    limit = _clamp(args.get("max_matches"), 10, 1, 30)
    matches, total = [], 0
    for m in re.finditer(re.escape(keyword), text, re.I):
        total += 1
        if len(matches) < limit:
            s, e = max(0, m.start() - ctx), min(len(text), m.end() + ctx)
            matches.append({"position": m.start(), "snippet": " ".join(text[s:e].split())})
    return {
        "id": normalize_id(args["arxiv_id"]), "keyword": keyword,
        "total_matches": total, "returned": len(matches), "matches": matches,
    }


async def handle_cited(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    if not args.get("arxiv_id"):
        raise ValueError("arxiv_id is required")
    extracted = await _load_text(client, args["arxiv_id"])
    if "error" in extracted:
        return extracted

    me = strip_version(normalize_id(args["arxiv_id"]))
    found: List[str] = []
    for m in _ARXIV_REF.finditer(extracted["text"]):
        i = m.group(1)
        if i != me and i not in found:
            found.append(i)

    if not found:
        return {"source": me, "ids_found": 0, "papers": [],
                "note": "No arXiv IDs found in the text; references may cite by title only (try find_paper_by_title)."}

    limit = _clamp(args.get("max_papers"), 15, 1, 30)
    result = await _get_many(client, found[:limit])
    if "error" in result:
        return result
    return {
        "source": me, "ids_found": len(found), "returned": len(result["papers"]),
        "papers": [_compact(p) for p in result["papers"]],
    }


async def handle_citations(client: ArxivClient, args: Dict[str, Any]) -> Dict[str, Any]:
    style = (args.get("style") or "apa").lower()
    if style not in ("apa", "ieee", "markdown"):
        raise ValueError("style must be one of: apa, ieee, markdown")
    result = await _get_many(client, args.get("arxiv_ids") or [])
    if "error" in result:
        return result

    out = []
    for p in result["papers"]:
        year, aid, url = p["published"][:4], strip_version(p["id"]), p["abs_url"]
        a = _authors(p, "apa" if style == "apa" else "ieee")
        if style == "apa":
            out.append(f"{a} ({year}). {p['title']}. arXiv:{aid}. {url}")
        elif style == "ieee":
            out.append(f'{a}, "{p["title"]}," arXiv:{aid}, {year}. [Online]. Available: {url}')
        else:
            out.append(f"- [{p['title']}]({url}) - {a}, {year}")
    return {"style": style, "count": len(out), "citations": out}


# ----------------------------------------------------------------------
# Tool declarations
# ----------------------------------------------------------------------
_ID_DESC = "arXiv ID (e.g. 1706.03762, 2401.01234v2) or an arxiv.org URL"

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
        "handler": handle_search,
    },
    {
        "name": "get_paper",
        "description": "Fetch metadata and abstract for one paper by arXiv ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"arxiv_id": {"type": "string", "description": _ID_DESC}},
            "required": ["arxiv_id"],
        },
        "handler": handle_get_paper,
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
        "handler": handle_get_papers,
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
        "handler": handle_latest,
    },
    {
        "name": "get_author_papers",
        "description": "List papers by an author, newest first by default. Optionally restrict to a category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "author": {"type": "string", "description": "Author name, e.g. 'Geoffrey Hinton'"},
                "category": {"type": "string", "description": "Optional arXiv category filter"},
                "max_results": {"type": "integer", "description": f"1-{MAX_RESULTS_CAP}, default 10"},
                "start": {"type": "integer", "description": "Result offset for pagination"},
                "sort_order": {"type": "string", "enum": SORT_ORDER, "description": "Default descending (newest first)"},
            },
            "required": ["author"],
        },
        "handler": handle_author,
    },
    {
        "name": "find_related_papers",
        "description": (
            "Find papers similar to a given paper. Extracts keywords from its title and searches for papers "
            "on the same topic (excluding the paper itself). Good for exploring a research area from one seed paper."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "description": _ID_DESC},
                "max_results": {"type": "integer", "description": "1-20, default 5"},
                "same_category": {"type": "boolean", "description": "Restrict to the seed paper's primary category (default true)"},
            },
            "required": ["arxiv_id"],
        },
        "handler": handle_related,
    },
    {
        "name": "get_paper_fulltext",
        "description": (
            "Download a paper's PDF and return its extracted text so it can be read, summarized or quoted. "
            "Long papers are returned in chunks: if 'truncated' is true, call again with start_char=next_start_char."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "description": _ID_DESC},
                "start_char": {"type": "integer", "description": "Character offset to start from, default 0"},
                "max_chars": {"type": "integer", "description": "Chunk size, 1000-50000, default 20000"},
            },
            "required": ["arxiv_id"],
        },
        "handler": handle_fulltext,
    },
    {
        "name": "export_bibtex",
        "description": "Generate BibTeX entries for one or more arXiv papers, ready to paste into a .bib file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_ids": {"type": "array", "items": {"type": "string"}, "description": "List of arXiv IDs"},
                "arxiv_id": {"type": "string", "description": "Single arXiv ID (alternative to arxiv_ids)"},
            },
            "required": [],
        },
        "handler": handle_bibtex,
    },
    {
        "name": "download_paper",
        "description": "Save a paper's PDF to the server machine's download folder and return the file path.",
        "inputSchema": {
            "type": "object",
            "properties": {"arxiv_id": {"type": "string", "description": _ID_DESC}},
            "required": ["arxiv_id"],
        },
        "handler": handle_download,
    },
    {
        "name": "list_categories",
        "description": "Browse arXiv subject categories (id and name). Use to find valid values for the 'category' filter.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "Prefix filter, e.g. 'cs', 'stat', 'math', 'eess'"},
                "search": {"type": "string", "description": "Substring to match in id or name, e.g. 'vision'"},
            },
            "required": [],
        },
        "handler": handle_categories,
    },
    {
        "name": "find_paper_by_title",
        "description": "Look up a paper by its title (exact phrase match) to get its arXiv ID. Useful for resolving citations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Paper title"},
                "max_results": {"type": "integer", "description": "1-10, default 3"},
            },
            "required": ["title"],
        },
        "handler": handle_by_title,
    },
    {
        "name": "compare_papers",
        "description": (
            "Compare two or more papers: side-by-side metadata and abstracts, shared authors and categories, "
            "and chronological order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_ids": {"type": "array", "items": {"type": "string"}, "description": "Two or more arXiv IDs"}
            },
            "required": ["arxiv_ids"],
        },
        "handler": handle_compare,
    },
    {
        "name": "analyze_author",
        "description": (
            "Profile an author from their recent arXiv papers: total paper count, top co-authors, "
            "main categories, and papers per year."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"author": {"type": "string", "description": "Author name, e.g. 'Yoshua Bengio'"}},
            "required": ["author"],
        },
        "handler": handle_analyze_author,
    },
    {
        "name": "category_activity",
        "description": "Show recent submission activity in a category: total papers in the last N days and the latest ones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "arXiv category, e.g. cs.LG"},
                "days": {"type": "integer", "description": "Window in days, 1-30, default 7"},
            },
            "required": ["category"],
        },
        "handler": handle_activity,
    },
    {
        "name": "search_in_paper",
        "description": "Find a keyword or phrase inside a paper's full text and return each match with surrounding context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "description": _ID_DESC},
                "keyword": {"type": "string", "description": "Word or phrase to find (case-insensitive)"},
                "max_matches": {"type": "integer", "description": "1-30, default 10"},
                "context_chars": {"type": "integer", "description": "Characters of context on each side, 50-500, default 150"},
            },
            "required": ["arxiv_id", "keyword"],
        },
        "handler": handle_search_in_paper,
    },
    {
        "name": "get_cited_arxiv_papers",
        "description": (
            "Extract the arXiv papers that a given paper cites (found via arXiv IDs in its reference list) "
            "and return their metadata. Lets you follow a paper's citation trail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_id": {"type": "string", "description": _ID_DESC},
                "max_papers": {"type": "integer", "description": "1-30, default 15"},
            },
            "required": ["arxiv_id"],
        },
        "handler": handle_cited,
    },
    {
        "name": "format_citations",
        "description": "Format papers as citations in APA, IEEE or Markdown-link style.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "arxiv_ids": {"type": "array", "items": {"type": "string"}, "description": "List of arXiv IDs"},
                "style": {"type": "string", "enum": ["apa", "ieee", "markdown"], "description": "Default apa"},
            },
            "required": ["arxiv_ids"],
        },
        "handler": handle_citations,
    },
]

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS}

"""
MCP prompts: reusable workflows the user can trigger from the client (in Claude Desktop they appear
in the attachment menu). Each one tells the model which tools to chain.
"""
from typing import Any, Callable, Dict, List


def _literature_review(a: Dict[str, str]) -> str:
    n = a.get("num_papers") or "10"
    cat = f" in category {a['category']}" if a.get("category") else ""
    return (
        f"Write a literature review on \"{a['topic']}\"{cat}.\n\n"
        f"1. Use search_papers to find about {n} relevant papers (run a couple of differently-worded searches; "
        "include both recent and highly foundational work).\n"
        "2. Use find_related_papers on the best one or two papers to discover anything you missed.\n"
        "3. Use get_paper_fulltext on the 2-3 most important papers to ground your claims in the actual methods and results.\n"
        "4. Write the review with: an overview of the area, the main approaches grouped by theme, a table of key papers "
        "(title, authors, year, one-line contribution), open problems, and a short reading order for a newcomer.\n"
        "5. Finish with export_bibtex for every paper you cited."
    )


def _explain_paper(a: Dict[str, str]) -> str:
    level = a.get("level") or "intermediate"
    return (
        f"Explain arXiv paper {a['arxiv_id']} at a {level} level.\n\n"
        "Use get_paper for the metadata and get_paper_fulltext for the content (fetch further chunks if truncated). "
        "Cover: the problem and why it matters, the key idea in plain language, how the method works, the main results, "
        "limitations, and what to read next (use find_related_papers)."
    )


def _daily_digest(a: Dict[str, str]) -> str:
    n = a.get("count") or "15"
    return (
        f"Give me a digest of the latest research in arXiv category {a['category']}.\n\n"
        f"Use get_latest_in_category with max_results={n}. Group the papers by theme, give each a one or two line "
        "plain-language summary with its arXiv ID, and flag the 2-3 that look most interesting and why."
    )


def _compare_papers(a: Dict[str, str]) -> str:
    return (
        f"Compare these arXiv papers: {a['arxiv_ids']}.\n\n"
        "Use get_papers for metadata and get_paper_fulltext for the details of each. Produce a comparison table "
        "(problem, method, datasets/experiments, key results, limitations) followed by a short analysis of how they "
        "differ, which one you would use in which situation, and any contradictions between them."
    )


PROMPTS: Dict[str, Dict[str, Any]] = {
    "literature_review": {
        "description": "Search, read and synthesize a literature review on a topic, with BibTeX.",
        "arguments": [
            {"name": "topic", "description": "Research topic", "required": True},
            {"name": "category", "description": "Optional arXiv category, e.g. cs.CL", "required": False},
            {"name": "num_papers", "description": "Approximate number of papers (default 10)", "required": False},
        ],
        "render": _literature_review,
    },
    "explain_paper": {
        "description": "Explain a paper at a chosen level using its full text.",
        "arguments": [
            {"name": "arxiv_id", "description": "arXiv ID or URL", "required": True},
            {"name": "level", "description": "beginner, intermediate or expert (default intermediate)", "required": False},
        ],
        "render": _explain_paper,
    },
    "daily_digest": {
        "description": "Summarize the newest papers in a category, grouped by theme.",
        "arguments": [
            {"name": "category", "description": "arXiv category, e.g. cs.AI", "required": True},
            {"name": "count", "description": "How many papers (default 15)", "required": False},
        ],
        "render": _daily_digest,
    },
    "compare_papers": {
        "description": "Side-by-side comparison of several papers.",
        "arguments": [
            {"name": "arxiv_ids", "description": "Comma-separated arXiv IDs", "required": True},
        ],
        "render": _compare_papers,
    },
}

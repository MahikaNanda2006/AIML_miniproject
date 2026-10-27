import re
import unicodedata
from typing import Any, Dict

from arxiv_client import strip_version

_STOP = {"a", "an", "the", "on", "of", "for", "and", "in", "to", "with", "via", "from", "by", "using"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def bibtex_key(p: Dict[str, Any]) -> str:
    last = _ascii(p["authors"][0].split()[-1]).lower() if p.get("authors") else "unknown"
    last = re.sub(r"[^a-z0-9]", "", last) or "unknown"
    year = (p.get("published") or "")[:4]
    word = next(
        (re.sub(r"[^a-z0-9]", "", _ascii(w).lower()) for w in p["title"].split()
         if re.sub(r"[^a-z0-9]", "", _ascii(w).lower()) not in _STOP | {""}),
        "paper",
    )
    return f"{last}{year}{word}"


def _esc(s: str) -> str:
    return s.replace("&", r"\&").replace("%", r"\%")


def to_bibtex(p: Dict[str, Any]) -> str:
    fields = [
        ("title", "{" + _esc(p["title"]) + "}"),
        ("author", " and ".join(p["authors"])),
        ("year", (p.get("published") or "")[:4]),
        ("eprint", strip_version(p["id"])),
        ("archivePrefix", "arXiv"),
        ("primaryClass", p.get("primary_category") or ""),
        ("url", p["abs_url"]),
    ]
    if p.get("doi"):
        fields.append(("doi", p["doi"]))
    if p.get("journal_ref"):
        fields.append(("note", _esc(p["journal_ref"])))
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
    return f"@misc{{{bibtex_key(p)},\n{body}\n}}"

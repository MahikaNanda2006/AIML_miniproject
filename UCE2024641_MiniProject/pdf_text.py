import io
import re
from typing import Any, Dict

from pypdf import PdfReader


def extract_text(data: bytes) -> Dict[str, Any]:
    """Extract plain text from PDF bytes. CPU-bound: call via asyncio.to_thread."""
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    text = "\n\n".join(pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return {"text": text.strip(), "pages": len(reader.pages)}

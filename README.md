# arXiv MCP Server

An MCP server that lets an AI assistant (like Claude Desktop) search arXiv, read papers, and build reading lists and bibliographies through the public arXiv API.

## What is MCP?

**Model Context Protocol (MCP)** is an open standard for connecting AI assistants to external tools and data. A program called an **MCP server** exposes:

- **Tools**: functions with a name, a description and a JSON input schema. The model decides when to call them.
- **Prompts**: reusable, parameterized workflows the user can trigger from the client.

An **MCP client** (Claude Desktop, for example) discovers these and connects the model to them.

```
Claude Desktop (client) <--- MCP (stdio or HTTP) ---> this server <--- HTTPS ---> arXiv (API + PDFs)
```

## Features

### Tools

| Tool | What it does |
|---|---|
| `search_papers` | Search by free text, title, abstract, author, category and date range (filters are ANDed). Pagination and sorting supported. |
| `get_paper` | Metadata and abstract for one paper by arXiv ID or URL. |
| `get_papers` | Metadata for up to 50 papers in one call. |
| `get_latest_in_category` | Newest submissions in a category such as `cs.CL`. |
| `get_author_papers` | An author's papers, newest first, optionally within a category. |
| `find_related_papers` | Takes a seed paper, derives keywords from its title, and finds similar papers (excluding itself). |
| `get_paper_fulltext` | Downloads the PDF and returns extracted text in chunks (use `next_start_char` to continue). |
| `export_bibtex` | BibTeX entries for one or more papers. |
| `download_paper` | Saves the PDF to a local folder and returns the path. |
| `list_categories` | Browse valid arXiv categories, filtered by group (`cs`, `stat`...) or keyword. |

### Prompts

| Prompt | What it does |
|---|---|
| `literature_review` | Searches, expands via related papers, reads key papers, and writes a structured review with BibTeX. |
| `explain_paper` | Explains a paper at beginner / intermediate / expert level using its full text. |
| `daily_digest` | Summarizes the newest papers in a category, grouped by theme. |
| `compare_papers` | Side-by-side comparison table and analysis of several papers. |

In Claude Desktop, prompts appear in the attachment (+) menu of a chat.

## Project structure

```
arxiv-mcp/
├── server.py          # MCP layer: tools, prompts, stdio and HTTP transports
├── tools.py           # Tool definitions and their handlers
├── prompts.py         # Prompt templates
├── arxiv_client.py    # HTTP client: rate limiting, retries, XML parsing, PDF download
├── pdf_text.py        # PDF -> plain text (pypdf)
├── formatters.py      # BibTeX generation
├── categories.py      # Static arXiv category taxonomy
├── requirements.txt
└── README.md
```

### What each file does

- **`server.py`**: creates the MCP `Server` and registers `list_tools`, `call_tool`, `list_prompts` and `get_prompt`. `call_tool` looks up the tool, runs its handler, and returns JSON; bad arguments become `INVALID_ARGUMENTS` and unexpected failures become `INTERNAL_ERROR` instead of crashing. It runs over **stdio** (default, used by Claude Desktop) or **HTTP** with `--port` (`http://localhost:<port>/mcp`). Logs go to stderr because stdout carries the protocol in stdio mode.
- **`tools.py`**: each tool is a dict with `name`, `description`, `inputSchema` and an async `handler(client, args)`. Handlers build queries, chain multiple calls (e.g. `find_related_papers` fetches the seed paper, then searches), and validate input. Also holds a small in-memory cache so paging through a paper's text doesn't re-download the PDF.
- **`prompts.py`**: the prompt definitions (arguments and the instruction text that tells the model which tools to chain).
- **`arxiv_client.py`**: talks to arXiv. Enforces one request per 3 seconds (arXiv's guideline) across API and PDF calls, retries on 429/503, parses the Atom XML into clean dicts, and downloads PDFs. Errors come back as `{"error": ..., "detail": ...}`.
- **`pdf_text.py`**: extracts and cleans text from PDF bytes.
- **`formatters.py`**: builds BibTeX entries and citation keys like `vaswani2017attention`.
- **`categories.py`**: category ids and names for `list_categories`.

## Setup

**Requirements:** Python 3.10 or newer.

```bash
cd arxiv-mcp
python -m venv .venv

# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

`requirements.txt` pins `mcp` to the 1.x series, which this code is written for.

## Running

```bash
python server.py               # stdio mode (waits silently for a client)
python server.py --port 8000   # HTTP mode at http://localhost:8000/mcp
```

Test interactively with the MCP Inspector (needs Node.js):

```bash
npx @modelcontextprotocol/inspector python server.py
```

Optional environment variable: `ARXIV_DOWNLOAD_DIR` sets where `download_paper` saves files (default: a `downloads/` folder next to `server.py`).

## Using it with Claude Desktop

1. Open Claude Desktop, then **Settings > Developer > Edit Config** to open `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add the server under `mcpServers`, using **absolute paths** and the Python from your virtual environment.

   macOS / Linux:
   ```json
   {
     "mcpServers": {
       "arxiv": {
         "command": "/absolute/path/to/arxiv-mcp/.venv/bin/python",
         "args": ["/absolute/path/to/arxiv-mcp/server.py"]
       }
     }
   }
   ```

   Windows (doubled backslashes):
   ```json
   {
     "mcpServers": {
       "arxiv": {
         "command": "C:\\path\\to\\arxiv-mcp\\.venv\\Scripts\\python.exe",
         "args": ["C:\\path\\to\\arxiv-mcp\\server.py"]
       }
     }
   }
   ```
3. Fully **quit and restart** Claude Desktop.
4. Open a new chat and try:
   - "Find recent papers on retrieval-augmented generation in cs.CL and summarize the top 3."
   - "Find papers related to 1706.03762, then give me BibTeX for them."
   - "Read arXiv 2005.14165 and explain it to me like I'm a beginner."
   - Or pick the `literature_review` prompt from the attachment menu.

If `mcpServers` already has other entries, add `"arxiv": { ... }` alongside them.

## Troubleshooting

- **Server missing or errored**: check logs (macOS: `~/Library/Logs/Claude/mcp-server-arxiv.log`; Windows: `%APPDATA%\Claude\logs\`). Usual causes: wrong path, JSON syntax error in the config, or a Python without the dependencies.
- **`ModuleNotFoundError`**: `command` is not pointing at the virtual environment's Python.
- **`AttributeError: 'Server' object has no attribute 'list_tools'`**: you have `mcp` 2.x installed; run `pip install -r requirements.txt` to get 1.x.
- **Slow responses**: expected; calls are spaced 3 seconds apart on purpose, and tools like `find_related_papers` make two requests.
- **`ARXIV_ERROR`**: arXiv rejected the query (bad category or malformed ID); see `detail`.
- **Empty `get_paper_fulltext` text**: the PDF is probably scanned images with no text layer.

## Notes

- Full text comes from the PDF, so equations and tables may extract imperfectly.
- Be considerate of arXiv: keep to one request every 3 seconds.
- API reference: https://info.arxiv.org/help/api/user-manual.html

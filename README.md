# arXiv MCP Server

An MCP server that lets an AI assistant (like Claude Desktop) search arXiv and fetch paper metadata and abstracts through the public arXiv API.

## What is MCP?

**Model Context Protocol (MCP)** is an open standard for connecting AI assistants to external tools and data. Instead of every app writing custom integrations, a program called an **MCP server** exposes a set of **tools** (functions with a name, a description, and a JSON input schema). An **MCP client** (Claude Desktop, for example) discovers those tools, and the model decides when to call them.

```
Claude Desktop (client)  <--- MCP (stdio or HTTP) --->  this server  <--- HTTPS --->  arXiv API
```

In this project the model can call tools like `search_papers`, and the server translates the call into an arXiv API request and returns clean JSON.

## Project structure

```
arxiv-mcp/
├── server.py          # MCP server: tool listing, tool calling, transports
├── tools.py           # Tool definitions + argument-to-query builders
├── arxiv_client.py    # HTTP client for arXiv: rate limiting, retries, XML parsing
├── requirements.txt   # Python dependencies
└── README.md
```

### `server.py` — the MCP layer
- Creates the MCP `Server` and registers two handlers:
  - `list_tools`: returns the tools defined in `tools.py` so the client knows what exists.
  - `call_tool`: looks up the requested tool, validates arguments with its builder, calls the arXiv client, and returns the result as JSON text.
- Provides two ways to run:
  - **stdio** (default): the client launches the script and talks over stdin/stdout. This is what Claude Desktop uses.
  - **HTTP** (`--port`): streamable HTTP at `http://localhost:<port>/mcp`, with CORS enabled.
- Logs go to stderr, because stdout is reserved for the MCP protocol in stdio mode.

### `tools.py` — what the model can do
- Holds the `TOOLS` list. Each tool has a `name`, `description`, `inputSchema`, and a `builder` function.
- Builders validate arguments and turn them into arXiv query parameters (e.g. `search_query`, `max_results`, `sortBy`). They raise `ValueError` on bad input, which the server returns as an `INVALID_ARGUMENTS` error.
- Available tools:

| Tool | Purpose |
|---|---|
| `search_papers` | Search by free text, title, abstract, author, category, and date range. Filters are ANDed. Supports `max_results` (1-50), `start`, `sort_by`, `sort_order`. |
| `get_paper` | Get one paper by arXiv ID or URL (e.g. `1706.03762`, `2401.01234v2`). |
| `get_papers` | Get up to 50 papers by a list of IDs. |
| `get_latest_in_category` | Newest submissions in a category such as `cs.CL`. |

Search tips: multi-word text matches all words; wrap in double quotes for an exact phrase (`"graph neural networks"`).

### `arxiv_client.py` — talking to arXiv
- Sends requests to `https://export.arxiv.org/api/query`.
- **Rate limiting**: enforces at most one request every 3 seconds (arXiv's guideline), shared across all tool calls.
- **Retries**: retries on HTTP 429/503, respecting `Retry-After`.
- **Parsing**: arXiv returns Atom XML. The client converts it to dicts with: `id`, `title`, `authors`, `abstract`, `published`, `updated`, `primary_category`, `categories`, `abs_url`, `pdf_url`, `comment`, `journal_ref`, `doi`, plus `total_results` and `start_index`.
- **Errors**: timeouts, HTTP errors, parse failures, and arXiv's own "Error" entries are returned as `{"error": ..., "detail": ...}` instead of crashing.

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

## Running the server

```bash
# stdio mode (what Claude Desktop uses; it waits silently for a client)
python server.py

# HTTP mode
python server.py --port 8000     # serves http://localhost:8000/mcp
```

To test interactively, use the MCP Inspector (needs Node.js):

```bash
npx @modelcontextprotocol/inspector python server.py
```

## Using it with Claude Desktop

1. Open Claude Desktop, then **Settings > Developer > Edit Config**. This opens `claude_desktop_config.json`. Typical locations:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add the server under `mcpServers`. Use **absolute paths**, and point `command` at the Python inside your virtual environment so the dependencies are found.

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

   Windows (note the doubled backslashes):
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
3. Save the file and **fully quit and restart** Claude Desktop (closing the window is not enough; quit from the tray/menu bar).
4. Open a new chat. The tools icon should list the `arxiv` server. Try:
   - "Find the 5 most recent papers on retrieval-augmented generation in cs.CL."
   - "Get the abstract of arXiv 1706.03762."

If the existing config already has other entries in `mcpServers`, add `"arxiv": { ... }` alongside them rather than replacing the block.

## Troubleshooting

- **Server doesn't appear / shows an error**: check the logs (macOS: `~/Library/Logs/Claude/mcp-server-arxiv.log`; Windows: `%APPDATA%\Claude\logs\`). The usual causes are a wrong path, JSON syntax errors in the config, or using a Python without the dependencies installed.
- **`ModuleNotFoundError`**: `command` is not pointing at the virtual environment's Python.
- **Slow responses**: expected when several calls happen back to back; the 3-second spacing is deliberate to respect arXiv's limits.
- **`ARXIV_ERROR` in a result**: arXiv rejected the query (e.g. an invalid category or malformed ID). Check the `detail` field.

## Notes

- arXiv's API returns metadata and abstracts, not full text. `pdf_url` links to the PDF.
- Be considerate: arXiv asks API users to keep to one request every 3 seconds.
- API reference: https://info.arxiv.org/help/api/user-manual.html
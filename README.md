# Salesforce MCP Server — Local Setup (macOS & Windows)

A minimal, local **Model Context Protocol (MCP)** server tailored for Salesforce workflows. This guide walks you from **clone → Python → virtual env → dependencies → Claude Desktop config → test** on both macOS and Windows.

---

## Prerequisites

- **Python 3.11+** 
- **Claude Desktop** installed
- **Git**


## 1) Clone the repository

```bash
git clone https://github.com/niteshsinwar/sfmcp
```


## 2) Create a virtual environment

### macOS / Linux (bash/zsh)
```bash
python3.11 -m venv venv
# You may keep the venv deactivated; we show both patterns below.
```

### Windows (PowerShell)
```powershell
py -3.11 -m venv venv
# You may keep the venv deactivated; we show both patterns below.
```


## 3) Install dependencies

```bash
# macOS / Linux
pip3 install -r requirements.txt
```

```powershell
# Windows (PowerShell)
pip install -r requirements.txt
```

---

## 6) Claude Desktop configuration

Claude Desktop reads a JSON file at:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

### A) macOS (bash wrapper with activation — **works with spaces in path**)

Replace the path with your actual absolute path to the project root.

```json
{
  "mcpServers": {
    "salesforce-mcp-server-local": {
      "command": "/bin/bash",
      "args": [
        "-lc",
        "cd '/ABS/PATH/TO/sfmcp' && source venv/bin/activate && python -m app.main --mcp-stdio"
      ]
    }
  }
}
```

### B) Windows (activate then run — if you prefer)

```json
{
  "mcpServers": {
    "salesforce-mcp-server-local": {
      "command": "cmd.exe",
      "args": [
        "/d",
        "/c",
        "cd /d \"C:\\ABS\\PATH\\TO\\sfmcp\" && call venv\\Scripts\\activate && python -m app.main --mcp-stdio"
      ]
    }
  }
}
```

After saving the config: **Quit and reopen Claude Desktop**.

---


you’re good—Claude will attach when it loads your server.

---



---

## 10) What’s next

- Add more tools in `app/mcp/tools/*.py` and they’ll auto-register.
- Keep dependencies pinned in `requirements.txt` to avoid breakage.
- Optionally add an SSE mode if you want to test with Postman (HTTP), otherwise STDIO is perfect for Claude Desktop.

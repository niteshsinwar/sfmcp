# Salesforce MCP Server — Quick Setup (macOS & Windows)

Straightforward steps from **clone → venv → install → Claude config**. Follow exactly.

---

## Prerequisites
- Python **3.11+**
- Claude Desktop
- Git

---

## macOS / Linux — Quick Start
```bash
# 1) Clone
git clone https://github.com/niteshsinwar/sfmcp && cd sfmcp

# 2) Create & activate venv
python3.11 -m venv venv
source venv/bin/activate

# 3) Install deps
pip install -r requirements.txt

# 4) (Optional) Test run
python -m app.main --mcp-stdio
```

### Add Claude config (UI path)
Claude Desktop → **Settings → Developer → Edit config**, paste this and replace `/ABS/PATH/TO/sfmcp` with your absolute path:

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
Quit & reopen Claude Desktop.

---

## Windows (command Prompt) — Quick Start
```bash
# 1) Clone
git clone https://github.com/niteshsinwar/sfmcp
cd sfmcp

# 2) Create & activate venv
py -3.11 -m venv venv
venv/Scripts/activate.bat

# 3) Install deps
pip install -r requirements.txt

# 4) (Optional) Test run
python -m app.main --mcp-stdio
```

### Add Claude config (UI path)
Claude Desktop → **Settings → Developer → Edit config**, paste this and replace `C:\\to\\your\\actual\\path\\sfmcp` with your absolute path:

```json
{
  "mcpServers": {
    "salesforce-mcp-server-local": {
      "command": "C:\\to\\your\\actual\\path\\sfmcp\\start_mcp.bat"
    }
  }
}
```
Quit & reopen Claude Desktop.

---

## What’s next

- Add more tools in `app/mcp/tools/*.py` and they’ll auto-register.
- Keep dependencies pinned in `requirements.txt` to avoid breakage.

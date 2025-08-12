# Salesforce MCP Server — Local Setup (macOS & Windows)

A minimal, local **Model Context Protocol (MCP)** server tailored for Salesforce workflows. This guide walks you from **clone → Python → virtual env → dependencies → Claude Desktop config → test** on both macOS and Windows.

---

## Prerequisites

- **Python 3.10+** (3.11+ recommended)
- **Claude Desktop** installed
- **Git**

Check versions:
```bash
python3 --version
```

On macOS, you can install a newer Python with Homebrew:
```bash
brew install python@3.12
```

---

## 1) Clone the repository

```bash
# macOS / Linux
git clone <YOUR_REPO_URL> sfmcp && cd sfmcp
```

```powershell
# Windows (PowerShell)
git clone <YOUR_REPO_URL> sfmcp
cd sfmcp
```

> If your repo lives elsewhere already, just `cd` into the folder that contains the `app/` directory.

---

## 2) Create a virtual environment

### macOS / Linux (bash/zsh)
```bash
python3 -m venv venv
# You may keep the venv deactivated; we show both patterns below.
```

### Windows (PowerShell)
```powershell
py -3 -m venv venv
# You may keep the venv deactivated; we show both patterns below.
```

> **Why no activation?** You can run your venv’s Python directly by path. This avoids shell-activation issues and is great for tools like Claude.

---

## 3) Install dependencies

```bash
# macOS / Linux
./venv/bin/python -m pip install --upgrade pip setuptools wheel
./venv/bin/pip install -r requirements.txt
```

```powershell
# Windows (PowerShell)
.env\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.env\Scripts\pip.exe install -r requirements.txt
```

---

## 4) Project structure

Ensure your tree looks like this (key files):

```
app/
  __init__.py
  main.py
  mcp/
    __init__.py
    server.py
    tools/
      __init__.py
      oauth_auth.py
services/
  __init__.py
  salesforce.py
requirements.txt
```

- Add empty `__init__.py` files as shown so packages import cleanly.
- Make sure the OAuth tool file is named `oauth_auth.py` (not `oauth tool.py`).

---

## 5) main.py (stdio-safe + auto tool loading)

Your `app/main.py` should not `print()` to stdout. Use this template (logs go to stderr):

```python
# app/main.py
import sys, logging, importlib, pkgutil
from app.mcp.server import mcp_server, tool_registry
import app.mcp.tools as tools_pkg

# auto-load all tool modules under app/mcp/tools
for m in pkgutil.iter_modules(tools_pkg.__path__, tools_pkg.__name__ + "."):
    importlib.import_module(m.name)

if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    if "--mcp-stdio" in sys.argv:
        logging.info("MCP starting (stdio)")
        logging.info("Tools: %s", ", ".join(tool_registry.keys()) or "(none)")
        mcp_server.run(transport="stdio")
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

> Example using your path:
> `'/Users/niteshsinwar/Desktop/My Stuff/My Projects/Salesforce MCP Server/sfmcp'`

### B) macOS (no activation; call venv Python directly)

```json
{
  "mcpServers": {
    "salesforce-mcp-server-local": {
      "command": "/ABS/PATH/TO/sfmcp/venv/bin/python",
      "args": ["-m", "app.main", "--mcp-stdio"],
      "env": {
        "PYTHONPATH": "/ABS/PATH/TO/sfmcp",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### C) Windows (call venv Python directly; recommended)

```json
{
  "mcpServers": {
    "salesforce-mcp-server-local": {
      "command": "C:\\ABS\\PATH\\TO\\sfmcp\\venv\\Scripts\\python.exe",
      "args": ["-m", "app.main", "--mcp-stdio"],
      "env": {
        "PYTHONPATH": "C:\\ABS\\PATH\\TO\\sfmcp",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

### D) Windows (activate then run — if you prefer)

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

## 7) Manual test (optional)

### macOS / Linux
```bash
# from project root
PYTHONPATH="$PWD" PYTHONUNBUFFERED=1 ./venv/bin/python -m app.main --mcp-stdio
# Ctrl+C to stop
```

### Windows (PowerShell)
```powershell
# from project root
$env:PYTHONPATH=(Get-Location).Path
$env:PYTHONUNBUFFERED="1"
.env\Scripts\python.exe -m app.main --mcp-stdio
# Ctrl+C to stop
```

If you see:
```
INFO MCP starting (stdio)
```
you’re good—Claude will attach when it loads your server.

---

## 8) Using the Salesforce OAuth tools

From Claude, run tools like:
- `salesforce_production_login`
- `salesforce_sandbox_login`
- `salesforce_custom_login`
- `salesforce_auth_status`
- `salesforce_logout`

These open a browser to authenticate and store tokens locally for the session.

---

## 9) Troubleshooting

- **Spaces in path**: Prefer the bash- or cmd-based configs above, or call the venv Python **by full path**.
- **Pip can’t find `mcp`**: Upgrade pip and ensure Python ≥ 3.10:
  ```bash
  ./venv/bin/python -m pip install --upgrade pip setuptools wheel
  ```
- **Module import errors**: Verify `__init__.py` files exist in `app/`, `app/mcp/`, `app/mcp/tools/`, and `app/services/`.
- **No tools listed**: Ensure your tool modules are imported (see `main.py` auto-loader).
- **Claude doesn’t show the server**: Confirm the JSON is in the right location, the process launches, and check logs:
  - macOS: `tail -n 200 -F ~/Library/Logs/Claude/*.log`
  - Windows: Check `%APPDATA%\Claude\logs`

---

## 10) What’s next

- Add more tools in `app/mcp/tools/*.py` and they’ll auto-register.
- Keep dependencies pinned in `requirements.txt` to avoid breakage.
- Optionally add an SSE mode if you want to test with Postman (HTTP), otherwise STDIO is perfect for Claude Desktop.

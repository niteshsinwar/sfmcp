# Salesforce MCP Server — Myridius (Aethereus)  

> “What if your LLM could *actually* do Salesforce things?”  
> This repo is a tiny **Model Context Protocol (MCP)** server that plugs into **Claude Desktop** so you can **log in**, **run SOQL**, **deploy metadata**, and **ship Apex/LWC** — fast. Professional under the hood, a little ✨chaotic good✨ on the surface.

- **Beginner-friendly**: copy → paste → go.  
- **Production-lean**: OAuth, deploy polling, idempotent upserts.  
- **Zero fluff**: you ask, tools run. No secret handshakes. 😎

---

## ✨ Features (at a glance)

- 🔐 **One-click OAuth** (browser redirect) to **Prod / Sandbox / Custom Domain**
- 🧰 **Handy tools**: SOQL, Object/Field upserts, Apex + LWC create/update, deploy status
- ⚡️ **Claude-ready**: `--mcp-stdio` baked in
- 🧹 **Minimal deps**: Python 3.11+, that’s it

---

## 🧪 Quick Start — macOS / Linux

```bash
# 1) Clone
git clone https://github.com/niteshsinwar/sfmcp && cd sfmcp

# 2) Create & activate venv

#first check python version:
python --version
# Make sure the the python version is greater then  or equal to 3.11
python -m venv venv
source venv/bin/activate

# 3) Install deps
pip install -r requirements.txt

# 4) (Optional) Test run (stdio mode for Claude)
python -m app.main --mcp-stdio
```

### Add to Claude Desktop (2 clicks, 1 paste)
Claude Desktop → **Settings → Developer → Edit config**, then paste (swap your absolute path):

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
Restart Claude Desktop. (Yes, the classic “turn it off and on again.”)

---

## 🪟 Quick Start — Windows (Command Prompt)

```bash
# 1) Clone
git clone https://github.com/niteshsinwar/sfmcp
cd sfmcp

# 2) Create & activate venv
#first check python version:
python --version
# Make sure the the python version is greater then  or equal to 3.11
python -m venv venv
venv\Scripts\activate

# 3) Install deps
pip install -r requirements.txt

# 4) (Optional) Test run
python -m app.main --mcp-stdio
```

### Claude config on Windows
Claude Desktop → **Settings → Developer → Edit config**:

```json
{
  "mcpServers": {
    "salesforce-mcp-server-local": {
      "command": "C:\\to\\your\\actual\\path\\sfmcp\\start_mcp.bat"
    }
  }
}
```

---

## 🕹 Using It in Claude (the fun part)

1. In a Claude chat, type: **“Login to Salesforce prod”** (or *sandbox* / *custom*).
2. Browser pops → log in → **Allow**.
3. Back in Claude, run tools like a wizard:

**Examples**
- “Run SOQL: `SELECT Id, Name FROM Account LIMIT 5`”
- “Create a Text field `Customer_Code__c` (length 50) on Account”
- “Create/update Apex class `HelloWorld`”
- “Create/update LWC `helloWorldCard`”

**Typical flow in one go**
```
- salesforce_production_login
- salesforce_auth_status
- upsert_custom_field (Account.Customer_Code__c, type=Text, length=50)
- execute_soql_query (SELECT DurableId FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName='Account' AND QualifiedApiName='Customer_Code__c')
```

---

## 🧩 Built-in Tools

These auto-register on server start:

| Tool | Why you care |
|---|---|
| `salesforce_production_login` | OAuth to **Prod** via browser. |
| `salesforce_sandbox_login` | OAuth to **Sandbox** (`test.salesforce.com`). |
| `salesforce_custom_login` | OAuth to a **custom domain** org. |
| `salesforce_logout` | Clear active session (politely). |
| `salesforce_auth_status` | Show current sessions + active routing. |
| `execute_soql_query` | Run SOQL. You know you want to. |
| `get_metadata_deploy_status` | Poll a deploy (supports `includeDetails`). |
| `fetch_object_metadata` | `sObject describe` quick view. |
| `upsert_custom_object` | Create/update a **Custom Object**. |
| `fetch_custom_field` | Checks **Tooling FieldDefinition** first (fresh) then `describe()`. |
| `upsert_custom_field` | Create/update a field on **standard/custom** objects. |
| `fetch_apex_class` | Get Apex (body + meta). |
| `create_apex_class` | Deploy new Apex class. |
| `upsert_apex_class` | Create or update, idempotent. |
| `fetch_lwc_component` | Read LWC bundle & resources. |
| `create_lwc_component` | Scaffold new LWC (html/js/xml/css). |
| `upsert_lwc_component` | Update LWC bundle safely. |

---

## 🎯 Demo Scenario (E2E sanity)

```bash
python -m test.run_dynamic_scenarios --target prod --user "your.user@domain.com" --inputs test/scenario_inputs.json --verbose
```

**What it does:**  
Creates an object → adds a field → creates/updates Apex → creates/updates LWC → polls deploys → fetches stuff.  
Result: Sip Coffee while seeing getting work done.

**⚠️ IMPORTANT for Contributors:**  
When you add a **new tool** or modify existing functionality, you **MUST**:
1. 🧪 **Add your tool to the test scenarios** in `test/scenario_inputs.json`
2. 🔍 **Run the full test suite** to ensure everything works end-to-end
3. ✅ **Verify no regressions** before submitting your PR

This E2E test is our safety net — treat it like production validation!


---

## 🤝 Contribute (bring your brain)

**🚨 Testing First Rule:**  
Before you even think about submitting a PR:
1. 📝 **Add your new tool/feature to `test/scenario_inputs.json`**
2. 🏃‍♂️ **Run the test suite:** `python -m test.run_dynamic_scenarios --target sandbox --user "your.test@domain.com" --inputs test/scenario_inputs.json --verbose`
3. ✅ **Ensure ALL tests pass** — no exceptions, no "it works on my machine"

**Contribution Guidelines:**
- **Ideas / Bugs** → GitHub Issues (clear repro = instant karma).
- **PRs** → small, focused, with a test if you change behavior.
- **New tools?** Pitch the shape:
  - Name & intent
  - Inputs/outputs JSON
  - Error model
  - Example prompt(s) for Claude
  - **Test scenario** demonstrating the tool works

**Testing is NOT optional:**  
Every new tool must have a corresponding test scenario. We maintain high reliability through comprehensive E2E testing. If your tool doesn't have tests, your PR will be rejected faster than a malformed SOQL query.

**Roadmap-ish (send PRs):**
- Apex test runner with coverage summaries
- Profiles/PermissionSets helpers
- Health Check

---

## 👨‍🚀 Maintainers

**NiteshSinwar (Myridius)** — caretakers of this chaos.  
Repo: https://github.com/niteshsinwar/sfmcp

> If this saved you an hour, star the repo ⭐. 

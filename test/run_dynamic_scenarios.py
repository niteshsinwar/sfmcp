
#!/usr/bin/env python3
"""
Dynamic Tools Scenario Runner
-----------------------------
Runs an end-to-end synergy test across oauth_auth and dynamic_tools:
  1) Login (prod/sandbox/custom)
  2) Auth status check
  3) SOQL sanity query
  4) Upsert custom object (unique temp name)
  5) Fetch object metadata
  6) Upsert custom field on that object
  7) Fetch custom field metadata
  8) Create Apex class -> Fetch -> Update Apex class
  9) Create LWC -> Fetch -> Update LWC
 10) If any previous step returns a deploy job id, check get_metadata_deploy_status

You can override inputs or tweak steps via a JSON file.

Usage examples:
  python run_dynamic_scenarios.py --target sandbox --user YOUR_SANDBOX_USERNAME
  python run_dynamic_scenarios.py --target prod --user YOUR_PROD_USERNAME
  python run_dynamic_scenarios.py --inputs scenario_inputs.json --verbose

Note: This will perform WRITE operations in your Salesforce org.
"""

import argparse
import importlib
import importlib.util
import inspect
import json
import logging
import os
import sys
import time
import types
from typing import Any, Dict, Tuple

LOG = logging.getLogger("scenario_runner")

# ---------- Bootstrap imports (package or flat layout) ----------
def _create_pkg(name: str) -> types.ModuleType:
    if name in sys.modules: return sys.modules[name]
    m = types.ModuleType(name); m.__path__ = []; sys.modules[name] = m; return m

def _load_as(modname: str, filepath: str):
    spec = importlib.util.spec_from_file_location(modname, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {modname} at {filepath}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def sanitize_lwc_name(name: str) -> str:
    """Return a safe LWC bundle name: start with lowercase letter; only [A-Za-z0-9_]; max ~40 chars."""
    import re
    s = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not s or not s[0].islower():
        s = 'c' + s
    return s[:40]
def bootstrap_modules(repo_root: str) -> Tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    try:
        server_mod = importlib.import_module("app.mcp.server")
        oauth_mod  = importlib.import_module("app.mcp.tools.oauth_auth")
        dynamic_mod= importlib.import_module("app.mcp.tools.dynamic_tools")
        return server_mod, oauth_mod, dynamic_mod
    except Exception as pkg_exc:
        LOG.debug("Package import failed: %s", pkg_exc)

    _create_pkg("app"); _create_pkg("app.mcp"); _create_pkg("app.services"); _create_pkg("app.mcp.tools")

    server_path = os.path.join(repo_root, "server.py")
    oauth_path  = os.path.join(repo_root, "oauth_auth.py")
    dynamic_path= os.path.join(repo_root, "dynamic_tools.py")

    if not os.path.exists(server_path):
        raise FileNotFoundError(f"server.py not found at {server_path}")
    server_mod = _load_as("app.mcp.server", server_path)

    sf_path = os.path.join(repo_root, "salesforce.py")
    if os.path.exists(sf_path):
        _load_as("app.services.salesforce", sf_path)

    oauth_mod = _load_as("app.mcp.tools.oauth_auth", oauth_path) if os.path.exists(oauth_path) else types.ModuleType("app.mcp.tools.oauth_auth")
    dynamic_mod = _load_as("app.mcp.tools.dynamic_tools", dynamic_path) if os.path.exists(dynamic_path) else None
    if dynamic_mod is None:
        raise FileNotFoundError(f"dynamic_tools.py not found at {dynamic_path}")

    return server_mod, oauth_mod, dynamic_mod

# ---------- Registry helpers ----------
def get_tool_callable(server_mod, dynamic_mod, name: str):
    reg = getattr(server_mod, "tool_registry", {})
    meta = reg.get(name)
    func = None
    if callable(meta):
        func = meta
    elif isinstance(meta, dict):
        # Support both 'func' and 'function'
        if callable(meta.get("func")):
            func = meta["func"]
        elif callable(meta.get("function")):
            func = meta["function"]
    elif hasattr(meta, "func") and callable(getattr(meta, "func")):
        func = meta.func
    if not callable(func):
        try:
            cand = getattr(dynamic_mod, name)
            if callable(cand): func = cand
        except Exception:
            pass
    return func

# ---------- Argument building ----------
def typed_placeholder(param: inspect.Parameter) -> Any:
    ann = param.annotation
    name = param.name.lower()

    if any(k in name for k in ("password", "token", "secret", "session", "instance", "refresh", "client_id", "client_secret", "domain")):
        return ""

    if name in ("soql", "query", "tooling_soql"):
        return "SELECT Id FROM User LIMIT 1"
    if name in ("apex_code","apex_body","body","html","js","css","package_xml","object_xml","description","type_params","plural_label","label","field_api_name","field_type","object_name","name"):
        return ""

    if ann in (str, "str"): return "TEST"
    if ann in (int, "int"): return 1
    if ann in (float, "float"): return 1.0
    if ann in (bool, "bool"): return False
    if ann in (dict, "dict") or getattr(ann, "_name", None) == "Dict": return {}
    if ann in (list, "list") or getattr(ann, "_name", None) == "List": return []

    if param.default is not inspect._empty:
        return param.default
    return None

def build_kwargs(func, overrides: Dict[str, Any] | None) -> Dict[str, Any]:
    sig = inspect.signature(func)
    kwargs = {}
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL): continue
        if overrides and name in overrides:
            kwargs[name] = overrides[name]
        else:
            kwargs[name] = typed_placeholder(p)
    return kwargs

def call_tool(server_mod, dynamic_mod, name: str, overrides: Dict[str, Any] | None) -> Dict[str, Any]:
    func = get_tool_callable(server_mod, dynamic_mod, name)
    if not callable(func):
        return {"tool": name, "status": "missing", "error": f"Tool '{name}' not callable in registry.", "result": None}

    start = time.time()
    try:
        kwargs = build_kwargs(func, overrides or {})
        result = func(**kwargs)
        dur = round(time.time() - start, 3)
        return {"tool": name, "status": "ok", "duration_sec": dur, "kwargs": kwargs, "result": result}
    except Exception as e:
        dur = round(time.time() - start, 3)
        return {"tool": name, "status": "error", "duration_sec": dur, "kwargs": overrides, "error": f"{type(e).__name__}: {e}"}



def _result_success(res_entry: dict) -> bool:
    """Return True iff the tool response JSON has {"success": true}."""
    try:
        if res_entry.get("status") != "ok":
            return False
        payload = res_entry.get("result")
        if isinstance(payload, str):
            data = json.loads(payload)
        else:
            data = payload
        return bool(data and data.get("success") is True)
    except Exception:
        return False


# ---------- Deploy status helpers ----------
def _extract_job_id(entry) -> str | None:
    try:
        payload = entry.get("result")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            # prefer explicit job_id field
            jid = payload.get("job_id") or payload.get("id")
            # some wrappers put deployResult under 'details' with id elsewhere
            return jid
    except Exception:
        return None
    return None

def _pretty_deploy_errors(deploy_payload: dict) -> str:
    """Return a human-readable summary of component failures from get_metadata_deploy_status."""
    try:
        details = deploy_payload.get("details") or {}
        failures = details.get("componentFailures") or []
        # Some APIs return a single object instead of list
        if isinstance(failures, dict):
            failures = [failures]
        if not failures:
            return "No component failures reported."
        lines = []
        for f in failures:
            file_name = f.get("fileName") or f.get("componentFullName") or "?"
            problem = f.get("problem") or f.get("message") or "Unknown error"
            ptype = f.get("problemType") or f.get("componentType") or ""
            line = f.get("lineNumber")
            col = f.get("columnNumber")
            loc = f" (line {line}, col {col})" if line or col else ""
            lines.append(f"- {file_name}{loc}: {problem} [{ptype}]")
        return "\n".join(lines) if lines else "No component failures reported."
    except Exception as e:
        return f"Could not parse deployment errors: {e}"

def call_deploy_status_if_any(server_mod, dynamic_mod, res_entry: dict, results_list: list) -> None:
    jid = _extract_job_id(res_entry)
    if not jid:
        return
    dep_over = {"job_id": jid, "include_details": True}
    dep_res = call_tool(server_mod, dynamic_mod, "get_metadata_deploy_status", dep_over)
    results_list.append(dep_res)

# ---------- Scenario ----------
def scenario(repo_root: str, target: str, user: str | None, inputs: Dict[str, Dict[str, Any]] | None):
    server_mod, oauth_mod, dynamic_mod = bootstrap_modules(repo_root)
    _ = oauth_mod; _ = dynamic_mod

    results = []

    # Unique suffix for names
    suffix = time.strftime("%Y%m%d_%H%M%S")
    obj_api = f"AI_Test_Object_{suffix}__c"
    obj_label = f"AI Test Object {suffix}"
    obj_plural = f"AI Test Objects {suffix}"
    field_api = f"Test_Field_{suffix}__c"
    apex_name = f"AITest_{suffix}"
    lwc_name  = sanitize_lwc_name(f"aiTest_{suffix.replace('_', '')}")  # keep it short-ish

    # ----------------- 1) Login -----------------
    if target == "sandbox":
        tool = "salesforce_sandbox_login"
        login_over = {"selected_user": user or ""}
    elif target == "prod":
        tool = "salesforce_production_login"
        login_over = {"selected_user": user or ""}
    elif target == "custom":
        tool = "salesforce_custom_login"
        # Allow domain override via inputs; otherwise placeholder
        dom = (inputs or {}).get(tool, {}).get("domain", "")
        login_over = {"selected_user": user or "", "domain": dom}
    else:
        raise SystemExit("--target must be one of: sandbox|prod|custom")

    if inputs and tool in inputs:
        login_over.update(inputs[tool])

    results.append(call_tool(server_mod, dynamic_mod, tool, login_over))

    # ----------------- 2) Auth status -----------------
    results.append(call_tool(server_mod, dynamic_mod, "salesforce_auth_status", (inputs or {}).get("salesforce_auth_status")))

    # ----------------- 3) SOQL sanity -----------------
    soql_over = {"soql": "SELECT Id, Name FROM Account LIMIT 1"}
    if inputs and "execute_soql_query" in inputs:
        soql_over.update(inputs["execute_soql_query"])
    results.append(call_tool(server_mod, dynamic_mod, "execute_soql_query", soql_over))

    # ----------------- 4) Upsert custom object -----------------
    obj_over = {
        "object_name": obj_api,
        "label": obj_label,
        "plural_label": obj_plural,
        "description": "Temporary object created by scenario runner"
    }
    if inputs and "upsert_custom_object" in inputs:
        obj_over.update(inputs["upsert_custom_object"])
    res_obj = call_tool(server_mod, dynamic_mod, "upsert_custom_object", obj_over)
    results.append(res_obj)

    # Small wait for object propagation
    time.sleep(2)

    # ----------------- 5) Fetch object metadata -----------------
    fetch_obj_over = {"object_name": obj_api}
    if inputs and "fetch_object_metadata" in inputs:
        fetch_obj_over.update(inputs["fetch_object_metadata"])
    results.append(call_tool(server_mod, dynamic_mod, "fetch_object_metadata", fetch_obj_over))

    # ----------------- 6) Upsert custom field -----------------
    field_over = {
        "object_name": obj_api,
        "field_api_name": field_api,
        "label": f"Test Field {suffix}",
        "field_type": "Text",
        "type_params": "length=50",
        "required": False,
        "description": "Temporary field created by scenario runner"
    }
    if inputs and "upsert_custom_field" in inputs:
        field_over.update(inputs["upsert_custom_field"])
    res_field = call_tool(server_mod, dynamic_mod, "upsert_custom_field", field_over)
    results.append(res_field)

    # ----------------- 7) Fetch custom field metadata -----------------
    
    fetch_field_over = {"object_name": obj_api, "field_api_name": field_api}
    if inputs and "fetch_custom_field" in inputs:
        fetch_field_over.update(inputs["fetch_custom_field"])
    results.append(call_tool(server_mod, dynamic_mod, "fetch_custom_field", fetch_field_over))

    # ----------------- 8) Apex: create -> fetch -> update -----------------
    apex_body = f"""public with sharing class {apex_name} {{
    public static String ping() {{ return 'pong-1'; }}
}}"""
    apex_over_create = {"name": apex_name, "class_name": apex_name, "body": apex_body, "apex_body": apex_body}
    if inputs and "create_apex_class" in inputs:
        apex_over_create.update(inputs["create_apex_class"])
    res_create_apex_class = call_tool(server_mod, dynamic_mod, "create_apex_class", apex_over_create)
    results.append(res_create_apex_class)
    call_deploy_status_if_any(server_mod, dynamic_mod, res_create_apex_class, results)

    fetch_apex_over = {"name": apex_name, "class_name": apex_name}
    if inputs and "fetch_apex_class" in inputs:
        fetch_apex_over.update(inputs["fetch_apex_class"])
    results.append(call_tool(server_mod, dynamic_mod, "fetch_apex_class", fetch_apex_over))

    apex_body2 = f"""public with sharing class {apex_name} {{
    public static String ping() {{ return 'pong-2'; }}
}}"""
    apex_over_update = {"name": apex_name, "class_name": apex_name, "body": apex_body2, "apex_body": apex_body2}
    if inputs and "upsert_apex_class" in inputs:
        apex_over_update.update(inputs["upsert_apex_class"])
    res_upsert_apex_class = call_tool(server_mod, dynamic_mod, "upsert_apex_class", apex_over_update)
    results.append(res_upsert_apex_class)
    call_deploy_status_if_any(server_mod, dynamic_mod, res_upsert_apex_class, results)

        
    # ----------------- 9) LWC: create -> (fetch -> update) if created -----------------
    html = f"<template><div>Hello {lwc_name}</div></template>"
    js = f"import {{ LightningElement }} from 'lwc';\nexport default class {lwc_name} extends LightningElement {{}}\n"
    css = "div { padding: 4px; }"

    lwc_create_over = {"component_name": lwc_name, "html_content": html, "js_content": js, "css_content": css}
    if inputs and "create_lwc_component" in inputs:
        lwc_create_over.update(inputs["create_lwc_component"])
    res_lwc_create = call_tool(server_mod, dynamic_mod, "create_lwc_component", lwc_create_over)
    results.append(res_lwc_create)
    call_deploy_status_if_any(server_mod, dynamic_mod, res_lwc_create, results)

    # Only proceed if creation succeeded
    if _result_success(res_lwc_create):
        time.sleep(2)  # small wait for propagation

        lwc_fetch_over = {"component_name": lwc_name}
        if inputs and "fetch_lwc_component" in inputs:
            lwc_fetch_over.update(inputs["fetch_lwc_component"])
        res_lwc_fetch = call_tool(server_mod, dynamic_mod, "fetch_lwc_component", lwc_fetch_over)
        results.append(res_lwc_fetch)

        js2 = f"import {{ LightningElement }} from 'lwc';\nexport default class {lwc_name} extends LightningElement {{ connectedCallback(){{}} }}\n"
        lwc_update_over = {"component_name": lwc_name, "html_content": html, "js_content": js2, "css_content": css}
        if inputs and "upsert_lwc_component" in inputs:
            lwc_update_over.update(inputs["upsert_lwc_component"])
        res_upsert_lwc_component = call_tool(server_mod, dynamic_mod, "upsert_lwc_component", lwc_update_over)
        results.append(res_upsert_lwc_component)
        call_deploy_status_if_any(server_mod, dynamic_mod, res_upsert_lwc_component, results)
    else:
        results.append({"tool": "fetch_lwc_component", "status": "skipped", "reason": "create_lwc_component failed"})
        results.append({"tool": "upsert_lwc_component", "status": "skipped", "reason": "create_lwc_component failed"})
# ----------------- 10) Deploy status (if id found) -----------------
    job_id = None
    for r in results:
        if not isinstance(r, dict): continue
        res = r.get("result")
        if isinstance(res, str):
            try:
                res_obj = json.loads(res)
            except Exception:
                res_obj = None
            if isinstance(res_obj, dict):
                for k in ("job_id","deployId","id","asyncProcessId"):
                    if k in res_obj and isinstance(res_obj[k], (str, int)):
                        job_id = str(res_obj[k]); break
        elif isinstance(res, dict):
            for k in ("job_id","deployId","id","asyncProcessId"):
                if k in res and isinstance(res[k], (str, int)):
                    job_id = str(res[k]); break
        if job_id: break

    if job_id:
        dep_over = {"job_id": job_id, "deploy_id": job_id, "id": job_id, "async_process_id": job_id}
        if inputs and "get_metadata_deploy_status" in inputs:
            dep_over.update(inputs["get_metadata_deploy_status"])
        results.append(call_tool(server_mod, dynamic_mod, "get_metadata_deploy_status", dep_over))
    else:
        results.append({"tool":"get_metadata_deploy_status","status":"skipped","reason":"no job id discovered in earlier steps"})

    summary = {
        "target": target,
        "object_api": obj_api,
        "field_api": field_api,
        "apex_name": apex_name,
        "lwc_name": lwc_name,
        "results": results
    }
    return summary

def main(argv=None):
    parser = argparse.ArgumentParser(description="Run oauth_auth + dynamic_tools synergy test.")
    parser.add_argument("--repo-root", default=".", help="Repo root where server.py lives")
    parser.add_argument("--target", choices=["sandbox","prod","custom"], default="sandbox")
    parser.add_argument("--user", help="Username/email to select in login tool (selected_user)")
    parser.add_argument("--inputs", type=str, help="JSON with per-tool overrides")
    parser.add_argument("--out", type=str, help="Write JSON results to this path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    overrides = None
    if args.inputs:
        with open(args.inputs, "r", encoding="utf-8") as f:
            overrides = json.load(f)

    summary = scenario(os.path.abspath(args.repo_root), args.target, args.user, overrides or {})

    # console report
    print("\n=== Scenario Summary ===")
    print(f"Target: {summary['target']}")
    print(f"Object: {summary['object_api']}")
    print(f"Field:  {summary['field_api']}")
    print(f"Apex:   {summary['apex_name']}")
    print(f"LWC:    {summary['lwc_name']}\n")

    ok = sum(1 for r in summary["results"] if r.get("status") == "ok")
    err = sum(1 for r in summary["results"] if r.get("status") == "error")
    skp = sum(1 for r in summary["results"] if r.get("status") == "skipped")
    print(f"OK: {ok}  Errors: {err}  Skipped: {skp}  Total steps: {len(summary['results'])}\n")

    for r in summary["results"]:
        tag = r.get("tool")
        status = r.get("status")
        print(f"- {tag}: {status} ({r.get('duration_sec','-')}s)")
        if status == "error":
            print(f"    ! {r.get('error')}")
        if status == "ok":
            res = r.get("result")
            snippet = None
            # Enhanced printing: show full deploy details for get_metadata_deploy_status
            try:
                snippet = json.dumps(res, default=str)
            except Exception:
                snippet = str(res)
            if r.get("tool") == "get_metadata_deploy_status":
                # Attempt to pretty print failures
                payload = r.get("result")
                if isinstance(payload, str):
                    try:
                        payload_json = json.loads(payload)
                    except Exception:
                        payload_json = None
                else:
                    payload_json = payload
                if isinstance(payload_json, dict):
                    print("    ↳ Deployment status:", payload_json.get("status"))
                    errs = _pretty_deploy_errors(payload_json)
                    if errs and "No component failures" not in errs:
                        print("    ↳ Failures:\n" + errs)
                    else:
                        # If no failures, still show a compact view
                        print("    ↳", json.dumps(payload_json, indent=2)[:2000])
                else:
                    print("    ↳", snippet[:2000])
            else:
                if snippet:
                    print(f"    ↳ {snippet[:220]}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        print(f"\nSaved JSON report to {args.out}\n")

if __name__ == "__main__":
    raise SystemExit(main())

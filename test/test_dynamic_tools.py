
#!/usr/bin/env python3
"""
Dynamic Tools Test Runner
=========================

Discovers tools registered via `app.mcp.server.tool_registry` and optionally executes them.
Supports both repo layouts:
- package style: app/mcp/server.py, app/mcp/tools/{oauth_auth.py,dynamic_tools.py}
- flat files at repo root: server.py, oauth_auth.py, dynamic_tools.py

Usage examples:
  # List tools only
  python test_dynamic_tools.py --list

  # Run *read-only* tools (skips deploy/upsert/delete/logout) with auto-generated placeholder inputs
  python test_dynamic_tools.py --run --allow-read-only

  # Run specific tools with custom inputs
  python test_dynamic_tools.py --run --only execute_soql_query \\
      --inputs tool_inputs.json

  # Run everything including write operations (use with caution)
  python test_dynamic_tools.py --run --allow-write --timeout 180 --results results.json

Provide per-tool inputs via JSON file, e.g. tool_inputs.json:
{
  "salesforce_production_login": { "selected_user": "myuser@example.com" },
  "execute_soql_query": { "soql": "SELECT Id, Name FROM Account LIMIT 1" }
}

The runner will auto-fill missing parameters with typed placeholders when safe.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Tuple

LOG = logging.getLogger("test_dynamic_tools")

# ----------------------------
# Module bootstrap helpers
# ----------------------------

def _create_pkg(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    m = types.ModuleType(name)
    m.__path__ = []  # mark as package
    sys.modules[name] = m
    return m

def _load_as(modname: str, filepath: str):
    spec = importlib.util.spec_from_file_location(modname, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {modname} at {filepath}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module

def bootstrap_modules(repo_root: str) -> Tuple[types.ModuleType, types.ModuleType, types.ModuleType]:
    """Return (server_mod, oauth_mod, dynamic_mod), loading from package layout if present
    or falling back to flat files at repo_root.
    """
    # Prefer package layout
    try:
        server_mod = importlib.import_module("app.mcp.server")
        oauth_mod  = importlib.import_module("app.mcp.tools.oauth_auth")
        dynamic_mod= importlib.import_module("app.mcp.tools.dynamic_tools")
        return server_mod, oauth_mod, dynamic_mod
    except Exception as pkg_exc:
        LOG.debug("Package import failed: %s", pkg_exc)

    # Fallback to flat files; alias into expected package names
    _create_pkg("app")
    _create_pkg("app.mcp")
    _create_pkg("app.services")
    _create_pkg("app.mcp.tools")

    server_path = os.path.join(repo_root, "server.py")
    oauth_path  = os.path.join(repo_root, "oauth_auth.py")
    dynamic_path= os.path.join(repo_root, "dynamic_tools.py")

    if not os.path.exists(server_path):
        raise FileNotFoundError(f"server.py not found at {server_path}")
    server_mod = _load_as("app.mcp.server", server_path)

    # Load salesforce shim if present, some tools import it
    sf_path = os.path.join(repo_root, "salesforce.py")
    if os.path.exists(sf_path):
        _load_as("app.services.salesforce", sf_path)

    if os.path.exists(oauth_path):
        oauth_mod = _load_as("app.mcp.tools.oauth_auth", oauth_path)
    else:
        oauth_mod = types.ModuleType("app.mcp.tools.oauth_auth")

    if os.path.exists(dynamic_path):
        dynamic_mod = _load_as("app.mcp.tools.dynamic_tools", dynamic_path)
    else:
        raise FileNotFoundError(f"dynamic_tools.py not found at {dynamic_path}")

    return server_mod, oauth_mod, dynamic_mod

# ----------------------------
# Tool discovery & helpers
# ----------------------------

WRITE_KEYWORDS = ("deploy", "upsert", "insert", "update", "delete", "logout", "create", "set_", "start_", "stop_")

def is_write_tool(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in WRITE_KEYWORDS)

def typed_placeholder(param: inspect.Parameter) -> Any:
    """Generate a safe placeholder for a function parameter based on type hints and name."""
    ann = param.annotation
    name = param.name.lower()

    # Common credential-ish params get blanks to force user to supply real values.
    if any(k in name for k in ("password", "token", "secret", "session", "instance", "refresh", "client_id", "client_secret")):
        return ""

    # SOQL/APEX bodies default to benign values
    if name in ("soql", "query", "apex_code", "apex_body", "package_xml"):
        return "SELECT Id FROM User LIMIT 1" if "soql" in name or "query" in name else ""

    # Type-based defaults
    if ann in (str, "str"):
        return "TEST"
    if ann in (int, "int"):
        return 1
    if ann in (float, "float"):
        return 1.0
    if ann in (bool, "bool"):
        return False

    # Containers
    if ann in (dict, "dict") or getattr(ann, "_name", None) == "Dict":
        return {}
    if ann in (list, "list") or getattr(ann, "_name", None) == "List":
        return []

    # Fallback
    if param.default is not inspect._empty:
        return param.default
    return None

def build_call_kwargs(func, overrides: Dict[str, Any] | None) -> Dict[str, Any]:
    sig = inspect.signature(func)
    kwargs = {}
    for name, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if overrides and name in overrides:
            kwargs[name] = overrides[name]
        else:
            kwargs[name] = typed_placeholder(p)
    return kwargs

# ----------------------------
# Runner
# ----------------------------

def run_tool(name: str, func, overrides: Dict[str, Any] | None, timeout: int, allow_write: bool) -> Dict[str, Any]:
    start = time.time()
    outcome = {
        "tool": name,
        "callable": f"{func.__module__}.{func.__name__}",
        "write_op": is_write_tool(name),
        "status": "skipped",
        "duration_sec": 0.0,
        "error": None,
        "result_preview": None,
        "kwargs": {}
    }
    try:
        if outcome["write_op"] and not allow_write:
            outcome["status"] = "skipped (write op)"
            return outcome

        kwargs = build_call_kwargs(func, overrides or {})
        outcome["kwargs"] = kwargs

        # naive timeout guard (soft)
        def _invoke():
            return func(**kwargs)

        result = _invoke()
        elapsed = time.time() - start
        outcome["duration_sec"] = round(elapsed, 3)
        outcome["status"] = "ok"
        try:
            outcome["result_preview"] = json.dumps(result, default=str)[:800]
        except Exception:
            outcome["result_preview"] = str(result)[:800]
        return outcome
    except Exception as e:
        elapsed = time.time() - start
        outcome["duration_sec"] = round(elapsed, 3)
        outcome["status"] = "error"
        outcome["error"] = f"{type(e).__name__}: {e}"
        return outcome

def main(argv=None):
    parser = argparse.ArgumentParser(description="Discover and test registered dynamic tools.")
    parser.add_argument("--repo-root", default=".", help="Path to repo root (where server.py may live). Default: current dir")
    parser.add_argument("--list", action="store_true", help="List tools and exit")
    parser.add_argument("--run", action="store_true", help="Execute tools (read-only by default unless --allow-write)")
    parser.add_argument("--only", nargs="*", help="Only run these tool names (space separated)")
    parser.add_argument("--exclude", nargs="*", help="Exclude these tool names")
    parser.add_argument("--inputs", type=str, help="JSON file with per-tool input overrides")
    parser.add_argument("--allow-read-only", action="store_true", help="Alias for --run with writes skipped (default behavior)")
    parser.add_argument("--allow-write", action="store_true", help="Allow running write/deploy/upsert/delete/logout tools (DANGEROUS)" )
    parser.add_argument("--timeout", type=int, default=120, help="Soft timeout in seconds per tool (best-effort)")
    parser.add_argument("--workers", type=int, default=1, help="Max parallel workers for execution")
    parser.add_argument("--results", type=str, help="Write JSON results to this path")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    server_mod, oauth_mod, dynamic_mod = bootstrap_modules(os.path.abspath(args.repo_root))

    # Force import side-effects to register tools
    _ = oauth_mod
    _ = dynamic_mod

    if not hasattr(server_mod, "tool_registry") or not isinstance(server_mod.tool_registry, dict):
        raise RuntimeError("tool_registry not found on app.mcp.server; cannot discover tools.")

    registry = server_mod.tool_registry

    LOG.info("Discovered %d tools", len(registry))

    tools = []
    for name, meta in registry.items():
        func = None
        if callable(meta):
            func = meta
        elif isinstance(meta, dict) and callable(meta.get("func")):
            func = meta["func"]
        elif hasattr(meta, "func") and callable(getattr(meta, "func")):
            func = meta.func  # type: ignore[attr-defined]
        else:
            # last resort: if the value itself is not callable, try to resolve by name from modules
            try:
                func = getattr(dynamic_mod, name)
            except Exception:
                pass

        if not callable(func):
            LOG.warning("Skipping uncallable tool entry '%s' (%r)", name, meta)
            continue
        tools.append((name, func))

    tools.sort(key=lambda x: x[0].lower())

    if args.list or not args.run:
        print("\nRegistered tools:\n------------------")
        for name, func in tools:
            doc = (inspect.getdoc(func) or "").split("\n")[0]
            rw = "WRITE" if is_write_tool(name) else "READ"
            print(f"- {name}  [{rw}]  -> {func.__module__}.{func.__name__}")
            if doc:
                print(f"    {doc}")
        print("\nTip: run with --run to execute (skips write ops unless --allow-write)\n")
        return 0

    # Load overrides
    overrides: Dict[str, Dict[str, Any]] = {}
    if args.inputs:
        with open(args.inputs, "r", encoding="utf-8") as f:
            overrides = json.load(f)

    selected = tools
    if args.only:
        only_set = set(args.only)
        selected = [(n, f) for (n, f) in tools if n in only_set]
    if args.exclude:
        excl = set(args.exclude)
        selected = [(n, f) for (n, f) in selected if n not in excl]

    LOG.info("Executing %d tools (write ops %s)", len(selected), "ENABLED" if args.allow_write else "SKIPPED")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = []
        for (name, func) in selected:
            res_overrides = overrides.get(name)
            futures.append(ex.submit(run_tool, name, func, res_overrides, args.timeout, args.allow_write))

        for fut in as_completed(futures):
            results.append(fut.result())

    # Print summary and optionally write JSON
    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    skp = sum(1 for r in results if r["status"].startswith("skipped"))

    print("\nSummary:\n--------")
    print(f"OK: {ok}   Errors: {err}   Skipped: {skp}   Total: {len(results)}\n")

    for r in sorted(results, key=lambda x: x["tool"]):
        print(f"{r['tool']}: {r['status']}  ({r['duration_sec']}s)")
        if r["error"]:
            print(f"  ! {r['error']}")
        if r["result_preview"]:
            print(f"  ↳ {r['result_preview'][:200]}")

    if args.results:
        with open(args.results, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nWrote JSON results to {args.results}\n")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

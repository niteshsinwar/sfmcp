import logging
import requests
import json
import time
import zipfile
import io
import re
from typing import Optional, Dict, Any, List
from lxml import etree
import base64

from app.mcp.server import register_tool
from app.services.salesforce import get_salesforce_connection

logger = logging.getLogger(__name__)



# Simple validator for LWC bundle names: must start with lowercase letter and contain only letters, numbers, or underscores.
def _validate_lwc_bundle_name(name: str) -> bool:
    try:
        return bool(re.match(r"^[a-z][A-Za-z0-9_]*$", name))
    except Exception:
        return False
# =============================================================================
# INTERNAL HELPERS – PACKAGE / XML GENERATORS
# =============================================================================

def _generate_package_xml(members: List[str], metadata_type: str, api_version: str) -> str:
    """Generate a package.xml with one or more members of a single metadata type."""
    PNS = "http://soap.sforce.com/2006/04/metadata"
    root = etree.Element(etree.QName(PNS, "Package"), nsmap={None: PNS})

    types_tag = etree.SubElement(root, etree.QName(PNS, "types"))
    for m in members:
        etree.SubElement(types_tag, etree.QName(PNS, "members")).text = m
    etree.SubElement(types_tag, etree.QName(PNS, "name")).text = metadata_type

    version_tag = etree.SubElement(root, etree.QName(PNS, "version"))
    version_tag.text = api_version

    return etree.tostring(
        root, encoding="UTF-8", xml_declaration=True, pretty_print=True
    ).decode("utf-8")


def _generate_custom_object_xml(
    object_label: str,
    plural_label: str,
    description: str = "",
    sharing_model: str = "ReadWrite",
    deployment_status: str = "Deployed",
) -> str:
    """Return CustomObject-level XML (no <fields/>)."""
    PNS = "http://soap.sforce.com/2006/04/metadata"
    root = etree.Element(etree.QName(PNS, "CustomObject"), nsmap={None: PNS})

    etree.SubElement(root, etree.QName(PNS, "label")).text = object_label
    etree.SubElement(root, etree.QName(PNS, "pluralLabel")).text = plural_label
    if description:
        etree.SubElement(root, etree.QName(PNS, "description")).text = description
    etree.SubElement(root, etree.QName(PNS, "sharingModel")).text = sharing_model
    etree.SubElement(root, etree.QName(PNS, "deploymentStatus")).text = deployment_status
    etree.SubElement(root, etree.QName(PNS, "enableActivities")).text = "true"
    etree.SubElement(root, etree.QName(PNS, "enableReports")).text = "true"
    etree.SubElement(root, etree.QName(PNS, "enableSearch")).text = "true"

    # Required name field
    name_field = etree.SubElement(root, etree.QName(PNS, "nameField"))
    etree.SubElement(name_field, etree.QName(PNS, "label")).text = f"{object_label} Name"
    etree.SubElement(name_field, etree.QName(PNS, "type")).text = "Text"

    return etree.tostring(
        root, encoding="UTF-8", xml_declaration=True, pretty_print=True
    ).decode("utf-8")



def _generate_custom_object_with_field(object_name: str, field_config: Dict[str, Any]) -> str:
    PNS = "http://soap.sforce.com/2006/04/metadata"
    root = etree.Element(etree.QName(PNS, "CustomObject"), nsmap={None: PNS})

    is_custom = object_name.endswith("__c")

    # Only include <fullName> for custom objects
    if is_custom:
        etree.SubElement(root, etree.QName(PNS, "fullName")).text = object_name

    # Build <fields> block mirroring CustomField metadata
    f = etree.SubElement(root, etree.QName(PNS, "fields"))
    etree.SubElement(f, etree.QName(PNS, "fullName")).text = field_config["fullName"]             # e.g., Customer_Code__c
    etree.SubElement(f, etree.QName(PNS, "label")).text = field_config["label"]
    etree.SubElement(f, etree.QName(PNS, "type")).text  = field_config["type"]

    # ---- Type-specific attrs ----
    t = field_config["type"]
    if t in {"Text", "LongTextArea"} and "length" in field_config:
        etree.SubElement(f, etree.QName(PNS, "length")).text = str(field_config["length"])
    if t == "LongTextArea":
        if "visibleLines" in field_config:
            etree.SubElement(f, etree.QName(PNS, "visibleLines")).text = str(field_config["visibleLines"])
    if t in {"Number", "Currency", "Percent"}:
        if "precision" in field_config:
            etree.SubElement(f, etree.QName(PNS, "precision")).text = str(field_config["precision"])
        if "scale" in field_config:
            etree.SubElement(f, etree.QName(PNS, "scale")).text = str(field_config["scale"])
    if t in {"Picklist", "MultiselectPicklist"} and field_config.get("picklistValues"):
        vs = etree.SubElement(f, etree.QName(PNS, "valueSet"))
        etree.SubElement(vs, etree.QName(PNS, "restricted")).text = "true"
        vsd = etree.SubElement(vs, etree.QName(PNS, "valueSetDefinition"))
        for pv in field_config["picklistValues"]:
            v = etree.SubElement(vsd, etree.QName(PNS, "value"))
            etree.SubElement(v, etree.QName(PNS, "fullName")).text = pv["fullName"]
            etree.SubElement(v, etree.QName(PNS, "label")).text    = pv.get("label", pv["fullName"])
            etree.SubElement(v, etree.QName(PNS, "default")).text  = str(pv.get("default", False)).lower()
    if t in {"Lookup", "MasterDetail"} and field_config.get("referenceTo"):
        etree.SubElement(f, etree.QName(PNS, "referenceTo")).text = field_config["referenceTo"]
        if "relationshipLabel" in field_config:
            etree.SubElement(f, etree.QName(PNS, "relationshipLabel")).text = field_config["relationshipLabel"]
        if "relationshipName" in field_config:
            etree.SubElement(f, etree.QName(PNS, "relationshipName")).text  = field_config["relationshipName"]
        if t == "MasterDetail" and "deleteConstraint" in field_config:
            etree.SubElement(f, etree.QName(PNS, "deleteConstraint")).text  = field_config["deleteConstraint"]

    # Common optional flags
    for tag in ("required", "unique", "externalId"):
        if tag in field_config:
            etree.SubElement(f, etree.QName(PNS, tag)).text = str(field_config[tag]).lower()
    if field_config.get("description"):
        etree.SubElement(f, etree.QName(PNS, "description")).text = field_config["description"]

    return _pretty_xml(root)


def _pretty_xml(node) -> str:
    """Return pretty-printed XML string with declaration."""
    return etree.tostring(
        node, encoding="UTF-8", xml_declaration=True, pretty_print=True
    ).decode("utf-8")

def _generate_custom_field_xml(field_config: Dict[str, Any]) -> str:
    """Generate <CustomField> XML for a single field."""
    PNS = "http://soap.sforce.com/2006/04/metadata"
    root = etree.Element(etree.QName(PNS, "CustomField"), nsmap={None: PNS})

    # Use just the field name for fullName, not object.field
    etree.SubElement(root, etree.QName(PNS, "fullName")).text = field_config["fullName"]
    etree.SubElement(root, etree.QName(PNS, "label")).text = field_config["label"]
    etree.SubElement(root, etree.QName(PNS, "type")).text = field_config["type"]

    # Length for text fields
    if field_config["type"] in {"Text", "LongTextArea"} and "length" in field_config:
        etree.SubElement(root, etree.QName(PNS, "length")).text = str(field_config["length"])
    
    # Precision/scale for number fields
    if field_config["type"] in {"Number", "Currency", "Percent"}:
        if "precision" in field_config:
            etree.SubElement(root, etree.QName(PNS, "precision")).text = str(field_config["precision"])
        if "scale" in field_config:
            etree.SubElement(root, etree.QName(PNS, "scale")).text = str(field_config["scale"])

    # Optional properties
    for tag in ("defaultValue", "description"):
        if tag in field_config and field_config[tag]:
            etree.SubElement(root, etree.QName(PNS, tag)).text = str(field_config[tag])
    
    # Boolean properties
    for boolean_tag in ("required", "unique", "externalId"):
        if boolean_tag in field_config:
            etree.SubElement(root, etree.QName(PNS, boolean_tag)).text = str(field_config[boolean_tag]).lower()

    # Picklist values
    if field_config["type"] in {"Picklist", "MultiselectPicklist"} and field_config.get("picklistValues"):
        value_set = etree.SubElement(root, etree.QName(PNS, "valueSet"))
        restricted = etree.SubElement(value_set, etree.QName(PNS, "restricted")).text = "true"
        value_set_def = etree.SubElement(value_set, etree.QName(PNS, "valueSetDefinition"))
        
        for pv in field_config["picklistValues"]:
            value = etree.SubElement(value_set_def, etree.QName(PNS, "value"))
            etree.SubElement(value, etree.QName(PNS, "fullName")).text = pv["fullName"]
            etree.SubElement(value, etree.QName(PNS, "label")).text = pv.get("label", pv["fullName"])
            etree.SubElement(value, etree.QName(PNS, "default")).text = str(pv.get("default", False)).lower()

    # Lookup/Master-Detail relationships
    if field_config["type"] in {"Lookup", "MasterDetail"} and field_config.get("referenceTo"):
        etree.SubElement(root, etree.QName(PNS, "referenceTo")).text = field_config["referenceTo"]
        
        if "relationshipLabel" in field_config:
            etree.SubElement(root, etree.QName(PNS, "relationshipLabel")).text = field_config["relationshipLabel"]
        if "relationshipName" in field_config:
            etree.SubElement(root, etree.QName(PNS, "relationshipName")).text = field_config["relationshipName"]

    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, pretty_print=True).decode("utf-8")


def _generate_lwc_meta_xml(component_name: str, description: str = "", api_version: str = "59.0") -> str:
    """Generate the .js-meta.xml file for LWC components."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>{api_version}</apiVersion>
    <isExposed>false</isExposed>
    <description>{description}</description>
    <targets>
        <target>lightning__RecordPage</target>
        <target>lightning__AppPage</target>
        <target>lightning__HomePage</target>
    </targets>
</LightningComponentBundle>"""


# =============================================================================
# METADATA REST – DEPLOY / POLL
# =============================================================================

def _execute_metadata_rest_deploy_multipart(
    sf_connection, zip_buffer: io.BytesIO, check_only: bool = False
) -> Dict[str, Any]:
    """Submit a deployment via the REST Metadata endpoint."""
    endpoint = f"{sf_connection.base_url}metadata/deployRequest"
    headers = {
        "Authorization": f"Bearer {sf_connection.session_id}",
        "Accept": "application/json",
    }
    deploy_opts = {
        "checkOnly": check_only,
        "testLevel": "NoTestRun",
        "singlePackage": True,
        "rollbackOnError": True,
    }
    json_part = json.dumps({"deployOptions": deploy_opts})
    files = {
        "entity_content": (None, json_part, "application/json"),
        "file": ("deploymentPackage.zip", zip_buffer.getvalue(), "application/zip"),
    }

    resp = requests.post(endpoint, headers=headers, files=files, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("id"):
        raise ValueError("Deploy response missing id")
    return data


def _poll_metadata_rest_deploy_status(
    sf_connection,
    async_process_id: str,
    timeout_seconds: int = 300,
    interval_seconds: int = 5,
) -> Dict[str, Any]:
    """Poll deployRequest/{id} until done or timeout."""
    endpoint = f"{sf_connection.base_url}metadata/deployRequest/{async_process_id}"
    headers = {
        "Authorization": f"Bearer {sf_connection.session_id}",
        "Accept": "application/json",
    }

    start = time.time()
    while True:
        if time.time() - start > timeout_seconds:
            return {"success": False, "status": "Timeout"}

        resp = requests.get(endpoint, headers=headers, timeout=45)
        resp.raise_for_status()
        result = resp.json().get("deployResult", {})
        if result.get("done"):
            return {
                "success": result["status"] in {"Succeeded", "SucceededPartial"},
                "status": result["status"],
                "details": result.get("details"),
            }

        logger.info(
            "Deployment %s status: %s (%s/%s components)",
            async_process_id,
            result.get("status"),
            result.get("details", {}).get("numberComponentsDeployed", 0),
            result.get("details", {}).get("numberComponentsTotal", 0),
        )
        time.sleep(interval_seconds)



@register_tool
def get_metadata_deploy_status(job_id: str, include_details: bool = True) -> str:
    """
    Return the status and (optional) component failures/successes for a metadata deploy job.
    """
    try:
        sf = get_salesforce_connection()
        q = "?includeDetails=true" if include_details else ""
        endpoint = f"{sf.base_url}metadata/deployRequest/{job_id}{q}"
        headers = {"Authorization": f"Bearer {sf.session_id}", "Accept": "application/json"}
        r = requests.get(endpoint, headers=headers, timeout=45)
        r.raise_for_status()
        payload = r.json()
        result = payload.get("deployResult", payload)
        return json.dumps({
            "success": result.get("status") in {"Succeeded", "SucceededPartial"},
            "status": result.get("status"),
            "details": result.get("details")
        }, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e), "job_id": job_id}, indent=2)


# =============================================================================
# APEX CLASS TOOLS (ENHANCED WITH CREATE)
# =============================================================================

@register_tool
def fetch_apex_class(class_name: str) -> str:
    """Fetch a single **ApexClass** record (body + metadata) by Name, combining
Tooling and Core API fields into one normalized payload.

What it does:
- Runs a Tooling SOQL to retrieve the class **Body**, `ApiVersion`, `Status`,
  `LengthWithoutComments`, timestamps, and actor IDs.
- Enriches with Core API fields for human-friendly names:
  `CreatedBy.Name`, `LastModifiedBy.Name`, and `NamespacePrefix`.
- Strips Salesforce `attributes` for a cleaner result.
- Returns a JSON string with `"success": true` and a `data` object on success.

Notes & caveats:
- **Uniqueness**: `ApexClass.Name` is unique per namespace; this function expects
  at most one match and returns the first (uses implicit LIMIT via single record).
- **Quoting**: The SOQL interpolates `class_name`. Prefer a helper like
  `soql_quote()` to avoid malformed queries when adapting this function.
- **Read-only**: No updates or deploys are performed here. Use
  `create_apex_class(...)` or `upsert_apex_class(...)` for changes.
- **Dependencies**: This function does not validate any referenced objects,
  fields, or classes within the Body—fetch/verify those separately if needed.

Args:
    class_name (str): Exact Apex class `Name` (DeveloperName), e.g., "InvoiceService".

Returns:
    str: JSON-encoded string.

    # Success
    {
      "success": true,
      "data": {
        "Id": "01p...",
        "Name": "InvoiceService",
        "Body": "public with sharing class InvoiceService { ... }",
        "ApiVersion": 59.0,
        "Status": "Active",
        "LengthWithoutComments": 1234,
        "CreatedDate": "2025-08-01T10:22:33.000+0000",
        "CreatedById": "005...",
        "LastModifiedDate": "2025-08-12T14:55:10.000+0000",
        "LastModifiedById": "005...",
        "CreatedByName": "Jane Admin",
        "LastModifiedByName": "John Dev",
        "NamespacePrefix": null
      }
    }

    # Not found
    {
      "success": false,
      "error": "InvoiceService not found"
    }

Examples:
    # Fetch a class and read its API version
    res = json.loads(fetch_apex_class("InvoiceService"))
    if res["success"]:
        api_ver = res["data"]["ApiVersion"]
        body = res["data"]["Body"]
"""

    try:
        sf = get_salesforce_connection()

        tooling_q = (
            "SELECT Id, Name, Body, ApiVersion, Status, LengthWithoutComments, "
            "CreatedDate, CreatedById, LastModifiedDate, LastModifiedById "
            f"FROM ApexClass WHERE Name = '{class_name}'"
        )
        tooling_res = sf.toolingexecute(f"query/?q={tooling_q}")
        if tooling_res.get("size") == 0:
            return json.dumps({"success": False, "error": f"{class_name} not found"}, indent=2)
        apex = tooling_res["records"][0]

        # add CreatedBy / LastModifiedBy names
        core_q = (
            "SELECT Id, NamespacePrefix, CreatedBy.Name, LastModifiedBy.Name "
            f"FROM ApexClass WHERE Name = '{class_name}'"
        )
        core_res = sf.query(core_q)
        if core_res.get("records"):
            extra = core_res["records"][0]
            apex["CreatedByName"] = extra["CreatedBy"]["Name"]
            apex["LastModifiedByName"] = extra["LastModifiedBy"]["Name"]
            apex["NamespacePrefix"] = extra["NamespacePrefix"]

        apex.pop("attributes", None)

        return json.dumps({"success": True, "data": apex}, indent=2)

    except Exception as e:
        logger.error("fetch_apex_class: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@register_tool
def create_apex_class(
    class_name: str, body: str, api_version: Optional[float] = None, description: str = ""
) -> str:
    """Create a **new Apex class** with strict preflight checks and name-uniqueness
enforcement.

This tool **only creates** a class. It fails fast if any class with the same
`Name` already exists. To avoid broken deployments, follow the preflight
checklist below.

Built-in safeguards:
- **Uniqueness check**: Queries `ApexClass` by `Name`; if found, returns an error.
- **Name validation**: Allows only letters, numbers, and underscores; must not be
  empty. (Best practice: start with a letter; avoid double underscores.)
- **API version**: Uses the provided `api_version`; otherwise defaults to the
  org’s version if available (or `"59.0"` as a fallback).

Preflight checklist (caller responsibility — “no hypothetical names”):
1) **Do not invent schema or class names**  
   Every object/field/class you reference in `body` should already exist:
   - Objects: `fetch_object_metadata("Invoice__c")` → expect `success: true`.
   - Fields:  `fetch_custom_field("Invoice__c", "Customer_Code__c")` → expect `success: true`.
   - Cross-class calls: confirm the target class exists:
       execute_soql_query(
         "SELECT Id FROM ApexClass WHERE Name = 'ContactService' LIMIT 1",
         use_tooling_api=True
       )
   If any proof is missing, create/repair those assets first or adjust the class.

2) **Optional SOQL/static sanity** (helpful on large classes)  
   If your code embeds SOQL, you can regex-scan the strings for object/field
   tokens and verify them with the helpers above before deploying.

3) **Tests & coverage**  
   This call does not run tests by itself. If your pipeline requires coverage,
   ensure related test classes are present and passing.

What this function does:
- Verifies the class **does not exist** (by `Name`) and validates `class_name`.
- Builds a deploy payload (`files = {"apex": body}`) and deploys via
  `deploy_apex_class_internal(...)` using the chosen API version.
- Returns a normalized JSON summary.

Args:
    class_name (str): Target Apex class `Name`/DeveloperName (e.g., "InvoiceService").
    body (str):       Full Apex source code.
    api_version (Optional[float]): API version to compile against. If None, defaults
                    to the org’s version when available, else "59.0".
    description (str): (Reserved for future use by your internals; not persisted by
                       this deploy helper unless your implementation uses it.)

Returns:
    str: JSON-encoded string.

    # Success
    {
      "success": true,
      "operation": "create_apex_class",
      "class_name": "InvoiceService",
      "api_version": 59.0,
      "job_id": "<deploy-id>",
      "message": "Successfully created Apex class 'InvoiceService'",
      "errors": null
    }

    # Already exists
    {
      "success": false,
      "error": "Apex class 'InvoiceService' already exists. Use upsert_apex_class to update it."
    }

    # Invalid name / deploy errors
    {
      "success": false,
      "error": "Invalid class name. Use only alphanumeric characters and underscores."
    }
    {
      "success": false,
      "operation": "create_apex_class",
      "class_name": "InvoiceService",
      "api_version": 59.0,
      "job_id": "<deploy-id>",
      "message": "Failed to create Apex class 'InvoiceService'",
      "errors": { ...compiler/metadata diagnostics... }
    }

Example: safe create flow (with proofs)
---------------------------------------
# 1) Ensure no existing class with same name
assert json.loads(execute_soql_query(
    "SELECT Id FROM ApexClass WHERE Name = 'InvoiceService' LIMIT 1",
    use_tooling_api=True
))["totalSize"] == 0

# 2) Prove schema exists for references used in your class
assert json.loads(fetch_object_metadata("Invoice__c"))["success"]
assert json.loads(fetch_custom_field("Invoice__c", "Customer_Code__c"))["success"]

# 3) Provide full class body
body = '''
public with sharing class InvoiceService {
    public static String ping() { return 'ok'; }
}
'''

# 4) Create
res = json.loads(create_apex_class("InvoiceService", body, 59.0))
assert res["success"], res
"""

    try:
        sf = get_salesforce_connection()
        
        # Check if class already exists
        check = sf.query(f"SELECT Id FROM ApexClass WHERE Name = '{class_name}'")
        if check["totalSize"] > 0:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Apex class '{class_name}' already exists. Use upsert_apex_class to update it.",
                },
                indent=2,
            )

        # Validate class name
        if not class_name.replace("_", "").isalnum():
            return json.dumps(
                {"success": False, "error": "Invalid class name. Use only alphanumeric characters and underscores."},
                indent=2,
            )

        if api_version is None:
            api_version = "59.0"

        files = {"apex": body}
        res = deploy_apex_class_internal(sf, class_name, files, str(api_version))
        
        return json.dumps({
            "success": res.get("success", False),
            "operation": "create_apex_class",
            "class_name": class_name,
            "api_version": api_version,
            "message": f"Successfully created Apex class '{class_name}'" if res.get("success") else f"Failed to create Apex class '{class_name}'",
            "job_id": res.get("job_id"),
            "errors": res.get("details") if not res.get("success") else None
        }, indent=2)

    except Exception as e:
        logger.error("create_apex_class: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@register_tool
def upsert_apex_class(
    class_name: str, body: str, api_version: Optional[float] = None
) -> str:
    """Update an existing **Apex class** with a context-first, schema-safe workflow.

This tool **only updates** an existing class. It preserves the current API
version unless you pass `api_version`. To prevent breaking changes, follow the
preflight checklist below so you don’t push code that references *nonexistent*
objects/fields or invented names.

Preflight checklist (caller responsibility):
1) **Fetch current class first (know the context)**
   - Get the live body + metadata so you diff consciously and avoid accidental
     removals.
   - Example:
       cur = json.loads(execute_soql_query(
           "SELECT Id, Name, ApiVersion, Body FROM ApexClass WHERE Name = 'InvoiceService' LIMIT 1",
           use_tooling_api=True
       ))
       assert cur["totalSize"] == 1, "Class not found—use create_apex_class()"
       old_body = cur["records"][0]["Body"]

2) **No hypothetical names**
   - Do **not** invent object, field, or class API names. Every name you add to
     `body` must already exist (unless your change also creates it in the same
     deploy package—this function does not).
   - For any sObject or field you reference, gather **proof**:
       # Object proof
       obj = json.loads(fetch_object_metadata("Invoice__c"))
       assert obj["success"], "Invoice__c object missing"
       # Field proof
       fld = json.loads(fetch_custom_field("Invoice__c", "Customer_Code__c"))
       assert fld["success"], "Customer_Code__c field missing"

   - For cross-class calls, verify the target class exists:
       ac = json.loads(execute_soql_query(
           "SELECT Id FROM ApexClass WHERE Name = 'ContactService' LIMIT 1",
           use_tooling_api=True
       ))
       assert ac["totalSize"] == 1, "ContactService class missing"

3) **API version sanity**
   - If you omit `api_version`, this tool uses the class’s current `ApiVersion`.
     Change it deliberately if you need newer language/runtime features.

What this function does:
- Confirms the class exists (queries `ApexClass` for `Id` & `ApiVersion`).
- Uses existing `ApiVersion` if none is provided.
- Deploys the new `body` via `deploy_apex_class_internal(...)`.
- Returns a normalized JSON summary.

Args:
    class_name (str): The Apex class Name (DeveloperName).
    body (str):       Full Apex source to deploy.
    api_version (Optional[float]): Target API version. If None, keeps current.

Returns:
    str: JSON-encoded string.

    # Success
    {
      "success": true,
      "operation": "update_apex_class",
      "class_name": "InvoiceService",
      "api_version": 59.0,
      "job_id": "<deploy-id>",
      "message": "Successfully updated Apex class 'InvoiceService'",
      "errors": null
    }

    # Not found
    {
      "success": false,
      "error": "InvoiceService not found (use create_apex_class to create new classes)"
    }

    # Deploy error
    {
      "success": false,
      "operation": "update_apex_class",
      "class_name": "InvoiceService",
      "api_version": 59.0,
      "job_id": "<deploy-id>",
      "message": "Failed to update Apex class 'InvoiceService'",
      "errors": { ...metadata diagnostics... }
    }

Example: safe update flow
-------------------------
# 1) Fetch the current class to understand context
live = json.loads(execute_soql_query(
    "SELECT Id, Name, ApiVersion, Body FROM ApexClass WHERE Name = 'InvoiceService' LIMIT 1",
    use_tooling_api=True
))
assert live["totalSize"] == 1
api_ver = live["records"][0]["ApiVersion"]
old_body = live["records"][0]["Body"]

# 2) Prove all schema references exist (object + field)
assert json.loads(fetch_object_metadata("Invoice__c"))["success"]
assert json.loads(fetch_custom_field("Invoice__c", "Customer_Code__c"))["success"]

# 3) Prepare your new body (diff against old_body in your editor/tooling)
new_body = old_body.replace("/* Todo */", "/* implemented */")

# 4) Update
result = json.loads(upsert_apex_class("InvoiceService", new_body, api_ver))
assert result["success"], result

Implementation notes:
- This function does not parse SOQL/DML to auto-validate schema; do the explicit
  proofs shown above. If you want stricter guardrails, add a pre-check that:
  - regex-scans SOQL strings for object/field tokens, and
  - calls `fetch_object_metadata` / `fetch_custom_field` for each token.
"""
    try:
        sf = get_salesforce_connection()
        check = sf.query(f"SELECT Id, ApiVersion FROM ApexClass WHERE Name = '{class_name}'")
        if check["totalSize"] == 0:
            return json.dumps(
                {
                    "success": False,
                    "error": f"{class_name} not found (use create_apex_class to create new classes)",
                },
                indent=2,
            )

        if api_version is None:
            api_version = check["records"][0]["ApiVersion"]

        files = {"apex": body}
        res = deploy_apex_class_internal(sf, class_name, files, str(api_version))
        
        return json.dumps({
            "success": res.get("success", False),
            "operation": "update_apex_class",
            "class_name": class_name,
            "api_version": api_version,
            "message": f"Successfully updated Apex class '{class_name}'" if res.get("success") else f"Failed to update Apex class '{class_name}'",
            "job_id": res.get("job_id"),
            "errors": res.get("details") if not res.get("success") else None
        }, indent=2)

    except Exception as e:
        logger.error("upsert_apex_class: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


# =============================================================================
# LWC TOOLS (ENHANCED WITH CREATE)
# =============================================================================

@register_tool
def fetch_lwc_component(component_name: str) -> str:
    """Fetch an LWC bundle (metadata + source files) robustly via the Tooling API.

This is a **read-only** utility that:
- Looks up the `LightningComponentBundle` by `DeveloperName`.
- First **describes** the object to detect which optional columns exist in this org
  (e.g., `Targets`, `TargetConfigs`, `LwcResources`) so the query won’t break on
  older/newer API versions.
- Retrieves **all resources** (`LightningComponentResource`) for the bundle and
  collates them into a friendly `files` map:
    - `html` → the component’s `.html`
    - `js`   → the component’s `.js` (excluding `*-meta.xml`)
    - `xml`  → the component’s `*-meta.xml`
    - `css`  → the component’s `.css` (if present)
    - `svg`  → the component’s `.svg` (if present)
    - any other assets are added under their file name

It removes Salesforce `attributes` keys from the returned records to keep payloads clean.

Notes & caveats:
- **Read-only**: This does not create or update anything.
- **Output stability**: If a file isn’t present in the bundle, its key is simply
  omitted from `files`. Do not assume `html`, `js`, or `xml` always exist.
- **Namespace**: The `bundle` includes `NamespacePrefix` (if any).
- **Quoting**: The query interpolates `component_name`; if you adapt this function,
  use a small helper like `soql_quote()` to avoid malformed queries.

Args:
    component_name (str): The bundle `DeveloperName`, e.g., "accountHeader".

Returns:
    str: JSON-encoded string.

    # Success
    {
      "success": true,
      "bundle": {
        "Id": "0Rb...AAA",
        "DeveloperName": "accountHeader",
        "NamespacePrefix": null,
        "Description": "....",
        "MasterLabel": "Account Header",
        "ApiVersion": 59.0,
        "IsExposed": true,
        "CreatedDate": "2025-08-01T10:22:33.000+0000",
        "CreatedById": "005...",
        "LastModifiedDate": "2025-08-12T14:55:10.000+0000",
        "LastModifiedById": "005...",
        // Optional fields like Targets/TargetConfigs may be present if supported
      },
      "files": {
        "html": "<template>...</template>",
        "js": "import { LightningElement } from 'lwc'; ...",
        "xml": "<?xml version=...><LightningComponentBundle>...</LightningComponentBundle>",
        "css": ".cls { ... }",
        "svg": "<svg .../>"
        // any other assets by filename
      }
    }

    # Not found
    {
      "success": false,
      "error": "Component not found"
    }

Examples:
    # Basic fetch
    res = json.loads(fetch_lwc_component("accountHeader"))
    if res["success"]:
        html = res["files"].get("html", "")
        js   = res["files"].get("js", "")
        xml  = res["files"].get("xml", "")

    # Guarded update flow (read → modify → write)
    fetched = json.loads(fetch_lwc_component("accountHeader"))
    assert fetched["success"]
    files = fetched["files"]
    files["html"] = files["html"].replace("Old Title", "New Title")
    # then pass to your upsert/update tool
"""

    try:
        sf = get_salesforce_connection()

        # Describe LightningComponentBundle to know optional fields
        try:
            describe = sf.toolingexecute("sobjects/LightningComponentBundle/describe/")
            available = {f["name"] for f in describe.get("fields", [])}
        except Exception:
            available = set()

        base = [
            "Id",
            "DeveloperName",
            "NamespacePrefix",
            "Description",
            "MasterLabel",
            "ApiVersion",
            "IsExposed",
            "CreatedDate",
            "CreatedById",
            "LastModifiedDate",
            "LastModifiedById",
        ]
        optional = [f for f in ("Targets", "TargetConfigs", "LwcResources") if f in available]
        query_fields = ", ".join(base + optional)
        bundle_q = (
            f"SELECT {query_fields} FROM LightningComponentBundle "
            f"WHERE DeveloperName = '{component_name}' LIMIT 1"
        )

        bundle_res = sf.toolingexecute(f"query/?q={bundle_q}")
        if bundle_res.get("size") == 0:
            return json.dumps({"success": False, "error": "Component not found"}, indent=2)
        bundle = bundle_res["records"][0]
        bundle_id = bundle["Id"]

        # Resources
        res_q = (
            "SELECT Id, FilePath, Format, Source, CreatedDate, LastModifiedDate "
            f"FROM LightningComponentResource WHERE LightningComponentBundleId = '{bundle_id}'"
        )
        res = sf.toolingexecute(f"query/?q={res_q}")

        files = {}
        for r in res.get("records", []):
            path = r["FilePath"]
            source = r["Source"]
            name = path.split("/")[-1]
            ext = name.split(".")[-1].lower()
            if ext == "html":
                files["html"] = source
            elif ext == "js" and not name.endswith(".js-meta.xml"):
                files["js"] = source
            elif name.endswith(".js-meta.xml"):
                files["xml"] = source
            elif ext == "css":
                files["css"] = source
            elif ext == "svg":
                files["svg"] = source
            else:
                files[name] = source

        bundle.pop("attributes", None)
        return json.dumps(
            {"success": True, "bundle": bundle, "files": files},
            indent=2,
        )

    except Exception as e:
        logger.error("fetch_lwc_component: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@register_tool
def create_lwc_component(
    component_name: str,
    html_content: str = "",
    js_content: str = "",
    css_content: str = ""
) -> str:
    """Create a **new Lightning Web Component** (LWC) bundle using exactly four inputs:
    (name, html, js, css). The meta XML is generated inside the function with
    App/Home/Record page targets enabled by default.

    What this does:
    - **Creation-only**: Fails if a bundle with the same DeveloperName already exists.
    - **Name validation**: Letters, numbers, underscores, and hyphens only.
    - **Auto API version**: Uses the org’s API version (`sf.sf_version`) if available,
      else defaults to "59.0".
    - **Generates meta XML** internally with <isExposed>true</isExposed> and
      targets: AppPage, HomePage, RecordPage.
    - **Optional defaults**: If `html_content` or `js_content` are blank, generates
      minimal starter files (so callers can simply pass empty strings).

    Safety (best practice):
    # ---- Name validation (LWC bundle) ----
    if not _validate_lwc_bundle_name(component_name):
        return json.dumps({
            "success": False,
            "error": "Invalid LWC bundle name. Must start with a lowercase letter and contain only letters, numbers, or underscores."
        }, indent=2)
    - **No hypothetical dependencies**: If your JS imports Apex
      (`@salesforce/apex/Class.method`) or references objects/fields, verify those
      exist separately before you deploy. This function does not auto-create or
      guarantee external dependencies.

    Args:
        component_name (str): LWC bundle DeveloperName, e.g., "accountHeader".
        html_content (str):  Component <template> source. If empty, a starter template
                            is generated.
        js_content (str):    Component ES module. If empty, a minimal class stub is
                            generated.
        css_content (str):   Optional stylesheet contents. If empty, no CSS file is
                            created.

    Returns:
        str: JSON-encoded string.

        # Success
        {
          "success": true,
          "operation": "create_lwc_component",
          "component_name": "accountHeader",
          "api_version": "59.0",
          "files_created": ["html","js","xml","css"],  // css omitted if not provided
          "job_id": "<deploy-id>",
          "message": "Successfully created LWC component 'accountHeader'",
          "errors": null
        }

        # Already exists
        {
          "success": false,
          "error": "LWC component 'accountHeader' already exists. Use upsert_lwc_component to update it."
        }

        # Invalid name / other errors
        {
          "success": false,
          "error": "Invalid component name. Use only alphanumeric characters, underscores, and hyphens."
        }

    Example:
        # Minimal creation with auto-generated HTML/JS and default targets in XML
        create_lwc_component("accountHeader")

        # Custom HTML/JS; no CSS
        html = \"\"\"<template>
          <div class="accountHeader"><h2>Accounts</h2></div>
        </template>\"\"\"
        js = \"\"\"import { LightningElement } from 'lwc';
        export default class AccountHeader extends LightningElement {}\"\"\"
        create_lwc_component("accountHeader", html, js)

        # With CSS (4th arg)
        css = ".accountHeader { padding: .5rem; }"
        create_lwc_component("accountHeader", html, js, css)
    """
    try:
        sf = get_salesforce_connection()

        # ---- Validate component name ----
        if not component_name or not component_name.replace("_", "").replace("-", "").isalnum():
            return json.dumps(
                {"success": False, "error": "Invalid component name. Use only alphanumeric characters, underscores, and hyphens."},
                indent=2
            )

        # ---- Existence check (Tooling) ----
        try:
            tooling_query = f"SELECT Id FROM LightningComponentBundle WHERE DeveloperName = '{component_name}'"
            exists = sf.toolingexecute(f"query/?q={tooling_query}")
            if exists.get("size", 0) > 0:
                return json.dumps(
                    {"success": False, "error": f"LWC component '{component_name}' already exists. Use upsert_lwc_component to update it."},
                    indent=2
                )
        except Exception as tooling_error:
            # If Tooling API fails, continue; the deploy will fail if it truly exists.
            logger.warning(f"Tooling API check failed: {tooling_error}. Proceeding with deployment.")

        # ---- Determine API version ----
        api_version = getattr(sf, "sf_version", "59.0")

        # ---- Generate defaults if caller passed blank content ----
        if not html_content.strip():
            html_content = f"""<template>
  <div class="{component_name}">
    <h1>Hello from {component_name}!</h1>
    <p>This is a new Lightning Web Component.</p>
  </div>
</template>"""

        if not js_content.strip():
            # Convert to a valid class name (simple, safe transform)
            safe_class = "".join(ch if ch.isalnum() else "_" for ch in component_name)
            if safe_class and safe_class[0].isdigit():
                safe_class = "_" + safe_class
            js_content = f"""import {{ LightningElement }} from 'lwc';
export default class {safe_class} extends LightningElement {{}}
"""

        # ---- Predefined meta XML: App/Home/Record enabled ----
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">
  <apiVersion>{api_version}</apiVersion>
  <isExposed>true</isExposed>
  <targets>
    <target>lightning__AppPage</target>
    <target>lightning__HomePage</target>
    <target>lightning__RecordPage</target>
  </targets>
</LightningComponentBundle>"""

        # ---- Files payload ----
        files = {
            "html": html_content,
            "js": js_content,
            "xml": xml_content
        }
        if css_content and css_content.strip():
            files["css"] = css_content

        # ---- Deploy ----
        res = deploy_lwc_component_internal(sf, component_name, files)

        return json.dumps({
            "success": res.get("success", False),
            "operation": "create_lwc_component",
            "component_name": component_name,
            "api_version": api_version,
            "files_created": list(files.keys()),
            "message": f"Successfully created LWC component '{component_name}'" if res.get("success") else f"Failed to create LWC component '{component_name}'",
            "job_id": res.get("job_id"),
            "errors": res.get("details") if not res.get("success") else None
        }, indent=2)

    except Exception as e:
        logger.error("create_lwc_component: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@register_tool
def upsert_lwc_component(
    component_name: str,
    html_content: str,
    js_content: str,
    css_content: str = ""
) -> str:
    """Update an existing **LWC bundle** using four inputs: name, HTML, JS, CSS.
    The component’s meta XML is auto-generated inside this function with
    App/Home/Record page targets enabled by default.

    Safety & preflight (built-in + caller responsibilities):
    # ---- Name validation (LWC bundle) ----
    if not _validate_lwc_bundle_name(component_name):
        return json.dumps({
            "success": False,
            "error": "Invalid LWC bundle name. Must start with a lowercase letter and contain only letters, numbers, or underscores."
        }, indent=2)
    - **Existence check (built-in):** Verifies the bundle exists via Tooling API.
      This function does not create new bundles.
    - **Apex import verification (built-in, best-effort):** Scans JS for
      `@salesforce/apex/Class.method` imports and verifies that the Apex class
      exists and appears to expose that method (looks for the method name and an
      `@AuraEnabled` annotation). Fails fast if a reference looks invalid.
    - **No hypothetical names (caller note):** If your JS/HTML references objects
      or fields (e.g., via wire adapters/UI-API), verify them separately using
      `fetch_object_metadata` / `fetch_custom_field`. This function can’t fully
      validate schema references embedded in JS templates.
    - **Required inputs:** `html_content` and `js_content` are mandatory. `css_content`
      is optional; if omitted, the bundle’s existing CSS (if any) is untouched.

    Args:
        component_name (str): LWC bundle DeveloperName (e.g., "accountHeader").
                              Must already exist.
        html_content (str):   Full `<template>` source for the component.
        js_content (str):     Full ES module for the component class. If importing
                              Apex, ensure the class/method actually exists & is
                              `@AuraEnabled`.
        css_content (str):    Optional stylesheet contents.

    Returns:
        str: JSON-encoded string.

        # Success
        {
          "success": true,
          "operation": "update_lwc_component",
          "component_name": "accountHeader",
          "files_updated": ["html","js","xml","css"],  // css omitted if not provided
          "job_id": "<deploy-id>",
          "message": "Successfully updated LWC component 'accountHeader'",
          "errors": null
        }

        # Typical failures
        {
          "success": false,
          "error": "Component not found (use create_lwc_component to create new components)"
        }
        {
          "success": false,
          "error": "Missing required file content: html"
        }
        {
          "success": false,
          "error": "Apex reference check failed",
          "details": ["Apex class 'ContactService' not found", ...]
        }

    Example:
        html = \"\"\"<template>
          <div class="accountHeader">
            <h2>Accounts</h2>
          </div>
        </template>\"\"\"

        js = \"\"\"import { LightningElement, wire } from 'lwc';
        // If you add an Apex import like the next line, make sure the class/method exist:
        // import getTopContacts from '@salesforce/apex/ContactService.getTopContacts';
        export default class AccountHeader extends LightningElement {}\"\"\"

        css = \".accountHeader { padding: 0.5rem; }\"  # optional

        upsert_lwc_component("accountHeader", html, js, css)
    """
    import re
    try:
        sf = get_salesforce_connection()

        # ---- Validate component name (keep parity with your create() rules) ----
        if not component_name or not component_name.replace("_", "").replace("-", "").isalnum():
            return json.dumps(
                {"success": False, "error": "Invalid component name. Use only letters, numbers, underscores, and hyphens."},
                indent=2
            )

        # ---- Must provide HTML & JS ----
        if not html_content or not html_content.strip():
            return json.dumps({"success": False, "error": "Missing required file content: html"}, indent=2)
        if not js_content or not js_content.strip():
            return json.dumps({"success": False, "error": "Missing required file content: js"}, indent=2)

        # ---- Ensure bundle exists ----
        try:
            tooling_query = f"SELECT Id FROM LightningComponentBundle WHERE DeveloperName = '{component_name}'"
            exists = sf.toolingexecute(f"query/?q={tooling_query}")
            if exists.get("size", 0) == 0:
                return json.dumps(
                    {"success": False, "error": "Component not found (use create_lwc_component to create new components)"},
                    indent=2
                )
            bundle_id = exists["records"][0]["Id"]
        except Exception as tooling_error:
            logger.warning(f"Tooling API existence check failed: {tooling_error}")
            # If we can't verify existence, better to fail closed than overwrite wrong bundle
            return json.dumps({"success": False, "error": "Unable to verify LWC existence via Tooling API"}, indent=2)

        # ---- Fetch current bundle (read-only) to help callers diff locally if needed ----
        try:
            res_q = (
                "SELECT Id, FilePath, Format, Source "
                f"FROM LightningComponentResource WHERE LightningComponentBundleId = '{bundle_id}'"
            )
            res = sf.toolingexecute(f"query/?q={res_q}")
            current_files = {}
            for r in res.get("records", []):
                name = r["FilePath"].split("/")[-1]
                ext = name.split(".")[-1].lower()
                if ext == "html":
                    current_files["html"] = r.get("Source", "")
                elif ext == "js" and not name.endswith(".js-meta.xml"):
                    current_files["js"] = r.get("Source", "")
                elif name.endswith(".js-meta.xml"):
                    current_files["xml"] = r.get("Source", "")
                elif ext == "css":
                    current_files["css"] = r.get("Source", "")
                elif ext == "svg":
                    current_files["svg"] = r.get("Source", "")
        except Exception as fetch_err:
            logger.warning(f"Fetch existing LWC resources failed: {fetch_err}")

        # ---- Best-effort Apex import verification from JS ----
        try:
            apex_refs = re.findall(
                r"@salesforce/apex/([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
                js_content
            )
            apex_errors = []
            for cls, method in apex_refs:
                a = sf.toolingexecute(
                    "query/?q=" +
                    f"SELECT Id, Name, Body FROM ApexClass WHERE Name = '{cls}' LIMIT 1"
                )
                if a.get("size", 0) == 0:
                    apex_errors.append(f"Apex class '{cls}' not found")
                    continue
                body = a["records"][0].get("Body", "") or ""
                # Heuristic: method name present and '@AuraEnabled' present in class body
                if method not in body or "@AuraEnabled" not in body:
                    apex_errors.append(f"Apex method '{cls}.{method}' not found or not @AuraEnabled")
            if apex_errors:
                return json.dumps(
                    {"success": False, "error": "Apex reference check failed", "details": apex_errors},
                    indent=2
                )
        except Exception as v_err:
            logger.warning(f"Apex reference precheck failed (continuing): {v_err}")

        # ---- Predefined meta XML (App/Home/Record enabled by default) ----
        api_ver = getattr(sf, "sf_version", "59.0")
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">
  <apiVersion>{api_ver}</apiVersion>
  <isExposed>true</isExposed>
  <targets>
    <target>lightning__AppPage</target>
    <target>lightning__HomePage</target>
    <target>lightning__RecordPage</target>
  </targets>
</LightningComponentBundle>"""

        # ---- Build payload for deployment ----
        files = {
            "html": html_content,
            "js": js_content,
            "xml": xml_content
        }
        if css_content and css_content.strip():
            files["css"] = css_content

        # ---- Deploy update ----
        res = deploy_lwc_component_internal(sf, component_name, files)

        return json.dumps(
            {
                "success": res.get("success", False),
                "operation": "update_lwc_component",
                "component_name": component_name,
                "files_updated": list(files.keys()),
                "message": (
                    f"Successfully updated LWC component '{component_name}'"
                    if res.get("success") else
                    f"Failed to update LWC component '{component_name}'"
                ),
                "job_id": res.get("job_id"),
                "errors": res.get("details") if not res.get("success") else None
            },
            indent=2
        )

    except Exception as e:
        logger.error("upsert_lwc_component: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)




# =============================================================================
# CUSTOM OBJECT TOOLS
# =============================================================================

@register_tool
def fetch_object_metadata(object_name: str) -> str:
    """Return describe() + record type info for any object."""
    try:
        sf = get_salesforce_connection()
        desc = getattr(sf, object_name).describe()
    except Exception:
        return json.dumps({"success": False, "error": f"{object_name} not found"}, indent=2)

    fields = []
    for f in desc["fields"]:
        fd = {
            "name": f["name"],
            "label": f["label"],
            "type": f["type"],
            "required": not f.get("nillable", True),
            "custom": f.get("custom", False),
        }
        if f["type"] in {"text", "textarea"}:
            fd["length"] = f.get("length")
        if f["type"] == "number":
            fd["precision"] = f.get("precision")
            fd["scale"] = f.get("scale")
        if f["type"] == "reference":
            fd["referenceTo"] = f.get("referenceTo", [])
            fd["relationshipName"] = f.get("relationshipName")
        fields.append(fd)

    record_types = []
    try:
        rts = sf.query(
            f"SELECT Id, Name, DeveloperName, IsActive FROM RecordType WHERE SobjectType = '{object_name}'"
        )
        record_types = [
            {
                "id": rt["Id"],
                "name": rt["Name"],
                "developerName": rt["DeveloperName"],
                "isActive": rt["IsActive"],
            }
            for rt in rts.get("records", [])
        ]
    except Exception:
        pass

    return json.dumps(
        {
            "success": True,
            "objectName": object_name,
            "label": desc["label"],
            "isCustom": desc["custom"],
            "totalFields": len(fields),
            "fields": fields,
            "recordTypes": record_types,
        },
        indent=2,
    )


@register_tool
def upsert_custom_object(
    object_name: str,
    label: str,
    plural_label: str,
    description: str = "",
    sharing_model: str = "ReadWrite",
) -> str:
    """Create or update a **custom object** via the Salesforce Metadata REST API.

What this does:
- **Normalizes & validates the API name**: If `object_name` does not end with
  `__c`, it is automatically suffixed with `__c` (this tool is for **custom**
  objects only). The remaining part must be alphanumeric/underscores.
- **Builds metadata**: Uses `_generate_custom_object_xml(...)` to produce an
  `.object` metadata file with the provided `label`, `plural_label`, optional
  `description`, and `sharing_model`.
- **Packages & deploys**: Creates a zip containing:
    - `package.xml` (with a `CustomObject` member)
    - `objects/<Object__c>.object` (the metadata XML)
  Submits it via `_execute_metadata_rest_deploy_multipart(...)` and polls
  completion with `_poll_metadata_rest_deploy_status(...)`.
- **Upsert semantics**: If the object does not exist, it is **created**. If it
  exists, only the properties represented in the generated XML are **updated**.
  (This tool intentionally does not touch fields, layouts, record types, or FLS.)

Notes & caveats:
- **Custom objects only**: Standard objects (e.g., `Account`) are not supported.
  Passing a standard name will be normalized to `Account__c` and fail validation.
- **Naming**: No guessing or renaming beyond appending `__c`. The base name must
  be letters/numbers/underscores and start with a letter. (Salesforce applies
  additional platform limits; if violated, the deploy will fail and surface
  errors in `details`.)
- **Sharing model**: Common values include `"ReadWrite"`, `"Private"`,
  `"ControlledByParent"`. Your `_generate_custom_object_xml` must map these
  correctly to `<sharingModel>`.
- **Scope**: This only manages the **object container**. Create fields separately
  (e.g., with `upsert_custom_field`). This call does not assign permissions,
  tabs, or layouts.
- **Deployment mode**: This implementation performs a **full deploy** (not
  check-only first). If you want a validation-first pattern, wrap this call in
  a preflight deploy as you do for fields.

Args:
    object_name (str):
        The custom object API name or base name (e.g., `"Invoice__c"` or
        `"Invoice"`). If it doesn’t end with `__c`, the function appends it.
    label (str):
        The object’s singular label (e.g., `"Invoice"`).
    plural_label (str):
        The object’s plural label (e.g., `"Invoices"`).
    description (str, optional):
        A human-readable description for admins. Defaults to `""`.
    sharing_model (str, optional):
        Object sharing model. Typical values: `"ReadWrite"`, `"Private"`,
        `"ControlledByParent"`. Defaults to `"ReadWrite"`.

Returns:
    str: JSON-encoded string with deployment outcome.

    # Success
    {
      "success": true,
      "job_id": "<deploy-id>",
      "status": "Succeeded",
      "details": null
    }

    # Failure (e.g., invalid name, deploy error)
    {
      "success": false,
      "error": "Invalid object name"  // or a thrown error message
      // When a deploy was attempted:
      "job_id": "<deploy-id>",
      "status": "Failed",
      "details": { ...metadata error diagnostics... }
    }

Examples:
    # 1) Create a new custom object
    upsert_custom_object(
        object_name="Invoice",
        label="Invoice",
        plural_label="Invoices",
        description="Stores billing invoices raised to customers",
        sharing_model="Private"
    )

    # 2) Update an existing custom object’s labels & sharing
    upsert_custom_object(
        object_name="Project__c",
        label="Project",
        plural_label="Projects",
        description="Active/internal projects",
        sharing_model="ReadWrite"
    )
"""


    try:
        sf = get_salesforce_connection()
        if not object_name.endswith("__c"):
            object_name += "__c"
        if not object_name[:-3].replace("_", "").isalnum():
            return json.dumps(
                {"success": False, "error": "Invalid object name"}, indent=2
            )

        # Build the XML
        custom_object_xml = _generate_custom_object_xml(
            label, plural_label, description, sharing_model
        )

        api_ver = getattr(sf, "sf_version", "59.0")
        pkg_xml = _generate_package_xml([object_name], "CustomObject", api_ver)

        # Zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("package.xml", pkg_xml)
            z.writestr(f"objects/{object_name}.object", custom_object_xml)
        buf.seek(0)

        dep = _execute_metadata_rest_deploy_multipart(sf, buf)
        status = _poll_metadata_rest_deploy_status(sf, dep["id"])

        return json.dumps(
            {
                "success": status["success"],
                "job_id": dep["id"],
                "status": status["status"],
                "details": status.get("details"),
            },
            indent=2,
        )
    except Exception as e:
        logger.error("upsert_custom_object: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


# =============================================================================
# CUSTOM FIELD TOOLS
# =============================================================================

@register_tool
def fetch_custom_field(object_name: str, field_name: str) -> str:
    """Fetch detailed metadata for a single field by combining **Core Describe**
and **Tooling API** information.

What it does:
- Uses `sObject.describe()` to return the runtime field definition as seen by
  the Core API (label, type, length/precision, required, reference targets,
  compound info, picklist settings, etc.).
- Queries Tooling `FieldDefinition` to enrich with static metadata such as
  `DurableId`, `DataType`, `Precision`, `Scale`, and `Length`.
- Returns a normalized JSON string with two sections:
  - `field`: the Core Describe field dict (with `attributes` stripped)
  - `extra`: the first matching Tooling `FieldDefinition` row (if any)

Notes & caveats:
- **API names only**: `object_name` and `field_name` must be API names
  (e.g., `Invoice__c`, `Customer_Code__c`). No guessing or discovery.
- **Visibility/FLS**: Describe results can be affected by the running user’s
  permissions. If the user cannot see the field, it may appear missing.
- **Compound fields**: For compound types (e.g., Address, Name), Describe may
  expose sub-fields via `fields` list with `compoundFieldName`. Tooling
  `FieldDefinition` rows often exist per particle (e.g., `BillingStreet`).
- **Picklists**: This function does not expand full value sets. Use a separate
  helper if you need the concrete picklist values.
- **Safety**: The Tooling SOQL uses string interpolation. Prefer a small
  quoting helper (e.g., `soql_quote()`) to avoid broken queries.

Args:
    object_name (str): The sObject API name that owns the field.
        Examples: "Account", "Contact", "Invoice__c".
    field_name (str): The field API name on that object.
        Examples: "Rating", "OwnerId", "Customer_Code__c".

Returns:
    str: JSON-encoded string.
         On success:
         {
           "success": true,
           "field": { ...describe() field dict without 'attributes'... },
           "extra": { ...first Tooling FieldDefinition row... }  // may be {}
         }

         On not found:
         {
           "success": false,
           "error": "Field not found"
         }

         On error:
         {
           "success": false,
           "error": "<message>"
         }

Examples:
    # Standard field on a standard object
    fetch_custom_field("Account", "Rating")

    # Custom field on a custom object
    fetch_custom_field("Invoice__c", "Customer_Code__c")

    # Reference field
    fetch_custom_field("Ticket__c", "Account__c")

Typical usage:
    res = json.loads(fetch_custom_field("Invoice__c", "Customer_Code__c"))
    if res["success"]:
        core = res["field"]
        tooling = res["extra"]
        # e.g., core["type"], core.get("length"), tooling.get("DurableId")
"""

    try:
        sf = get_salesforce_connection()

        desc = getattr(sf, object_name).describe()
        field = next((f for f in desc["fields"] if f["name"] == field_name), None)
        if not field:
            return json.dumps({"success": False, "error": "Field not found"}, indent=2)

        tooling_q = (
            "SELECT Id, DurableId, DataType, Precision, Scale, Length "
            f"FROM FieldDefinition WHERE EntityDefinition.QualifiedApiName = '{object_name}' "
            f"AND QualifiedApiName = '{field_name}'"
        )
        tooling_res = sf.toolingexecute(f"query/?q={tooling_q}")
        extra = tooling_res["records"][0] if tooling_res.get("records") else {}

        field.pop("attributes", None)
        return json.dumps({"success": True, "field": field, "extra": extra}, indent=2)

    except Exception as e:
        logger.error("fetch_custom_field: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)

@register_tool
def upsert_custom_field(
    object_name: str,
    field_api_name: str,
    label: str,
    field_type: str,
    type_params: str = "",
    required: bool = False,
    description: str = ""
) -> str:
    """Create or update a **custom field** using simple string/boolean params
    (no dict), then ensure you can actually query it by granting FLS via a
    “System Admin” Permission Set (auto-created/assigned if needed).

    Why this exists
    ----------------
    LLMs sometimes struggle with nested dicts. This variant accepts **5–6 simple
    arguments** and a compact `type_params` string that carries type-specific
    knobs.

    Arguments
    ---------
    object_name (str):
    # ---- Friendly fallback for missing label ----
    if not label:
        base = re.sub(r'__c$', '', field_api_name or 'Field')
        label = re.sub(r'[_\W]+', ' ', base).strip().title() or 'New Field'
        Target sObject (API name). Standard objects allowed as-is
        (Account, Contact, Lead, Opportunity, Case). Otherwise must be custom
        and will be normalized to end with `__c`.
    field_api_name (str):
        Field API name (must end with `__c`; will be normalized if missing).
    label (str):
        Field label.
    field_type (str):
        One of: Text, Number, Currency, Checkbox, Date, DateTime, Picklist,
        Lookup, MasterDetail, LongTextArea, Email, Phone, URL.
    type_params (str):
        Compact key=value list (semicolon or comma separated). Examples:
          - Text:        "length=80"
          - LongText:    "length=32768;visibleLines=5"
          - Number:      "precision=18;scale=2"
          - Currency:    "precision=18;scale=2"
          - Checkbox:    "default=true"
          - Picklist:    "values=New|Packed|Shipped|Delivered"
                         (use | or , as separator)
          - Lookup:      "referenceTo=Account;relationshipName=TicketAccount;relationshipLabel=Account"
          - MasterDetail:"referenceTo=Account;relationshipName=TicketAccount;relationshipLabel=Account;deleteConstraint=Cascade"
          - Email/Phone/URL/Date/DateTime: "" (no params)
    required (bool):
        Whether field is required (where applicable).
    description (str):
        Optional help text/description.

    Behavior
    --------
    1) **Normalize & validate names** (no guessing beyond adding `__c`).
    2) **Verify object exists** (describe) and whether field exists already.
    3) **Create or update** the field via Metadata REST:
         - Validation (check-only) first, then actual deploy.
    4) **Grant FLS** so you can immediately query the field:
         - Ensure a Permission Set labeled **"System Admin"** exists (create if not).
         - Assign it to the **current user** if not already assigned.
         - Create/Update a **FieldPermissions** record for the new field
           (`PermissionsRead=true`, `PermissionsEdit=true`).

    Returns
    -------
    JSON string:
      {
        \1success_flag/false,
        "operation": "create_custom_field" | "update_custom_field",
        "object_name": "...",
        "field_name": "...",
        "field_type": "...",
        "job_id": "<deploy-id>",
        "message": "...",
        "errors": null | {...},
        "fls_grant": {
          "permission_set_id": "...",
          "assigned_to_me": true/false,
          "field_permissions_id": "..."  // or null if skipped
        }
      }

    Quick examples
    --------------
    # Text(80)
    upsert_custom_field("Invoice__c", "Customer_Code__c", "Customer Code", "Text", "length=80")

    # Number(18,2), required
    upsert_custom_field("Order__c", "Net_Amount__c", "Net Amount", "Number", "precision=18;scale=2", True)

    # Picklist
    upsert_custom_field("Order__c", "Status__c", "Status", "Picklist", "values=New|Packed|Shipped|Delivered")

    # Lookup(Account)
    upsert_custom_field("Ticket__c", "Account__c", "Account", "Lookup",
                               "referenceTo=Account;relationshipName=TicketAccount;relationshipLabel=Account")
    """
    import io, json, zipfile, re
    from typing import Any, Dict

    def _parse_kv(s: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not s:
            return out
        # split by ; or , pairs -> key=value
        for pair in re.split(r"[;,]\s*", s.strip()):
            if not pair:
                continue
            if "=" not in pair:
                # allow lone values for "values=" style if user passes "A|B|C"
                # but we handle values via key anyway, so skip
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            v = v.strip()
            # coerce bools/ints if look like them
            if v.lower() in ("true", "false"):
                out[k] = (v.lower() == "true")
            else:
                # numbers
                if re.fullmatch(r"-?\d+", v):
                    out[k] = int(v)
                elif re.fullmatch(r"-?\d+\.\d+", v):
                    try:
                        out[k] = float(v)
                    except Exception:
                        out[k] = v
                else:
                    out[k] = v
        return out

    def _normalize_object_name(o: str) -> str:
        std = {"Account","Contact","Lead","Opportunity","Case"}
        if o in std:
            return o
        return o if o.endswith("__c") else (o + "__c")

    def _valid_custom_name(n: str) -> bool:
        # must end with __c and contain only letters/numbers/underscores; start with a letter
        if not n.endswith("__c"):
            return False
        core = n[:-3]
        return bool(core) and core[0].isalpha() and core.replace("_","").isalnum()

    def _build_field_config() -> Dict[str, Any]:
        cfg: Dict[str, Any] = {
            "fullName": field_name,          # required by XML generator
            "name": field_name,              # kept for parity with older helpers
            "label": label,
            "type": field_type,
            "required": required,
            "description": description
        }
        tp = _parse_kv(type_params)

        ft = field_type.lower()
        if ft in ("text","email","phone","url"):
            if ft == "text":
                cfg["length"] = tp.get("length", 80)
        elif ft in ("longtextarea","longtext","textarea"):
            cfg["type"] = "LongTextArea"
            cfg["length"] = tp.get("length", 32768)
            cfg["visibleLines"] = tp.get("visibleLines", 3)
        elif ft in ("number","currency"):
            cfg["precision"] = tp.get("precision", 18)
            cfg["scale"] = tp.get("scale", 0 if ft=="number" else 2)
            cfg["type"] = "Currency" if ft=="currency" else "Number"
        elif ft == "checkbox":
            cfg["defaultValue"] = tp.get("default", False)
            cfg["type"] = "Checkbox"
        elif ft in ("date","datetime"):
            cfg["type"] = "DateTime" if ft=="datetime" else "Date"
        elif ft == "picklist":
            vals_raw = tp.get("values", "")
            if isinstance(vals_raw, str):
                items = [v.strip() for v in re.split(r"[|,]", vals_raw) if v.strip()]
            else:
                items = []
            # Convert to the format expected by _generate_custom_field_xml
            cfg["picklistValues"] = [{"fullName": item, "label": item, "default": False} for item in items]
            cfg["type"] = "Picklist"
        elif ft in ("lookup","masterdetail","master-detail"):
            cfg["type"] = "MasterDetail" if ft.startswith("master") else "Lookup"
            cfg["referenceTo"] = tp.get("referenceTo")
            cfg["relationshipName"] = tp.get("relationshipName")
            cfg["relationshipLabel"] = tp.get("relationshipLabel", label)
            if cfg["type"] == "MasterDetail":
                if required is False:
                    # MD is inherently required at the DB level; flip to True to avoid metadata error
                    cfg["required"] = True
                if "deleteConstraint" in tp:
                    cfg["deleteConstraint"] = tp["deleteConstraint"]
        else:
            # Pass-through: rely on XML generator to validate/throw
            pass

        return cfg

    try:
        sf = get_salesforce_connection()

        # ---- Normalize names ----
        object_name = _normalize_object_name(object_name)
        field_name = field_api_name if field_api_name.endswith("__c") else (field_api_name + "__c")

        # ---- Validate names ----
        if not _valid_custom_name(field_name):
            return json.dumps({"success": False, "error": "Invalid field API name (must end with __c, start with a letter, contain only letters/numbers/underscores)."}, indent=2)
        # object must be custom unless standard allowed
        if not (object_name in {"Account","Contact","Lead","Opportunity","Case"} or _valid_custom_name(object_name)):
            return json.dumps({"success": False, "error": "Invalid object API name."}, indent=2)

        # ---- Object existence check (describe) ----
        try:
            desc = getattr(sf, object_name).describe()
        except Exception:
            return json.dumps({"success": False, "error": f"Object not found: {object_name}"}, indent=2)

        # ---- Field existence check (describe fields) ----
        existing_field = next((f for f in desc.get("fields", []) if f.get("name") == field_name), None)
        is_update = existing_field is not None

        # ---- Build field config from simple params ----
        field_config: Dict[str, Any] = _build_field_config()

        # ---- Generate XML & package ----
        field_xml = _generate_custom_field_xml(field_config)

        api_ver = getattr(sf, "sf_version", "59.0")
        member_name = f"{object_name}.{field_name}"

        # ✅ Deploy a field, not the whole object (avoids label/pluralLabel requirements)
        pkg_xml = _generate_package_xml([member_name], "CustomField", api_ver)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
             z.writestr("package.xml", pkg_xml)
             # MDAPI still expects the field inside the object’s metadata file
             obj_xml = _generate_custom_object_with_field(object_name, field_config)
             z.writestr(f"objects/{object_name}.object", obj_xml)
 
        # ---- Actual deploy ----
        buf.seek(0)
        deploy = _execute_metadata_rest_deploy_multipart(sf, buf, check_only=False)
        final_status = _poll_metadata_rest_deploy_status(sf, deploy["id"])
        op = "update_custom_field" if is_update else "create_custom_field"

        # Short-circuit on deploy failure (skip FLS work if field didn't deploy)
        if not final_status.get("success"):
            return json.dumps({
                "success": False,
                "operation": op,
                "object_name": object_name,
                "field_name": field_name,
                "field_type": field_type,
                "job_id": deploy["id"],
                "message": "Field deployment failed",
                "errors": final_status.get("details")
            }, indent=2)

        # ---- Post-step: Ensure FLS via Permission Set "System Admin" ----
        fls_result = {"permission_set_id": None, "assigned_to_me": False, "field_permissions_id": None}
        try:
            # 1) Find or create Permission Set (Label='System Admin')
            ps_q = ("SELECT Id, Name, Label FROM PermissionSet "
                    "WHERE Label = 'System Admin' OR Name = 'System_Admin' LIMIT 1")
            ps_res = sf.query(ps_q)
            if ps_res.get("totalSize", 0) == 0:
                # Create it
                created = sf.PermissionSet.create({
                    "Name": "System_Admin",
                    "Label": "System Admin",
                    "Description": "Auto-created by tool for field-level access",
                    "HasActivationRequired": False
                })
                ps_id = created.get("id")
            else:
                ps_id = ps_res["records"][0]["Id"]
            fls_result["permission_set_id"] = ps_id

            # 2) Get current user id (Chatter 'me' endpoint is reliable)
            try:
                me = sf.restful("chatter/users/me")
                me_id = me["id"]
            except Exception:
                # Fallback: best-effort last-login user (not ideal, but avoids hard fail)
                me_q = "SELECT Id FROM User WHERE IsActive = true ORDER BY LastLoginDate DESC NULLS LAST LIMIT 1"
                me_res = sf.query(me_q)
                me_id = me_res["records"][0]["Id"] if me_res.get("totalSize", 0) > 0 else None

            # 3) Assign PS to current user if not already
            if me_id:
                chk_q = f"SELECT Id FROM PermissionSetAssignment WHERE AssigneeId = '{me_id}' AND PermissionSetId = '{ps_id}' LIMIT 1"
                chk = sf.query(chk_q)
                if chk.get("totalSize", 0) == 0:
                    sf.PermissionSetAssignment.create({"AssigneeId": me_id, "PermissionSetId": ps_id})
                fls_result["assigned_to_me"] = True if me_id else False

            # 4) Grant FieldPermissions (read+edit) for the field on that PS
            field_full = f"{object_name}.{field_name}"
            fp_q = ("SELECT Id, PermissionsRead, PermissionsEdit FROM FieldPermissions "
                    f"WHERE ParentId = '{ps_id}' AND SobjectType = '{object_name}' AND Field = '{field_full}' LIMIT 1")
            fp = sf.query(fp_q)
            if fp.get("totalSize", 0) == 0:
                created_fp = sf.FieldPermissions.create({
                    "ParentId": ps_id,
                    "SobjectType": object_name,
                    "Field": field_full,
                    "PermissionsRead": True,
                    "PermissionsEdit": True
                })
                fls_result["field_permissions_id"] = created_fp.get("id")
            else:
                fp_id = fp["records"][0]["Id"]
                # Ensure both perms are true
                sf.FieldPermissions.update(fp_id, {"PermissionsRead": True, "PermissionsEdit": True})
                fls_result["field_permissions_id"] = fp_id

        except Exception as fls_err:
            # Don’t fail the whole operation—surface the FLS error context.
            return json.dumps({
                "success": True,  # field deployed successfully
                "operation": op,
                "object_name": object_name,
                "field_name": field_name,
                "field_type": field_type,
                "job_id": deploy["id"],
                "message": f"Field deployed, but FLS grant step encountered an error: {fls_err}",
                "errors": None,
                "fls_grant": fls_result
            }, indent=2)

        # ---- Done ----
        return json.dumps({
            "success": True,
            "operation": op,
            "object_name": object_name,
            "field_name": field_name,
            "field_type": field_type,
            "job_id": deploy["id"],
            "message": (f"Successfully {'updated' if is_update else 'created'} field {field_name} on {object_name} "
                        "and granted read/edit via 'System Admin' Permission Set"),
            "errors": None,
            "fls_grant": fls_result
        }, indent=2)

    except Exception as e:
        logger.error("upsert_custom_field error: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)

# =============================================================================
# SOQL QUERY EXECUTION TOOL
# =============================================================================

@register_tool
def execute_soql_query(query: str, use_tooling_api: bool = False) -> str:
    """Execute a SOQL (or Tooling SOQL) query and return a normalized JSON string.

This helper trims whitespace, runs the query via the chosen API, and strips
Salesforce `attributes` objects from records for a cleaner, consistent payload.
It **does not** validate schema or handle pagination.

Notes:
- Schema checks: This function doesn’t verify that objects/fields exist. Validate
  beforehand using helpers like `fetch_object_metadata`, `fetch_custom_field`, or by
  confirming with the user.
- Pagination: Results beyond the server page size are not auto-fetched. Use LIMIT or
  handle `nextRecordsUrl` outside this function if you need full pagination.
- API choice: Set `use_tooling_api=True` for metadata objects (e.g., ApexClass,
  LightningComponent*, EntityDefinition, FieldDefinition).
- Safety: Avoid naive string interpolation. Escape user-supplied values (e.g., with a
  `soql_quote()` helper) to prevent broken queries.

Args:
    query (str): Raw SOQL string (e.g., "SELECT Id, Name FROM Account LIMIT 10").
    use_tooling_api (bool): Execute against the Tooling API when True; otherwise use
        the standard REST API. Defaults to False.

Returns:
    str: JSON-encoded string.
         On success:
           {
             "success": true,
             "totalSize": <int>,
             "done": <bool>,
             "records": [ ... ]  // nested `attributes` removed
           }
         On error:
           {
             "success": false,
             "error": "<message>",
             "query": "<original query>"
           }

Examples:
    # Data query
    execute_soql_query("SELECT Id, Name FROM Account LIMIT 10")

    # Metadata (Tooling) query
    execute_soql_query(
        "SELECT Id, Name, Body FROM ApexClass WHERE Name = 'MyClass'",
        use_tooling_api=True
    )
"""
    try:
        sf = get_salesforce_connection()
        
        # Clean up the query - remove extra whitespace and ensure proper formatting
        clean_query = ' '.join(query.strip().split())
        
        if use_tooling_api:
            # Use Tooling API for metadata queries
            result = sf.toolingexecute(f"query/?q={clean_query}")
        else:
            # Use standard API for data queries
            result = sf.query(clean_query)
        
        # Clean up the response - remove attributes and format nicely
        if result.get("records"):
            for record in result["records"]:
                record.pop("attributes", None)
                # Also clean nested objects
                for key, value in record.items():
                    if isinstance(value, dict) and "attributes" in value:
                        value.pop("attributes", None)
        
        return json.dumps({
            "success": True, 
            "totalSize": result.get("totalSize", 0),
            "done": result.get("done", True),
            "records": result.get("records", [])
        }, indent=2)
        
    except Exception as e:
        logger.error("execute_soql_query error: %s", e, exc_info=True)
        return json.dumps({
            "success": False, 
            "error": str(e),
            "query": query
        }, indent=2)

# =============================================================================
# APEX / LWC DEPLOY INTERNALS
# =============================================================================

def deploy_apex_class_internal(
    sf_connection, class_name: str, files_content: Dict[str, str], api_version: str
) -> Dict[str, Any]:
    """Deploy Apex class via REST Metadata."""
    pkg_xml = _generate_package_xml([class_name], "ApexClass", api_version)

    PNS = "http://soap.sforce.com/2006/04/metadata"
    root = etree.Element(etree.QName(PNS, "ApexClass"), nsmap={None: PNS})
    etree.SubElement(root, etree.QName(PNS, "apiVersion")).text = str(api_version)
    etree.SubElement(root, etree.QName(PNS, "status")).text = "Active"
    meta_xml = etree.tostring(
        root, encoding="UTF-8", pretty_print=True, xml_declaration=True
    ).decode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", pkg_xml)
        z.writestr(f"classes/{class_name}.cls", files_content["apex"])
        z.writestr(f"classes/{class_name}.cls-meta.xml", meta_xml)
    buf.seek(0)

    dep = _execute_metadata_rest_deploy_multipart(sf_connection, buf)
    status = _poll_metadata_rest_deploy_status(sf_connection, dep["id"])
    # Normalize success strictly from terminal deploy status
    success_flag = str(status.get("status", "")).lower() == "succeeded"
    status["job_id"] = dep["id"]
    return status


def deploy_lwc_component_internal(
    sf_connection, component_name: str, files_content: Dict[str, str]
) -> Dict[str, Any]:
    """Deploy an LWC bundle."""
    api_version = getattr(sf_connection, "sf_version", "59.0")
    pkg_xml = _generate_package_xml([component_name], "LightningComponentBundle", api_version)

    base = f"lwc/{component_name}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("package.xml", pkg_xml)
        z.writestr(f"{base}{component_name}.html", files_content["html"])
        z.writestr(f"{base}{component_name}.js", files_content["js"])
        z.writestr(f"{base}{component_name}.js-meta.xml", files_content["xml"])
        if files_content.get("css"):
            z.writestr(f"{base}{component_name}.css", files_content["css"])
        if files_content.get("svg"):
            z.writestr(f"{base}{component_name}.svg", files_content["svg"])
    buf.seek(0)

    dep = _execute_metadata_rest_deploy_multipart(sf_connection, buf)
    status = _poll_metadata_rest_deploy_status(sf_connection, dep["id"])
    # Normalize success strictly from terminal deploy status
    success_flag = str(status.get("status", "")).lower() == "succeeded"
    status["job_id"] = dep["id"]
    return status

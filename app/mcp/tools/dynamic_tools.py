import logging
import requests
import json
import time
import zipfile
import io
from typing import Optional, Dict, Any, List
from lxml import etree
import base64

from app.mcp.server import register_tool
from app.services.salesforce import get_salesforce_connection

logger = logging.getLogger(__name__)

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


# =============================================================================
# APEX CLASS TOOLS (ENHANCED WITH CREATE)
# =============================================================================

@register_tool
def fetch_apex_class(class_name: str) -> str:
    """Return full metadata for an Apex class (Tooling API + core fields)."""
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
    """Create a new Apex class."""
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
    """Update an existing Apex class."""
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
def fetch_lwc_component_safe(component_name: str) -> str:
    """Fetch metadata and source for an LWC component, surviving org schema diffs."""
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
        logger.error("fetch_lwc_component_safe: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@register_tool
def create_lwc_component(
    component_name: str, 
    html_content: str = "", 
    js_content: str = "", 
    css_content: str = "",
    description: str = "",
    api_version: Optional[str] = None
) -> str:
    """Create a new Lightning Web Component with basic template files."""
    try:
        sf = get_salesforce_connection()
        
        # 🔧 FIX: Use Tooling API for existence check instead of regular query
        try:
            tooling_query = f"SELECT Id FROM LightningComponentBundle WHERE DeveloperName = '{component_name}'"
            exists = sf.toolingexecute(f'query/?q={tooling_query}')
            if exists.get("size", 0) > 0:
                return json.dumps(
                    {"success": False, "error": f"LWC component '{component_name}' already exists. Use upsert_lwc_component to update it."},
                    indent=2
                )
        except Exception as tooling_error:
            # If Tooling API also fails, proceed with deployment (let Salesforce handle duplicates)
            logger.warning(f"Tooling API check failed: {tooling_error}. Proceeding with deployment.")

        # Validate component name
        if not component_name.replace("_", "").replace("-", "").isalnum():
            return json.dumps(
                {"success": False, "error": "Invalid component name. Use only alphanumeric characters, underscores, and hyphens."},
                indent=2
            )

        if api_version is None:
            api_version = getattr(sf, "sf_version", "59.0")

        # Generate default files if not provided
        if not html_content:
            html_content = f"""<template>
    <div class="{component_name}">
        <h1>Hello from {component_name}!</h1>
        <p>This is a new Lightning Web Component.</p>
    </div>
</template>"""

        if not js_content:
            js_content = f"""import {{ LightningElement }} from 'lwc';

export default class {component_name.capitalize()} extends LightningElement {{
    // Component logic goes here
}}"""

        # Generate meta XML
        xml_content = _generate_lwc_meta_xml(component_name, description, api_version)

        files = {
            "html": html_content,
            "js": js_content,
            "xml": xml_content
        }
        
        if css_content:
            files["css"] = css_content

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
def upsert_lwc_component(component_name: str, files: Dict[str, str]) -> str:
    """Update an existing LWC component."""
    try:
        sf = get_salesforce_connection()
        
        # 🔧 FIX: Use Tooling API for existence check with fallback
        try:
            tooling_query = f"SELECT Id FROM LightningComponentBundle WHERE DeveloperName = '{component_name}'"
            exists = sf.toolingexecute(f'query/?q={tooling_query}')
            if exists.get("size", 0) == 0:
                return json.dumps(
                    {"success": False, "error": "Component not found (use create_lwc_component to create new components)"}, 
                    indent=2
                )
        except Exception as tooling_error:
            # If Tooling API also fails, proceed with deployment
            logger.warning(f"Tooling API check failed: {tooling_error}. Proceeding with update.")

        # Validate required files
        for req in ("html", "js", "xml"):
            if req not in files:
                return json.dumps(
                    {"success": False, "error": f"Missing required file: {req}"}, indent=2
                )

        res = deploy_lwc_component_internal(sf, component_name, files)
        
        return json.dumps({
            "success": res.get("success", False),
            "operation": "update_lwc_component",
            "component_name": component_name,
            "files_updated": list(files.keys()),
            "message": f"Successfully updated LWC component '{component_name}'" if res.get("success") else f"Failed to update LWC component '{component_name}'",
            "job_id": res.get("job_id"),
            "errors": res.get("details") if not res.get("success") else None
        }, indent=2)

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
    """Create or update a custom object (simple implementation)."""
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
    """Fetch describe info + Tooling metadata for one field."""
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
def upsert_custom_field(object_name: str, field_config: Dict[str, Any]) -> str:
    """Create a new custom field via Metadata REST."""
    try:
        sf = get_salesforce_connection()

        # Ensure proper object naming
        if not object_name.endswith("__c") and object_name not in {
            "Account", "Contact", "Lead", "Opportunity", "Case"
        }:
            object_name += "__c"

        # Get field name and ensure proper naming
        field_name = field_config["name"]
        if not field_name.endswith("__c"):
            field_name += "__c"
        
        # Validate field name
        if not field_name[:-3].replace("_", "").isalnum():
            return json.dumps({"success": False, "error": "Invalid field name"}, indent=2)

        # Set the fullName for XML generation (this is crucial)
        field_config["fullName"] = field_name  # Just the field name, not object.field
        field_xml = _generate_custom_field_xml(field_config)

        # Create package.xml with the correct member format
        api_ver = getattr(sf, "sf_version", "59.0")
        member_name = f"{object_name}.{field_name}"  # This goes in package.xml
        pkg_xml = _generate_package_xml([member_name], "CustomField", api_ver)

        # Create deployment package with CORRECT file structure
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("package.xml", pkg_xml)
            
            # 🔧 CRITICAL FIX: Use the object folder structure approach
            z.writestr(f"objects/{object_name}/fields/{field_name}.field", field_xml)
            
            # Also create a minimal object definition to ensure object exists in package
            minimal_object_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">
    <fields>
        <fullName>{field_name}</fullName>
        <label>{field_config['label']}</label>
        <type>{field_config['type']}</type>
        {"<length>" + str(field_config['length']) + "</length>" if 'length' in field_config else ""}
        {"<required>" + str(field_config.get('required', False)).lower() + "</required>" if 'required' in field_config else ""}
        {"<description>" + field_config['description'] + "</description>" if 'description' in field_config else ""}
    </fields>
</CustomObject>"""
            z.writestr(f"objects/{object_name}.object", minimal_object_xml)
        
        buf.seek(0)

        # Deploy with validation first
        deploy_result = _execute_metadata_rest_deploy_multipart(sf, buf, check_only=True)
        validation_status = _poll_metadata_rest_deploy_status(sf, deploy_result["id"])
        
        if not validation_status["success"]:
            return json.dumps({
                "success": False,
                "error": "Validation failed",
                "validation_errors": validation_status.get("details"),
                "job_id": deploy_result["id"]
            }, indent=2)

        # If validation passes, do actual deployment
        buf.seek(0)
        actual_deploy = _execute_metadata_rest_deploy_multipart(sf, buf, check_only=False)
        final_status = _poll_metadata_rest_deploy_status(sf, actual_deploy["id"])

        return json.dumps({
            "success": final_status["success"],
            "operation": "create_custom_field",
            "object_name": object_name,
            "field_name": field_name,
            "field_type": field_config.get("type"),
            "job_id": actual_deploy["id"],
            "message": f"Successfully created field {field_name} on {object_name}" if final_status["success"] else "Field creation failed",
            "errors": final_status.get("details") if not final_status["success"] else None
        }, indent=2)

    except Exception as e:
        logger.error("upsert_custom_field error: %s", e, exc_info=True)
        return json.dumps({"success": False, "error": str(e)}, indent=2)

# =============================================================================
# SOQL QUERY EXECUTION TOOL
# =============================================================================

@register_tool
def execute_soql_query(query: str, use_tooling_api: bool = False) -> str:
    """Execute any SOQL query and return results in JSON format.
    
    Args:
        query: The SOQL query to execute (e.g., "SELECT Id, Name FROM Account LIMIT 10")
        use_tooling_api: Set to True for Tooling API queries (for metadata objects like ApexClass, CustomField etc.)
    
    Returns:
        JSON string with query results or error message
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
    status["job_id"] = dep["id"]
    return status

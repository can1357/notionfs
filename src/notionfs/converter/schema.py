"""Database schema serialization for _schema.yaml files."""

from typing import Any

import yaml


def schema_to_yaml(schema: dict[str, Any]) -> str:
   """Convert Notion database schema to YAML format.

   Args:
      schema: Raw Notion database properties schema.

   Returns:
      YAML string suitable for _schema.yaml file.
   """
   properties: dict[str, Any] = {}

   for name, prop in schema.items():
      prop_type = prop.get("type")
      if not prop_type:
         continue

      prop_entry = _serialize_property(prop, prop_type)
      if prop_entry:
         properties[name] = prop_entry

   return yaml.dump(
      {"properties": properties},
      default_flow_style=False,
      allow_unicode=True,
      sort_keys=False,
   )


def _serialize_property(prop: dict[str, Any], prop_type: str) -> dict[str, Any] | None:
   """Serialize a single property to YAML-friendly format."""
   match prop_type:
      case "title":
         return {"type": "title"}

      case "rich_text":
         return {"type": "rich_text"}

      case "number":
         num_result: dict[str, Any] = {"type": "number"}
         format_str = prop.get("number", {}).get("format")
         if format_str and format_str != "number":
            num_result["format"] = format_str
         return num_result

      case "select":
         options = prop.get("select", {}).get("options", [])
         option_names = [opt.get("name") for opt in options if opt.get("name")]
         sel_result: dict[str, Any] = {"type": "select"}
         if option_names:
            sel_result["options"] = option_names
         return sel_result

      case "multi_select":
         options = prop.get("multi_select", {}).get("options", [])
         option_names = [opt.get("name") for opt in options if opt.get("name")]
         msel_result: dict[str, Any] = {"type": "multi_select"}
         if option_names:
            msel_result["options"] = option_names
         return msel_result

      case "date":
         return {"type": "date"}

      case "checkbox":
         return {"type": "checkbox"}

      case "url":
         return {"type": "url"}

      case "email":
         return {"type": "email"}

      case "phone_number":
         return {"type": "phone_number"}

      case "people":
         return {"type": "people"}

      case "relation":
         rel_data = prop.get("relation", {})
         rel_result: dict[str, Any] = {"type": "relation"}
         if rel_data.get("database_id"):
            rel_result["database_id"] = rel_data["database_id"]
         return rel_result

      case "formula":
         formula_data = prop.get("formula", {})
         formula_result: dict[str, Any] = {"type": "formula", "readonly": True}
         if formula_data.get("expression"):
            formula_result["expression"] = formula_data["expression"]
         return formula_result

      case "rollup":
         rollup_data = prop.get("rollup", {})
         rollup_result: dict[str, Any] = {"type": "rollup", "readonly": True}
         if rollup_data.get("relation_property_name"):
            rollup_result["relation"] = rollup_data["relation_property_name"]
         if rollup_data.get("rollup_property_name"):
            rollup_result["property"] = rollup_data["rollup_property_name"]
         if rollup_data.get("function"):
            rollup_result["function"] = rollup_data["function"]
         return rollup_result

      case "status":
         status_data = prop.get("status", {})
         options = status_data.get("options", [])
         groups = status_data.get("groups", [])
         option_names = [opt.get("name") for opt in options if opt.get("name")]
         status_result: dict[str, Any] = {"type": "status"}
         if option_names:
            status_result["options"] = option_names
         if groups:
            status_result["groups"] = [g.get("name") for g in groups if g.get("name")]
         return status_result

      case "files":
         return {"type": "files"}

      case "unique_id":
         uid_data = prop.get("unique_id", {})
         uid_result: dict[str, Any] = {"type": "unique_id", "readonly": True}
         if uid_data.get("prefix"):
            uid_result["prefix"] = uid_data["prefix"]
         return uid_result

      case "created_time":
         return {"type": "created_time", "readonly": True}

      case "last_edited_time":
         return {"type": "last_edited_time", "readonly": True}

      case "created_by":
         return {"type": "created_by", "readonly": True}

      case "last_edited_by":
         return {"type": "last_edited_by", "readonly": True}

      case _:
         # Unknown type - include minimal info
         return {"type": prop_type}


def parse_schema(content: str) -> dict[str, Any]:
   """Parse _schema.yaml content to schema dict.

   Args:
      content: YAML file content.

   Returns:
      Schema dict with property definitions.
   """
   try:
      data = yaml.safe_load(content)
      if not data or not isinstance(data, dict):
         return {}
      props = data.get("properties", {})
      return dict(props) if isinstance(props, dict) else {}
   except yaml.YAMLError:
      return {}


def schema_to_notion_format(parsed_schema: dict[str, Any]) -> dict[str, Any]:
   """Convert parsed schema to Notion API property format for type lookups.

   This creates a minimal schema dict suitable for frontmatter_to_properties().

   Args:
      parsed_schema: Schema dict from parse_schema().

   Returns:
      Dict in Notion API schema format (property name -> {type: ...}).
   """
   result: dict[str, Any] = {}

   for name, prop in parsed_schema.items():
      if not isinstance(prop, dict):
         continue
      prop_type = prop.get("type")
      if prop_type:
         result[name] = {"type": prop_type}

   return result


# List of property types that cannot be written via API
READONLY_TYPES = frozenset({
   "formula",
   "rollup",
   "unique_id",
   "created_time",
   "last_edited_time",
   "created_by",
   "last_edited_by",
})


def is_readonly_property(prop_def: dict[str, Any]) -> bool:
   """Check if a property definition is read-only.

   Args:
      prop_def: Property definition from schema.

   Returns:
      True if property cannot be written via API.
   """
   if prop_def.get("readonly"):
      return True
   return prop_def.get("type") in READONLY_TYPES

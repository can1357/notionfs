"""Bidirectional property conversion between Notion and frontmatter."""

from typing import Any


def extract_plain_text(rich_text: list[dict[str, Any]]) -> str:
    """Extract plain text from Notion rich_text array."""
    if not rich_text:
        return ""
    return "".join(item.get("plain_text", "") for item in rich_text)


def properties_to_frontmatter(
    properties: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert Notion page properties to YAML-safe frontmatter dict.

    Args:
       properties: Raw Notion properties dict from page object.
       schema: Optional database schema for additional type info.

    Returns:
       Dict suitable for YAML frontmatter serialization.
    """
    result: dict[str, Any] = {}

    for name, prop in properties.items():
        prop_type = prop.get("type")
        if not prop_type:
            continue

        value = _extract_property_value(prop, prop_type, name, result)
        if value is not None:
            result[name] = value

    return result


def _extract_property_value(
    prop: dict[str, Any],
    prop_type: str,
    name: str,
    result: dict[str, Any],
) -> Any:
    """Extract value from a single Notion property."""
    type_data = prop.get(prop_type)

    match prop_type:
        case "title":
            return extract_plain_text(type_data) if type_data else ""

        case "rich_text":
            return extract_plain_text(type_data) if type_data else ""

        case "number":
            return type_data  # Already a number or None

        case "select":
            return type_data.get("name") if type_data else None

        case "multi_select":
            if not type_data:
                return []
            return [item.get("name") for item in type_data if item.get("name")]

        case "date":
            if not type_data:
                return None
            start = type_data.get("start")
            end = type_data.get("end")
            if end:
                result[f"{name}_end"] = end
            return start

        case "checkbox":
            return bool(type_data)

        case "url" | "email" | "phone_number":
            return type_data

        case "people":
            if not type_data:
                return []
            names = []
            for person in type_data:
                # Prefer name, fall back to ID
                if person.get("name"):
                    names.append(person["name"])
                elif person.get("id"):
                    names.append(person["id"])
            return names

        case "relation":
            if not type_data:
                return []
            return [item.get("id") for item in type_data if item.get("id")]

        case "formula":
            return _extract_formula_value(type_data)

        case "rollup":
            return _extract_rollup_value(type_data)

        case "created_time" | "last_edited_time":
            # Skip - these are reflected in file mtime/birthtime, not frontmatter
            return None

        case "created_by" | "last_edited_by":
            # Skip - user metadata not needed in frontmatter
            return None

        case "status":
            return type_data.get("name") if type_data else None

        case "files":
            if not type_data:
                return []
            urls = []
            for file_obj in type_data:
                # Handle both external and Notion-hosted files
                if file_obj.get("type") == "external":
                    url = file_obj.get("external", {}).get("url")
                else:
                    url = file_obj.get("file", {}).get("url")
                if url:
                    urls.append(url)
            return urls

        case "unique_id":
            if not type_data:
                return None
            prefix = type_data.get("prefix", "")
            number = type_data.get("number")
            if number is not None:
                return f"{prefix}{number}" if prefix else str(number)
            return None

        case _:
            # Unknown type - try to extract something useful
            return None


def _extract_formula_value(formula_data: dict[str, Any] | None) -> Any:
    """Extract computed value from formula property."""
    if not formula_data:
        return None

    formula_type = formula_data.get("type")
    if not formula_type:
        return None

    return formula_data.get(formula_type)


def _extract_rollup_value(rollup_data: dict[str, Any] | None) -> Any:
    """Extract computed value from rollup property."""
    if not rollup_data:
        return None

    rollup_type = rollup_data.get("type")
    if not rollup_type:
        return None

    match rollup_type:
        case "number":
            return rollup_data.get("number")
        case "date":
            date_data = rollup_data.get("date")
            return date_data.get("start") if date_data else None
        case "array":
            # Array of results - extract plain values
            array = rollup_data.get("array", [])
            values = []
            for item in array:
                item_type = item.get("type")
                if item_type and item_type in item:
                    item_value = item[item_type]
                    if item_type == "rich_text" and isinstance(item_value, list):
                        item_value = extract_plain_text(item_value)
                    values.append(item_value)
            return values
        case _:
            return rollup_data.get(rollup_type)


def frontmatter_to_properties(
    frontmatter: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Convert frontmatter back to Notion property update format.

    Args:
       frontmatter: Frontmatter dict from parsed markdown.
       schema: Database schema defining property types.

    Returns:
       Dict in Notion's property update format.
    """
    result: dict[str, Any] = {}

    for key, value in frontmatter.items():
        # Skip readonly keys (prefixed with _)
        if key.startswith("_"):
            continue

        # Skip keys ending with _end (handled with their parent date)
        if key.endswith("_end"):
            continue

        # Skip keys not in schema
        if key not in schema:
            continue

        prop_schema = schema[key]
        prop_type = prop_schema.get("type")
        if not prop_type:
            continue

        converted = _convert_to_notion_property(key, value, prop_type, frontmatter)
        if converted is not None:
            result[key] = converted

    return result


def _convert_to_notion_property(
    key: str,
    value: Any,
    prop_type: str,
    frontmatter: dict[str, Any],
) -> dict[str, Any] | None:
    """Convert a single frontmatter value to Notion property format."""
    match prop_type:
        case "title":
            text = str(value) if value is not None else ""
            return {"title": [{"text": {"content": text}}]}

        case "rich_text":
            text = str(value) if value is not None else ""
            return {"rich_text": [{"text": {"content": text}}]}

        case "number":
            if value is None or value == "":
                return {"number": None}
            try:
                return {"number": float(value)}
            except (ValueError, TypeError):
                return {"number": None}

        case "select":
            if value is None or value == "":
                return {"select": None}
            return {"select": {"name": str(value)}}

        case "multi_select":
            if not value:
                return {"multi_select": []}
            if isinstance(value, list):
                items = value
            else:
                items = [v.strip() for v in str(value).split(",") if v.strip()]
            return {"multi_select": [{"name": str(item)} for item in items]}

        case "date":
            if value is None or value == "":
                return {"date": None}
            date_obj: dict[str, str] = {"start": str(value)}
            # Check for corresponding _end key
            end_key = f"{key}_end"
            if end_key in frontmatter and frontmatter[end_key]:
                date_obj["end"] = str(frontmatter[end_key])
            return {"date": date_obj}

        case "checkbox":
            return {"checkbox": bool(value)}

        case "url":
            if value is None or value == "":
                return {"url": None}
            return {"url": str(value)}

        case "email":
            if value is None or value == "":
                return {"email": None}
            return {"email": str(value)}

        case "phone_number":
            if value is None or value == "":
                return {"phone_number": None}
            return {"phone_number": str(value)}

        case "status":
            if value is None or value == "":
                return {"status": None}
            return {"status": {"name": str(value)}}

        case "relation":
            if not value:
                return {"relation": []}
            if isinstance(value, str):
                # Handle comma-separated string of IDs
                ids = [v.strip() for v in value.split(",") if v.strip()]
            else:
                ids = list(value)
            return {"relation": [{"id": str(id_)} for id_ in ids]}

        case _:
            # Unsupported types for writing: formula, rollup, created_*, last_edited_*, etc.
            return None

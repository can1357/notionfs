"""Tests for database schema conversion."""

from notionfs.converter.schema import (
   READONLY_TYPES,
   is_readonly_property,
   parse_schema,
   schema_to_notion_format,
   schema_to_yaml,
)


class TestSchemaToYaml:
   """Tests for schema_to_yaml function."""

   def test_title_property(self) -> None:
      schema = {"Name": {"type": "title"}}
      yaml_str = schema_to_yaml(schema)
      assert "Name:" in yaml_str
      assert "type: title" in yaml_str

   def test_select_with_options(self) -> None:
      schema = {
         "Status": {
            "type": "select",
            "select": {
               "options": [
                  {"name": "Todo"},
                  {"name": "In Progress"},
                  {"name": "Done"},
               ]
            },
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Status:" in yaml_str
      assert "type: select" in yaml_str
      assert "options:" in yaml_str
      assert "Todo" in yaml_str
      assert "In Progress" in yaml_str
      assert "Done" in yaml_str

   def test_multi_select_with_options(self) -> None:
      schema = {
         "Tags": {
            "type": "multi_select",
            "multi_select": {
               "options": [{"name": "bug"}, {"name": "feature"}]
            },
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Tags:" in yaml_str
      assert "type: multi_select" in yaml_str
      assert "options:" in yaml_str
      assert "bug" in yaml_str
      assert "feature" in yaml_str

   def test_date_property(self) -> None:
      schema = {"Due Date": {"type": "date"}}
      yaml_str = schema_to_yaml(schema)
      assert "Due Date:" in yaml_str
      assert "type: date" in yaml_str

   def test_number_with_format(self) -> None:
      schema = {
         "Price": {
            "type": "number",
            "number": {"format": "dollar"},
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Price:" in yaml_str
      assert "type: number" in yaml_str
      assert "format: dollar" in yaml_str

   def test_number_without_format(self) -> None:
      schema = {
         "Count": {
            "type": "number",
            "number": {"format": "number"},
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Count:" in yaml_str
      assert "type: number" in yaml_str
      # Default format should not be included
      assert "format:" not in yaml_str

   def test_formula_marked_readonly(self) -> None:
      schema = {
         "Calculated": {
            "type": "formula",
            "formula": {"expression": "prop(\"A\") + prop(\"B\")"},
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Calculated:" in yaml_str
      assert "type: formula" in yaml_str
      assert "readonly: true" in yaml_str
      assert "expression:" in yaml_str

   def test_rollup_marked_readonly(self) -> None:
      schema = {
         "Total": {
            "type": "rollup",
            "rollup": {
               "relation_property_name": "Tasks",
               "rollup_property_name": "Points",
               "function": "sum",
            },
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Total:" in yaml_str
      assert "type: rollup" in yaml_str
      assert "readonly: true" in yaml_str
      assert "function: sum" in yaml_str

   def test_status_with_groups(self) -> None:
      schema = {
         "Progress": {
            "type": "status",
            "status": {
               "options": [{"name": "Not Started"}, {"name": "In Progress"}, {"name": "Done"}],
               "groups": [{"name": "To Do"}, {"name": "In Progress"}, {"name": "Complete"}],
            },
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Progress:" in yaml_str
      assert "type: status" in yaml_str
      assert "options:" in yaml_str
      assert "groups:" in yaml_str

   def test_relation_property(self) -> None:
      schema = {
         "Parent": {
            "type": "relation",
            "relation": {"database_id": "abc123"},
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "Parent:" in yaml_str
      assert "type: relation" in yaml_str
      assert "database_id: abc123" in yaml_str

   def test_unique_id_with_prefix(self) -> None:
      schema = {
         "ID": {
            "type": "unique_id",
            "unique_id": {"prefix": "TASK-"},
         }
      }
      yaml_str = schema_to_yaml(schema)
      assert "ID:" in yaml_str
      assert "type: unique_id" in yaml_str
      assert "readonly: true" in yaml_str
      assert "prefix: TASK-" in yaml_str


class TestParseSchema:
   """Tests for parse_schema function."""

   def test_parse_simple_schema(self) -> None:
      content = """properties:
  Name:
    type: title
  Status:
    type: select
    options:
      - Todo
      - Done
"""
      schema = parse_schema(content)
      assert "Name" in schema
      assert schema["Name"]["type"] == "title"
      assert "Status" in schema
      assert schema["Status"]["type"] == "select"
      assert schema["Status"]["options"] == ["Todo", "Done"]

   def test_parse_empty_content(self) -> None:
      assert parse_schema("") == {}

   def test_parse_invalid_yaml(self) -> None:
      assert parse_schema("not: valid: yaml: syntax") == {}

   def test_parse_missing_properties(self) -> None:
      content = "other_key: value"
      assert parse_schema(content) == {}


class TestSchemaToNotionFormat:
   """Tests for schema_to_notion_format function."""

   def test_converts_parsed_schema(self) -> None:
      parsed = {
         "Name": {"type": "title"},
         "Status": {"type": "select", "options": ["Todo", "Done"]},
         "Count": {"type": "number"},
      }
      result = schema_to_notion_format(parsed)

      assert result["Name"] == {"type": "title"}
      assert result["Status"] == {"type": "select"}
      assert result["Count"] == {"type": "number"}

   def test_ignores_non_dict_values(self) -> None:
      parsed = {
         "Name": {"type": "title"},
         "Invalid": "not a dict",
      }
      result = schema_to_notion_format(parsed)
      assert "Name" in result
      assert "Invalid" not in result

   def test_ignores_missing_type(self) -> None:
      parsed = {
         "Name": {"type": "title"},
         "NoType": {"options": ["a", "b"]},
      }
      result = schema_to_notion_format(parsed)
      assert "Name" in result
      assert "NoType" not in result


class TestReadonlyProperties:
   """Tests for readonly property detection."""

   def test_readonly_types_defined(self) -> None:
      expected = {"formula", "rollup", "unique_id", "created_time",
                  "last_edited_time", "created_by", "last_edited_by"}
      assert READONLY_TYPES == expected

   def test_is_readonly_by_type(self) -> None:
      assert is_readonly_property({"type": "formula"})
      assert is_readonly_property({"type": "rollup"})
      assert is_readonly_property({"type": "unique_id"})
      assert is_readonly_property({"type": "created_time"})

   def test_is_readonly_by_flag(self) -> None:
      assert is_readonly_property({"type": "select", "readonly": True})

   def test_not_readonly(self) -> None:
      assert not is_readonly_property({"type": "title"})
      assert not is_readonly_property({"type": "select"})
      assert not is_readonly_property({"type": "number"})
      assert not is_readonly_property({"type": "date"})


class TestRoundtrip:
   """Tests for schema serialization roundtrip."""

   def test_roundtrip_preserves_types(self) -> None:
      original_schema = {
         "Name": {"type": "title"},
         "Status": {
            "type": "select",
            "select": {"options": [{"name": "Open"}, {"name": "Closed"}]},
         },
         "Priority": {"type": "number", "number": {"format": "number"}},
         "Due": {"type": "date"},
         "Done": {"type": "checkbox"},
      }

      yaml_str = schema_to_yaml(original_schema)
      parsed = parse_schema(yaml_str)
      notion_format = schema_to_notion_format(parsed)

      assert notion_format["Name"] == {"type": "title"}
      assert notion_format["Status"] == {"type": "select"}
      assert notion_format["Priority"] == {"type": "number"}
      assert notion_format["Due"] == {"type": "date"}
      assert notion_format["Done"] == {"type": "checkbox"}

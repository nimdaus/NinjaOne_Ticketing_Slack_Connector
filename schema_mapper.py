"""
Schema Mapper — NinjaOne Ticket Form → Slack Block Kit

Converts NinjaOne ticket form field definitions into Slack Block Kit
modal blocks. Designed defensively: unknown field types fall back to
plain_text_input so the form always renders, even if the API returns
types we haven't mapped yet.
"""

import logging

logger = logging.getLogger("eng_assist_bot.schema_mapper")

# ---------------------------------------------------------------------------
# NinjaOne field type → Slack Block Kit element builder
# ---------------------------------------------------------------------------

# Each builder returns a (element_dict, extra_block_props) tuple.
# extra_block_props is merged into the enclosing input block (e.g. optional).


def _build_text_element(field: dict) -> dict:
    """Single-line plain text input."""
    return {
        "type": "plain_text_input",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": field.get("description", field.get("label", "Enter value"))[:150],
        },
    }


def _build_multiline_element(field: dict) -> dict:
    """Multi-line plain text input (for WYSIWYG / long text fields)."""
    return {
        "type": "plain_text_input",
        "action_id": "value",
        "multiline": True,
        "placeholder": {
            "type": "plain_text",
            "text": field.get("description", field.get("label", "Enter text"))[:150],
        },
    }


def _build_dropdown_element(field: dict) -> dict:
    """Static select dropdown — options sourced from field values."""
    options = _extract_options(field)
    if not options:
        # Fallback: if no options found, render as text input
        logger.warning(
            "Dropdown field '%s' has no options — falling back to text input",
            field.get("label", field.get("name", "?")),
        )
        return _build_text_element(field)
    return {
        "type": "static_select",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": f"Select {field.get('label', 'option')}",
        },
        "options": options,
    }


def _build_multi_select_element(field: dict) -> dict:
    """Multi-select — user can pick multiple values."""
    options = _extract_options(field)
    if not options:
        logger.warning(
            "Multi-select field '%s' has no options — falling back to text input",
            field.get("label", field.get("name", "?")),
        )
        return _build_text_element(field)
    return {
        "type": "multi_static_select",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": f"Select {field.get('label', 'options')}",
        },
        "options": options,
    }


def _build_checkbox_element(field: dict) -> dict:
    """Single checkbox toggle (boolean field)."""
    label = field.get("label", field.get("name", "Yes"))
    return {
        "type": "checkboxes",
        "action_id": "value",
        "options": [
            {
                "text": {"type": "plain_text", "text": label[:75]},
                "value": "true",
            }
        ],
    }


def _build_date_element(field: dict) -> dict:
    """Date picker."""
    return {
        "type": "datepicker",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": f"Select {field.get('label', 'date')}",
        },
    }


def _build_datetime_element(field: dict) -> dict:
    """Date and Time picker."""
    return {
        "type": "datetimepicker",
        "action_id": "value",
    }


def _build_time_element(field: dict) -> dict:
    """Time picker."""
    return {
        "type": "timepicker",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": f"Select {field.get('label', 'time')}",
        },
    }


def _build_email_element(field: dict) -> dict:
    """Email text input."""
    return {
        "type": "email_text_input",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": field.get("description", "Enter email address")[:150],
        },
    }


def _build_url_element(field: dict) -> dict:
    """URL text input."""
    return {
        "type": "url_text_input",
        "action_id": "value",
        "placeholder": {
            "type": "plain_text",
            "text": field.get("description", "Enter URL")[:150],
        },
    }


def _build_number_element(field: dict) -> dict:
    """Number input."""
    return {
        "type": "number_input",
        "action_id": "value",
        "is_decimal_allowed": True,
        "placeholder": {
            "type": "plain_text",
            "text": field.get("description", "Enter number")[:150],
        },
    }


# ---------------------------------------------------------------------------
# Type registry — maps NinjaOne field type strings to builders.
# Case-insensitive lookup is performed at resolution time.
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, callable] = {
    # Text types
    "TEXT": _build_text_element,
    "TEXT_FIELD": _build_text_element,
    "TEXTFIELD": _build_text_element,
    "SHORT_TEXT": _build_text_element,
    "STRING": _build_text_element,
    "PHONE": _build_text_element,
    # Multiline types
    "WYSIWYG": _build_multiline_element,
    "TEXTAREA": _build_multiline_element,
    "LONG_TEXT": _build_multiline_element,
    "MULTILINE": _build_multiline_element,
    # Dropdown types
    "DROPDOWN": _build_dropdown_element,
    "SELECT": _build_dropdown_element,
    "ENUM": _build_dropdown_element,
    "SINGLE_SELECT": _build_dropdown_element,
    # Multi-select types
    "MULTI_SELECT": _build_multi_select_element,
    "MULTI_DROPDOWN": _build_multi_select_element,
    "MULTISELECT": _build_multi_select_element,
    # Boolean / checkbox
    "CHECKBOX": _build_checkbox_element,
    "BOOLEAN": _build_checkbox_element,
    "BOOL": _build_checkbox_element,
    # Date & Time
    "DATE": _build_date_element,
    "DATEPICKER": _build_date_element,
    "DATE_TIME": _build_datetime_element,
    "DATETIME": _build_datetime_element,
    "TIME": _build_time_element,
    # Specific Dropdowns / Multi-Selects
    "DEVICE_DROPDOWN": _build_dropdown_element,
    "DEVICE_MULTI_SELECT": _build_multi_select_element,
    "ORGANIZATION_DROPDOWN": _build_dropdown_element,
    "ORGANIZATION_LOCATION_DROPDOWN": _build_dropdown_element,
    "ORGANIZATION_LOCATION_MULTI_SELECT": _build_multi_select_element,
    "ORGANIZATION_MULTI_SELECT": _build_multi_select_element,
    # Other specific types
    "IP_ADDRESS": _build_text_element,
    "IPADDRESS": _build_text_element,
    "TOTP": _build_text_element,
    "ATTACHMENT": _build_text_element,
    # Email
    "EMAIL": _build_email_element,
    # URL
    "URL": _build_url_element,
    "LINK": _build_url_element,
    # Numeric
    "NUMERIC": _build_number_element,
    "NUMBER": _build_number_element,
    "INTEGER": _build_number_element,
    "DECIMAL": _build_number_element,
}


# ---------------------------------------------------------------------------
# Options extractor — tries multiple known shapes from NinjaOne API
# ---------------------------------------------------------------------------


def _extract_options(field: dict) -> list[dict]:
    """
    Extract dropdown/select options from a NinjaOne field definition.

    The API may return options in different shapes depending on version:
      - field["values"]          — list of {id, name} or {value, label}
      - field["content"]["values"] — nested under content
      - field["options"]         — direct options list

    We try all known shapes and normalize to Slack option format.
    """
    raw_options = []

    # Shape 1: field["values"]
    if isinstance(field.get("values"), list):
        raw_options = field["values"]
    # Shape 2: field["content"]["values"]
    elif isinstance(field.get("content"), dict) and isinstance(
        field["content"].get("values"), list
    ):
        raw_options = field["content"]["values"]
    # Shape 3: field["options"]
    elif isinstance(field.get("options"), list):
        raw_options = field["options"]

    slack_options = []
    for opt in raw_options:
        # Normalize from {id, name}, {value, label}, {value, text}, or plain string
        if isinstance(opt, str):
            text = opt
            value = opt
        elif isinstance(opt, dict):
            text = str(
                opt.get("name")
                or opt.get("label")
                or opt.get("text")
                or opt.get("displayName")
                or opt.get("value", "?")
            )
            value = str(opt.get("id") or opt.get("value") or text)
        else:
            continue

        # Slack limits: text max 75 chars, value max 75 chars
        slack_options.append(
            {
                "text": {"type": "plain_text", "text": text[:75]},
                "value": value[:75],
            }
        )

    # Slack requires at least 1 option and at most 100
    return slack_options[:100]


# ---------------------------------------------------------------------------
# Public API — build Block Kit blocks from a form schema
# ---------------------------------------------------------------------------


def build_blocks_from_schema(
    form_schema: dict,
    field_overrides: dict | None = None,
) -> list[dict]:
    """
    Convert a NinjaOne ticket form schema into Slack Block Kit blocks.

    Args:
        form_schema: The full response from GET /v2/ticketing/ticket-form/{id},
                     or at minimum a dict with a "fields" key containing field
                     definitions.
        field_overrides: Optional dict of field-id → override settings from the
                         admin UI.  ``{"included": false}`` hides a field;
                         ``{"slackType": "..."}`` changes its Slack widget.

    Returns:
        List of Slack Block Kit block dicts ready for a modal view.
    """
    blocks: list[dict] = []

    # Try multiple possible locations for the fields list
    fields = _extract_fields_list(form_schema)

    if not fields:
        logger.warning(
            "No fields found in form schema — returning empty blocks. "
            "Schema keys: %s",
            list(form_schema.keys()) if isinstance(form_schema, dict) else "N/A",
        )
        return blocks

    logger.info("Mapping %d form fields to Block Kit blocks", len(fields))

    for field in fields:
        block = _field_to_block(field, field_overrides=field_overrides)
        if block:
            blocks.append(block)

    return blocks


def extract_values_from_submission(
    view_values: dict,
    form_schema: dict,
    field_overrides: dict | None = None,
) -> list[dict]:
    """
    Extract submitted values from a Slack modal view.

    Returns a list of dicts, one per form field:
        {"id": "123", "label": "Integration Type", "value": "5"}

    ``id`` is the NinjaOne field ID (as a string; convert to int before
    sending to the API if needed).  ``value`` for dropdown fields is the
    option ID stored by ``_extract_options``, matching what NinjaOne
    expects when the field is populated on ticket creation.

    Callers build the description body from (label, value) pairs and the
    NinjaOne fields payload from (id, value) pairs.

    Args:
        field_overrides: Optional dict of field-id → override settings.
                         Fields marked ``{"included": false}`` are skipped
                         unless they are required.
    """
    fields = _extract_fields_list(form_schema)
    result: list[dict] = []

    for field in fields:
        field_id = str(field.get("id") or field.get("fieldId") or field.get("name", ""))
        label = str(
            field.get("label")
            or field.get("name")
            or field.get("displayName")
            or field_id
        )

        # Determine if the field is required (required fields are never excluded)
        is_required = field.get("required", False)
        if isinstance(is_required, str):
            is_required = is_required.lower() in ("true", "1", "yes")

        # Skip excluded fields (unless required)
        override = (field_overrides or {}).get(str(field_id), {})
        if override.get("included") is False and not is_required:
            continue

        block_id = f"field_{field_id}"
        block_data = view_values.get(block_id, {})
        action_data = block_data.get("value", {})
        value = _extract_single_value(action_data) if action_data else ""

        result.append({"id": field_id, "label": label, "value": value})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_fields_list(form_schema: dict) -> list[dict]:
    """
    Locate the fields array in the form schema.

    NinjaOne may nest fields under different keys depending on the endpoint
    and version. Try common locations defensively.
    """
    if not isinstance(form_schema, dict):
        return []

    # Direct "fields" key
    if isinstance(form_schema.get("fields"), list):
        return form_schema["fields"]

    # Nested under "content" → "fields"
    content = form_schema.get("content")
    if isinstance(content, dict) and isinstance(content.get("fields"), list):
        return content["fields"]

    # Nested under "definition" → "fields"
    definition = form_schema.get("definition")
    if isinstance(definition, dict) and isinstance(definition.get("fields"), list):
        return definition["fields"]

    # The schema itself might be a list of fields
    if isinstance(form_schema.get("ticketFormFields"), list):
        return form_schema["ticketFormFields"]

    return []


# Maps override slackType strings (from the admin UI) to element builder functions.
_OVERRIDE_TYPE_MAP: dict[str, callable] = {
    "plain_text_input":           _build_text_element,
    "plain_text_input_multiline": _build_multiline_element,
    "static_select":              _build_dropdown_element,
    "multi_static_select":        _build_multi_select_element,
    "datepicker":                 _build_date_element,
    "checkboxes":                 _build_checkbox_element,
    "number_input":               _build_number_element,
}


def _field_to_block(field: dict, field_overrides: dict | None = None) -> dict | None:
    """Convert a single NinjaOne field definition to a Slack input block.

    Args:
        field_overrides: Optional dict of field-id → override settings.
                         ``{"included": false}`` skips the field (unless required).
                         ``{"slackType": "..."}`` replaces the default element builder.
    """
    field_type = str(
        field.get("type")
        or field.get("fieldType")
        or field.get("uiType")
        or "TEXT"
    ).upper()

    field_id = str(field.get("id") or field.get("fieldId") or field.get("name", "unknown"))
    label = str(
        field.get("label")
        or field.get("name")
        or field.get("displayName")
        or f"Field {field_id}"
    )

    # Skip system/hidden fields
    if field.get("hidden") or field.get("systemField"):
        logger.debug("Skipping hidden/system field: %s", label)
        return None

    # Determine if the field is required (must be known before override checks)
    is_required = field.get("required", False)
    if isinstance(is_required, str):
        is_required = is_required.lower() in ("true", "1", "yes")

    # Apply field overrides
    override = (field_overrides or {}).get(str(field_id), {})

    # included: false → skip the field, but never skip required fields
    if override.get("included") is False and not is_required:
        logger.debug("Skipping excluded field (override): %s", label)
        return None

    # slackType override → swap the element builder
    override_slack_type = override.get("slackType")
    if override_slack_type:
        builder = _OVERRIDE_TYPE_MAP.get(override_slack_type)
        if builder is None:
            logger.warning(
                "Unknown override slackType '%s' for field '%s' — "
                "falling back to NinjaOne type mapping",
                override_slack_type,
                label,
            )
            builder = _TYPE_MAP.get(field_type)
    else:
        builder = _TYPE_MAP.get(field_type)

    if builder is None:
        logger.warning(
            "Unknown NinjaOne field type '%s' for field '%s' — "
            "falling back to plain_text_input",
            field_type,
            label,
        )
        builder = _build_text_element

    element = builder(field)

    block = {
        "type": "input",
        "block_id": f"field_{field_id}",
        "label": {"type": "plain_text", "text": label[:2000]},
        "element": element,
    }

    if not is_required:
        block["optional"] = True

    return block


def _extract_single_value(action_data: dict) -> str:
    """Extract the user's submitted value from a single action's data."""
    # plain_text_input, email_text_input, url_text_input, number_input
    if "value" in action_data and action_data["value"] is not None:
        return str(action_data["value"])

    # static_select
    if "selected_option" in action_data and action_data["selected_option"]:
        opt = action_data["selected_option"]
        return opt.get("value", opt.get("text", {}).get("text", ""))

    # multi_static_select
    if "selected_options" in action_data and action_data["selected_options"]:
        return ", ".join(
            o.get("value", o.get("text", {}).get("text", ""))
            for o in action_data["selected_options"]
        )

    # datepicker
    if "selected_date" in action_data and action_data["selected_date"]:
        return action_data["selected_date"]

    # datetimepicker
    if "selected_date_time" in action_data and action_data["selected_date_time"]:
        return str(action_data["selected_date_time"])

    # timepicker
    if "selected_time" in action_data and action_data["selected_time"]:
        return action_data["selected_time"]

    # checkboxes
    if "selected_options" in action_data:
        selected = action_data["selected_options"] or []
        if selected:
            return ", ".join(o.get("value", "") for o in selected)
        return "false"

    return ""

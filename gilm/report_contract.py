"""A fixed response grammar; it supplies neither branch totals nor source hashes."""

REPORT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "sales_report_v1",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["sales_by_branch_cents", "start", "end", "currency", "definition", "source_version"],
            "properties": {
                "sales_by_branch_cents": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
                "start": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                "end": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                "currency": {"type": "string", "enum": ["USD"]},
                "definition": {"type": "string", "enum": ["gross booked sales"]},
                "source_version": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
    },
}

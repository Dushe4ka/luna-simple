"""The wire protocol of turn events: required fields, validation, JSON Schema."""

from __future__ import annotations

import json

PROTOCOL_VERSION = 1
_NUM = (int, float)
_OPT_NUM = (int, float, type(None))

EVENT_FIELDS: dict[str, dict[str, type | tuple]] = {
    "turn_started": {"turn_id": str},
    "text_delta": {"text": str},
    "thinking_delta": {"text": str},
    "tool_started": {"call_id": str, "name": str, "args": dict, "args_preview": str},
    "tool_finished": {"call_id": str, "name": str, "ok": bool, "detail": str, "duration": _NUM},
    "approval_needed": {"value": dict},
    "todos": {"todos": list},
    "notice": {"level": str, "text": str},
    "usage_delta": {"usage": dict},
    "compacted": {"summary": str, "removed": int},
    "background_done": {"handle": str, "ok": bool, "summary": str},
    "reload_requested": {},
    "turn_done": {"usage": dict, "cost": _OPT_NUM, "stop_reason": str},
    "error": {"message": str, "kind": str},
    "cancelled": {},
}
_JSON_TYPES = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    dict: "object",
    list: "array",
    type(None): "null",
}


def validate_event(event: dict) -> None:
    """Raise ``ValueError`` unless ``event`` is a well-formed wire event."""
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        raise ValueError("a wire event is an object with a string 'event' field")
    fields = EVENT_FIELDS.get(event["event"])
    if fields is None:
        raise ValueError(f"unknown event {event['event']!r}")
    for name, kind in fields.items():
        if name not in event:
            raise ValueError(f"{event['event']}: missing field {name!r}")
        value = event[name]
        if isinstance(value, bool) and kind in (int, _NUM, _OPT_NUM):
            raise ValueError(f"{event['event']}.{name}: expected a number")
        if not isinstance(value, kind):
            raise ValueError(
                f"{event['event']}.{name}: expected {kind}, got {type(value).__name__}"
            )
    if event["event"] == "approval_needed":
        requests = event["value"].get("action_requests")
        if not isinstance(requests, list) or not all(
            isinstance(r, dict) and "call_id" in r for r in requests
        ):
            raise ValueError(
                "approval_needed.value.action_requests must be a list of requests with call_id"
            )


def _schema_type(kind) -> dict:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    names = sorted({_JSON_TYPES[k] for k in kinds})
    if names == ["integer", "number"]:
        names = ["number"]
    if names == ["integer", "null", "number"]:
        names = ["null", "number"]
    return {"type": names[0] if len(names) == 1 else names}


def json_schema() -> dict:
    """JSON Schema (2020-12) for one wire event."""
    variants = []
    for name, fields in EVENT_FIELDS.items():
        props = {"event": {"const": name}, **{f: _schema_type(k) for f, k in fields.items()}}
        variants.append(
            {
                "type": "object",
                "properties": props,
                "required": ["event", *fields],
                "additionalProperties": True,
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/Dushe4ka/luna-simple/schema/events.schema.json",
        "title": f"Luna turn event (protocol {PROTOCOL_VERSION})",
        "oneOf": variants,
    }


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, ensure_ascii=False))

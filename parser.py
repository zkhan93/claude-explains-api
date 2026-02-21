import json
import logging
from pathlib import Path

logger = logging.getLogger("codebase-analyzer")


def parse_stream_jsonl(output_file: Path) -> dict:
    """Parse a stream-json JSONL file from claude and extract the final result.

    Claude's stream-json format emits one JSON object per line. We scan for:
    - The last "assistant" message → contains the final response text
    - A "result" event → contains session_id and metadata

    Args:
        output_file: Path to the JSONL output file.

    Returns:
        dict with keys: result (str), session_id (str), is_error (bool)
    """
    try:
        raw = output_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("Output file missing: %s", output_file)
        return {"result": "Claude produced no output file", "session_id": "", "is_error": True}

    if not raw.strip():
        logger.error("Output file is empty: %s", output_file)
        return {"result": "Claude produced empty output", "session_id": "", "is_error": True}

    lines = raw.strip().splitlines()
    last_assistant_text = ""
    session_id = ""
    is_error = False
    errors: list[str] = []
    cost_usd = 0.0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        # "result" event has session_id, errors, cost, and final metadata
        if event_type == "result":
            session_id = event.get("session_id", session_id)
            is_error = event.get("is_error", is_error)
            errors = event.get("errors", errors)
            cost_usd = event.get("total_cost_usd", cost_usd)
            # result event may also carry the final text
            if event.get("result"):
                last_assistant_text = event["result"]

        # "assistant" message contains response content
        elif event_type == "assistant":
            message = event.get("message", {})
            content = message.get("content", [])
            # Extract text blocks from content array
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            if texts:
                last_assistant_text = "\n".join(texts)

    if not last_assistant_text and not session_id:
        # Fallback: maybe it's actually a single JSON object (not JSONL)
        try:
            data = json.loads(raw)
            return {
                "result": data.get("result", raw),
                "session_id": data.get("session_id", ""),
                "is_error": data.get("is_error", False),
                "errors": data.get("errors", []),
                "cost_usd": data.get("total_cost_usd", 0.0),
            }
        except json.JSONDecodeError:
            pass

        logger.warning("No assistant message or result found in JSONL (lines=%d)", len(lines))
        return {"result": raw, "session_id": "", "is_error": False, "errors": [], "cost_usd": 0.0}

    if is_error and errors:
        logger.error("Claude errors: %s", errors)

    logger.info(
        "Parsed JSONL | lines=%d result_len=%d session=%s cost=$%.4f",
        len(lines),
        len(last_assistant_text),
        session_id or "-",
        cost_usd,
    )

    return {
        "result": last_assistant_text,
        "session_id": session_id,
        "is_error": is_error,
        "errors": errors,
        "cost_usd": cost_usd,
    }

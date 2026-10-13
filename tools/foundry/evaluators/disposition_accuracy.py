import json
import re


def _record(response):
    if not isinstance(response, str):
        response = json.dumps(response)
    match = re.search(r"```json\s*(\{.*?\})\s*```", response, re.S)
    if match:
        try:
            return json.loads(match.group(1))
        except ValueError:
            pass
    found = re.search(r'"disposition"\s*:\s*"([A-Z_]+)"', response)
    return {"disposition": found.group(1)} if found else {}


def _response(sample, item):
    """Agent-target runs flatten the agent output into the row as item["sample.output_text"]; offline rows carry "response"."""
    for source in (sample, item):
        if isinstance(source, dict):
            for key in ("output_text", "sample.output_text", "response"):
                value = source.get(key)
                if value:
                    return value if isinstance(value, str) else json.dumps(value)
    return ""


def grade(sample, item) -> float:
    """1.0 when the agent's final disposition is one of the labelled acceptable dispositions, else 0.0."""
    item = item if isinstance(item, dict) else {}
    disposition = str(_record(_response(sample, item)).get("disposition") or "").upper()
    expected = [x.strip().upper() for x in re.split(r"\s+or\s+|,|\|", str(item.get("ground_truth") or "")) if x.strip()]
    return 1.0 if disposition and disposition in expected else 0.0

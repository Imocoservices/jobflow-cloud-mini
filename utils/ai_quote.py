import os
import math
from typing import Dict, Any, List
from openai import OpenAI

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # fast & cheap; change if you like
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = """You are a quoting assistant for a home-services contractor.
Extract a structured, *realistic* quote from transcript + any text context.
Return JSON only with fields:
{
  "summary": "one-paragraph layman summary",
  "tasks": [ {"title": str, "details": str, "qty": number|null, "unit": "hr|sqft|each|null"} ],
  "materials": [ {"name": str, "qty": number, "unit": str, "est_cost": number|null} ],
  "entities": { "addresses": [str], "people": [str], "dates": [str] },
  "quote_suggested": [ {"description": str, "quantity": number, "unit_price": number} ],
  "suggested_price": number
}
Rules:
- Use sensible unit prices for South Florida handyman/painting.
- If missing info, estimate conservatively, but do not inflate.
- Keep 4-10 line items max. Quantity and unit_price must be numbers.
"""

def build_user_prompt(transcript: str, context_text: str = "") -> str:
    return f"""TRANSCRIPT:
{transcript.strip()}

EXTRA CONTEXT (optional):
{context_text.strip()}
"""

def generate_quote(transcript: str, context_text: str = "") -> Dict[str, Any]:
    content = build_user_prompt(transcript, context_text)
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        response_format={"type":"json_object"},
        messages=[
            {"role":"system", "content": SYSTEM},
            {"role":"user", "content": content}
        ],
    )
    data = resp.choices[0].message.content
    import json
    obj = json.loads(data)

    # Calculate line totals & overall suggested price if missing
    total = 0.0
    for it in obj.get("quote_suggested", []) or []:
        qty = float(it.get("quantity") or 0)
        up = float(it.get("unit_price") or 0)
        it["line_total"] = round(qty * up, 2)
        total += it["line_total"]
    obj["suggested_price"] = float(obj.get("suggested_price") or total or 0.0)
    obj["suggested_price"] = round(obj["suggested_price"], 2)
    return obj

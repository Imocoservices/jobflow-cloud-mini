import os, json
from pathlib import Path
from typing import Tuple, List
from openai import OpenAI

def _client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    return OpenAI(api_key=key)

def transcribe_audio(audio_path: Path) -> tuple[str|None, str|None]:
    client = _client()
    if not client:
        return None, "OPENAI_API_KEY missing"
    try:
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(model="whisper-1", file=f)
        text = resp.text if hasattr(resp,"text") else resp.get("text","")
        return text, None
    except Exception as e:
        return None, str(e)

def suggest_quote(text: str) -> Tuple[List[dict], float, str|None]:
    client = _client()
    if not client:
        # Graceful fallback: dummy example if key is missing
        items = [{"description":"Labor & materials (placeholder)","quantity":1,"unit_price":250.0}]
        total = 250.0
        return items, total, None
    prompt = f"""
You are a contractor estimator. From this transcript/notes, propose 2-5 line items.
Return JSON array with objects: description, quantity, unit_price.
Transcript/notes:
{text}
"""
    try:
        chat = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
            messages=[{"role":"user","content":prompt}],
            response_format={"type":"json_object"}
        )
        content = chat.choices[0].message.content
        data = json.loads(content)
        # Accept either {"items":[...]} or just [...]
        items = data.get("items", data if isinstance(data, list) else [])
        # normalize + total
        norm = []
        total = 0.0
        for it in items:
            q = float(it.get("quantity",1))
            p = float(it.get("unit_price",0))
            d = str(it.get("description",""))
            norm.append({"description":d,"quantity":q,"unit_price":p})
            total += q*p
        return norm, round(total,2), None
    except Exception as e:
        return [], 0.0, str(e)

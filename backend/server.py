"""TwelveHoursCO - Bulk Product Generator & Listing Approval Dashboard backend."""
import asyncio
import base64
import csv
import io
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware

from emergentintegrations.llm.chat import LlmChat, UserMessage

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# Environment
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
EMERGENT_LLM_KEY = os.environ["EMERGENT_LLM_KEY"]

# Mongo
client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]
capsules_coll = db.capsules

# App
app = FastAPI()
api_router = APIRouter(prefix="/api")

# Logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("twelvehours")


# -----------------------------
# Models
# -----------------------------
class Capsule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    capsule_name: str
    title: str
    description: str
    front_concept: str
    back_concept: str
    tags: List[str]
    front_image_b64: Optional[str] = None
    back_image_b64: Optional[str] = None
    status: str = "draft"  # draft | approved | denied
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_at: Optional[str] = None


class CapsulePublic(BaseModel):
    """Capsule without heavy base64 fields - for listing."""
    id: str
    capsule_name: str
    title: str
    description: str
    front_concept: str
    back_concept: str
    tags: List[str]
    status: str
    created_at: str
    approved_at: Optional[str] = None


# -----------------------------
# Prompt templates
# -----------------------------
DESCRIPTION_TEMPLATE = """THE GRIND // {capsule_name}

A premium, heavy-hitting alternative staple designed for the late-night rotation. Featuring a clean minimalist left-chest graphic on the front and an aggressive, oversized {back_graphic} filling the back.

* Built on the classic Gildan 5000 heavy cotton tee for a structured, boxy streetwear fit.
* Premium DTG printing with stark white ink for maximum contrast.
* 100% Cotton (Fiber content may vary for different colors).
* True to size (Size up for an oversized look).

Care Instructions: Machine wash cold, inside out, with like colors. Tumble dry low or hang dry to preserve print longevity."""


TEXT_SYSTEM_PROMPT = """You are a creative director for TwelveHoursCO, a gothic, industrial grunge, dark alternative streetwear brand. Your aesthetic is stark white ink on solid black: monolithic, religious-industrial, post-punk, occult austerity, late-night void, machinery decay, brutalist serif typography, hand-drawn ink illustration.

You generate ONE shirt design capsule at a time. Return ONLY raw JSON, no prose, no code fences. Schema:
{
  "capsule_name": "2-3 word evocative name (e.g., 'Iron Vigil', 'Hollow Hours', 'Ash Liturgy', 'Concrete Saints')",
  "title": "SEO product title following this exact formula: '{capsule_name} - Oversized Back Print T-Shirt | Gothic Industrial Streetwear | Dark Alt Y2K Tee'",
  "front_concept": "One sentence describing a small left-chest pocket print: minimal, symbolic, intricate (e.g., a tiny gothic sigil, small ornate cross, small mechanical eye, small barbed wire ring). Keep under 20 words.",
  "back_concept": "One sentence describing a large oversized back graphic that fills the back: detailed, monolithic gothic/industrial imagery (e.g., a towering crucifix wrapped in industrial chains, a praying skeleton enthroned on hydraulic press, a decayed rose breaking through rusted gears). Keep under 25 words.",
  "tags": ["array of EXACTLY 13 SEO tags - MUST include 'Gothic Streetwear' and 'Back Print Shirt'; remaining 11 are unique tags specific to this design's theme/imagery"]
}

Be SPECIFIC. Avoid generic clichés like just 'skull' or 'cross'. Reference texture, action, decay, religious or industrial machinery. Each capsule must feel distinct from typical streetwear."""


# -----------------------------
# Capsule generator service
# -----------------------------
async def generate_capsule_text() -> dict:
    """Call Gemini 3 Flash to generate the design concept JSON."""
    session_id = f"capsule-{uuid.uuid4()}"
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=session_id,
        system_message=TEXT_SYSTEM_PROMPT,
    ).with_model("gemini", "gemini-3-flash-preview")

    # Inject randomness so each generation feels different
    themes = [
        "religious-industrial decay", "post-punk machinery", "occult austerity",
        "brutalist cathedral", "rusted shrine", "hydraulic crucifixion",
        "monastic factory", "post-mortem mechanics", "iron prayer", "late-night void",
        "Y2K gothic", "techno-gothic", "abandoned chapel", "concrete saints",
        "ash liturgy", "neon mortuary", "wire crown", "graveyard assembly line",
    ]
    seed_theme = random.choice(themes)

    msg = UserMessage(
        text=f"Generate one design capsule. Lean into this seed theme: '{seed_theme}'. Return only JSON."
    )
    raw = await chat.send_message(msg)
    text = raw if isinstance(raw, str) else str(raw)

    # Strip code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    # Try to extract first JSON object
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in LLM response: {text[:200]}")
    data = json.loads(match.group(0))

    # Validate & normalise tags
    tags = data.get("tags", [])
    if isinstance(tags, list):
        # Ensure required ones
        required = ["Gothic Streetwear", "Back Print Shirt"]
        for r in required:
            if not any(t.lower() == r.lower() for t in tags):
                tags.insert(0, r)
        # Cap to 13
        data["tags"] = tags[:13]
    return data


async def generate_capsule_image(prompt: str) -> Optional[str]:
    """Generate one image using Gemini Nano Banana, return base64 PNG string."""
    session_id = f"img-{uuid.uuid4()}"
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=session_id,
        system_message="You are an expert graphic designer producing stark white-ink-on-black gothic streetwear print graphics.",
    ).with_model("gemini", "gemini-3.1-flash-image-preview").with_params(modalities=["image", "text"])

    msg = UserMessage(text=prompt)
    _text, images = await chat.send_message_multimodal_response(msg)
    if not images:
        return None
    return images[0]["data"]  # base64 string


def build_front_prompt(front_concept: str) -> str:
    return (
        f"Single graphic asset for a t-shirt left-chest pocket print. "
        f"Stark pure white ink illustration on a 100% pure pitch-black (#000000) background. "
        f"Subject: {front_concept}. "
        f"Style: gothic industrial streetwear, intricate linework, high-contrast monochrome, "
        f"silkscreen-print look. Small isolated motif centered on canvas with generous black space. "
        f"NO shirt mockup, NO model, NO text labels - only the white-on-black graphic asset itself. Square 1:1."
    )


def build_back_prompt(back_concept: str) -> str:
    return (
        f"Single oversized back-of-shirt print graphic. "
        f"Stark pure white ink illustration on a 100% pure pitch-black (#000000) background. "
        f"Subject: {back_concept}. "
        f"Style: bold gothic industrial streetwear, dramatic high-contrast monochrome, "
        f"detailed silkscreen-print engraving, religious-industrial decay aesthetic, monolithic composition. "
        f"Fills nearly the entire canvas. NO shirt mockup, NO model, NO text watermark - only the white-on-black graphic asset itself. Square 1:1."
    )


# -----------------------------
# Endpoints
# -----------------------------
@api_router.get("/")
async def root():
    return {"service": "TwelveHoursCO", "status": "online"}


@api_router.post("/capsules/generate", response_model=Capsule)
async def generate_capsule():
    """Generate a new design capsule: AI text + 2 AI images. Returns full draft."""
    try:
        text_data = await generate_capsule_text()
    except Exception as e:
        logger.exception("text generation failed")
        raise HTTPException(status_code=500, detail=f"Text generation failed: {e}")

    capsule_name = text_data.get("capsule_name", "Unnamed Capsule")
    front_concept = text_data.get("front_concept", "")
    back_concept = text_data.get("back_concept", "")

    # Generate both images in parallel
    front_prompt = build_front_prompt(front_concept)
    back_prompt = build_back_prompt(back_concept)
    try:
        front_b64, back_b64 = await asyncio.gather(
            generate_capsule_image(front_prompt),
            generate_capsule_image(back_prompt),
        )
    except Exception as e:
        logger.exception("image generation failed")
        raise HTTPException(status_code=500, detail=f"Image generation failed: {e}")

    description = DESCRIPTION_TEMPLATE.format(
        capsule_name=capsule_name.upper(),
        back_graphic=back_concept.rstrip(".").lower(),
    )

    capsule = Capsule(
        capsule_name=capsule_name,
        title=text_data.get("title", f"{capsule_name} - Oversized Back Print T-Shirt | Gothic Industrial Streetwear | Dark Alt Y2K Tee"),
        description=description,
        front_concept=front_concept,
        back_concept=back_concept,
        tags=text_data.get("tags", []),
        front_image_b64=front_b64,
        back_image_b64=back_b64,
        status="draft",
    )

    await capsules_coll.insert_one(capsule.model_dump())
    return capsule


@api_router.post("/capsules/{capsule_id}/approve", response_model=CapsulePublic)
async def approve_capsule(capsule_id: str):
    now_iso = datetime.now(timezone.utc).isoformat()
    result = await capsules_coll.update_one(
        {"id": capsule_id},
        {"$set": {"status": "approved", "approved_at": now_iso}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Capsule not found")
    doc = await capsules_coll.find_one({"id": capsule_id}, {"_id": 0, "front_image_b64": 0, "back_image_b64": 0})
    return CapsulePublic(**doc)


@api_router.post("/capsules/{capsule_id}/deny")
async def deny_capsule(capsule_id: str):
    """Discard the capsule (delete from db)."""
    result = await capsules_coll.delete_one({"id": capsule_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Capsule not found")
    return {"ok": True, "id": capsule_id}


@api_router.get("/capsules/approved", response_model=List[CapsulePublic])
async def list_approved():
    docs = await capsules_coll.find(
        {"status": "approved"},
        {"_id": 0, "front_image_b64": 0, "back_image_b64": 0},
    ).sort("approved_at", -1).to_list(1000)
    return [CapsulePublic(**d) for d in docs]


@api_router.get("/capsules/stats")
async def stats():
    approved = await capsules_coll.count_documents({"status": "approved"})
    return {"approved": approved}


@api_router.get("/capsules/{capsule_id}/image/{side}")
async def get_capsule_image(capsule_id: str, side: str):
    if side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="side must be 'front' or 'back'")
    field = "front_image_b64" if side == "front" else "back_image_b64"
    doc = await capsules_coll.find_one({"id": capsule_id}, {field: 1, "_id": 0})
    if not doc or not doc.get(field):
        raise HTTPException(status_code=404, detail="Image not found")
    # Gemini may return JPEG or PNG; detect from magic bytes
    raw = base64.b64decode(doc[field])
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(content=raw, media_type=mime)


@api_router.get("/capsules/export.csv")
async def export_csv():
    docs = await capsules_coll.find(
        {"status": "approved"},
        {"_id": 0, "front_image_b64": 0, "back_image_b64": 0},
    ).sort("approved_at", -1).to_list(10000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "capsule_name", "title", "description", "tags",
        "front_concept", "back_concept", "approved_at",
        "front_image_url", "back_image_url",
    ])
    base_url = "/api/capsules"
    for d in docs:
        writer.writerow([
            d.get("id", ""),
            d.get("capsule_name", ""),
            d.get("title", ""),
            d.get("description", ""),
            " | ".join(d.get("tags", [])),
            d.get("front_concept", ""),
            d.get("back_concept", ""),
            d.get("approved_at", ""),
            f"{base_url}/{d.get('id')}/image/front",
            f"{base_url}/{d.get('id')}/image/back",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=twelvehoursco_approved.csv"},
    )


# Include router & CORS
app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

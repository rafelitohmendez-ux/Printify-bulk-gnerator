"""TwelveHoursCO - Bulk Product Generator & Listing Approval Dashboard backend.

Features:
- Background queue worker (pre-warms 5 capsules so approve/deny is instant)
- Theme seed selector (built-in + custom themes)
- Ban-words list to steer the AI
- Inline title/tag editing pre-approve
- Single-side image regeneration
- TTL cleanup of orphan drafts (1 hour)
"""
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware

from emergentintegrations.llm.chat import LlmChat, UserMessage

from printify_client import (
    GILDAN_5000_BLUEPRINT_ID,
    PrintifyError,
    list_print_providers,
    list_shops,
    push_capsule_as_draft,
)

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
settings_coll = db.settings

# Constants
DEFAULT_QUEUE_SIZE = 5
DRAFT_TTL_SECONDS = 60 * 60  # 1 hour
QUEUE_TICK_SECONDS = 2
CLEANUP_TICK_SECONDS = 5 * 60  # 5 min

# Built-in theme pool
DEFAULT_THEMES: List[Dict[str, str]] = [
    {"key": "religious_industrial", "name": "Religious Industrial Decay", "prompt": "religious-industrial decay, crucifixion machinery, divine rust"},
    {"key": "post_punk_machinery", "name": "Post-Punk Machinery", "prompt": "post-punk machinery, broken assembly lines, factory sermons"},
    {"key": "occult_austerity", "name": "Occult Austerity", "prompt": "occult austerity, austere ritual minimalism, monastic darkness"},
    {"key": "brutalist_cathedral", "name": "Brutalist Cathedral", "prompt": "brutalist cathedral architecture, concrete reliquary, monolithic sacred geometry"},
    {"key": "rusted_shrine", "name": "Rusted Shrine", "prompt": "rusted shrine, iron offerings, oxidized devotion"},
    {"key": "hydraulic_crucifixion", "name": "Hydraulic Crucifixion", "prompt": "hydraulic crucifixion, mechanical martyrdom, piston-driven sacrament"},
    {"key": "monastic_factory", "name": "Monastic Factory", "prompt": "monastic factory, robe-clad assembly, gothic industrialization"},
    {"key": "iron_prayer", "name": "Iron Prayer", "prompt": "iron prayer, forged liturgy, blacksmith devotion"},
    {"key": "late_night_void", "name": "Late-Night Void", "prompt": "late-night void, 3 AM emptiness, nocturnal liminality"},
    {"key": "y2k_gothic", "name": "Y2K Gothic", "prompt": "Y2K gothic, millennium dread, lo-fi cyber decay"},
    {"key": "techno_gothic", "name": "Techno Gothic", "prompt": "techno-gothic, cybernetic mysticism, electronic seance"},
    {"key": "abandoned_chapel", "name": "Abandoned Chapel", "prompt": "abandoned chapel, derelict sanctuary, forgotten worship"},
    {"key": "concrete_saints", "name": "Concrete Saints", "prompt": "concrete saints, brutalist iconography, cast-stone reverence"},
    {"key": "ash_liturgy", "name": "Ash Liturgy", "prompt": "ash liturgy, burnt ritual remains, cinder communion"},
    {"key": "neon_mortuary", "name": "Neon Mortuary", "prompt": "neon mortuary, electric wake, fluorescent funeral"},
    {"key": "wire_crown", "name": "Wire Crown", "prompt": "wire crown, barbed regalia, industrial coronation"},
    {"key": "graveyard_assembly", "name": "Graveyard Assembly", "prompt": "graveyard assembly line, factory of the dead, mechanized mourning"},
    {"key": "post_mortem_mechanics", "name": "Post-Mortem Mechanics", "prompt": "post-mortem mechanics, autopsy machinery, surgical liturgy"},
]

# App + router
app = FastAPI()
api_router = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("twelvehours")

# Concurrency: avoid hammering LLM API
generation_lock = asyncio.Lock()
_worker_task: Optional[asyncio.Task] = None
_cleanup_task: Optional[asyncio.Task] = None


# -----------------------------
# Models
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    status: str = "draft"  # draft | approved
    consumed: bool = False  # true once a draft has been sent to the UI for review
    theme_seed: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)
    approved_at: Optional[str] = None


class CapsulePublic(BaseModel):
    id: str
    capsule_name: str
    title: str
    description: str
    front_concept: str
    back_concept: str
    tags: List[str]
    status: str
    theme_seed: Optional[str] = None
    created_at: str
    approved_at: Optional[str] = None
    printify_product_id: Optional[str] = None
    printify_push_status: Optional[str] = None  # 'success' | 'failed' | None
    printify_push_error: Optional[str] = None


class CustomTheme(BaseModel):
    name: str
    prompt: str


class SettingsDoc(BaseModel):
    id: str = "config"
    active_theme: str = "auto"  # "auto" | built-in key | custom name
    custom_themes: List[CustomTheme] = []
    banned_words: List[str] = []
    queue_size: int = DEFAULT_QUEUE_SIZE
    # Printify integration
    printify_shop_id: Optional[int] = None
    printify_print_provider_id: Optional[int] = None
    printify_auto_push: bool = False


class SettingsUpdate(BaseModel):
    active_theme: Optional[str] = None
    custom_themes: Optional[List[CustomTheme]] = None
    banned_words: Optional[List[str]] = None
    queue_size: Optional[int] = None
    printify_shop_id: Optional[int] = None
    printify_print_provider_id: Optional[int] = None
    printify_auto_push: Optional[bool] = None


class ApprovePayload(BaseModel):
    title: Optional[str] = None
    tags: Optional[List[str]] = None
    capsule_name: Optional[str] = None


# -----------------------------
# Description template
# -----------------------------
DESCRIPTION_TEMPLATE = """THE GRIND // {capsule_name}

A premium, heavy-hitting alternative staple designed for the late-night rotation. Featuring a clean minimalist left-chest graphic on the front and an aggressive, oversized {back_graphic} filling the back.

* Built on the classic Gildan 5000 heavy cotton tee for a structured, boxy streetwear fit.
* Premium DTG printing with stark white ink for maximum contrast.
* 100% Cotton (Fiber content may vary for different colors).
* True to size (Size up for an oversized look).

Care Instructions: Machine wash cold, inside out, with like colors. Tumble dry low or hang dry to preserve print longevity."""


def build_text_system_prompt(banned_words: List[str]) -> str:
    base = """You are a creative director for TwelveHoursCO, a gothic, industrial grunge, dark alternative streetwear brand. Your aesthetic is stark white ink on solid black: monolithic, religious-industrial, post-punk, occult austerity, late-night void, machinery decay, brutalist serif typography, hand-drawn ink illustration.

You generate ONE shirt design capsule at a time. Return ONLY raw JSON, no prose, no code fences. Schema:
{
  "capsule_name": "2-3 word evocative name (e.g., 'Iron Vigil', 'Hollow Hours', 'Ash Liturgy', 'Concrete Saints')",
  "title": "SEO product title following this exact formula: '{capsule_name} - Oversized Back Print T-Shirt | Gothic Industrial Streetwear | Dark Alt Y2K Tee'",
  "front_concept": "STRICTLY a single, ultra-minimal MICRO-GRAPHIC for the left-chest / center-front position. Must be ONE isolated symbol or compact mark - a single sigil, a small monogram, a tiny industrial icon, a compact occult glyph, a minimal geometric mark, a single rune, a tiny seal, a clean wordmark in 1-2 letters, or a small abstract icon. Stark, clean, industrial linework only. NO scenes. NO multiple elements. NO illustrations of objects or figures. ONE symbol on a void. Keep under 15 words.",
  "back_concept": "One sentence describing a large oversized back graphic that fills the back: detailed, monolithic gothic/industrial imagery. Keep under 25 words.",
  "tags": ["array of EXACTLY 13 SEO tags - MUST include 'Gothic Streetwear' and 'Back Print Shirt'; remaining 11 are unique tags specific to this design's theme/imagery"]
}

For front_concept specifically: think 'a tiny ink stamp', 'a watch-dial-sized mark', 'a single hand-pulled glyph'. NEVER a full illustration. NEVER multiple visual elements. The front is a whisper; the back is a scream.

For back_concept: be SPECIFIC, dense, monolithic. Reference texture, action, decay, religious or industrial machinery."""
    if banned_words:
        joined = ", ".join(f"'{w}'" for w in banned_words)
        base += f"\n\nABSOLUTELY DO NOT use any of these banned words or their close variants anywhere in the output: {joined}. If you would normally use them, substitute with a different, vivid alternative."
    return base


# -----------------------------
# Settings persistence
# -----------------------------
async def get_settings() -> dict:
    doc = await settings_coll.find_one({"id": "config"}, {"_id": 0})
    if not doc:
        default = SettingsDoc().model_dump()
        await settings_coll.insert_one(default)
        return default
    return doc


def resolve_theme(settings: dict) -> dict:
    """Return chosen theme {key/name, prompt} based on settings."""
    active = (settings.get("active_theme") or "auto").strip()
    customs = settings.get("custom_themes") or []
    all_themes = DEFAULT_THEMES + [
        {"key": f"custom:{t['name']}", "name": t["name"], "prompt": t["prompt"]}
        for t in customs
    ]
    if active == "auto" or not active:
        return random.choice(all_themes) if all_themes else {"key": "auto", "name": "auto", "prompt": "gothic industrial streetwear"}
    for t in all_themes:
        if t["key"] == active or t["name"].lower() == active.lower():
            return t
    return random.choice(all_themes) if all_themes else {"key": "auto", "name": "auto", "prompt": "gothic industrial streetwear"}


# -----------------------------
# AI generation
# -----------------------------
async def llm_generate_text(theme_prompt: str, banned_words: List[str]) -> dict:
    session_id = f"capsule-{uuid.uuid4()}"
    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=session_id,
        system_message=build_text_system_prompt(banned_words),
    ).with_model("gemini", "gemini-3-flash-preview")

    ban_hint = ""
    if banned_words:
        ban_hint = f" Avoid words: {', '.join(banned_words)}."
    msg = UserMessage(
        text=f"Generate one design capsule. Lean into this seed theme: '{theme_prompt}'.{ban_hint} Return only JSON."
    )
    raw = await chat.send_message(msg)
    text = raw if isinstance(raw, str) else str(raw)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in LLM response: {text[:200]}")
    data = json.loads(match.group(0))
    tags = data.get("tags") or []
    required = ["Gothic Streetwear", "Back Print Shirt"]
    for r in required:
        if not any(str(t).lower() == r.lower() for t in tags):
            tags.insert(0, r)
    data["tags"] = tags[:13]
    return data


async def llm_generate_image(prompt: str) -> Optional[str]:
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
    return images[0]["data"]


def build_front_prompt(front_concept: str) -> str:
    return (
        f"A single, ultra-minimal MICRO-GRAPHIC asset for a t-shirt left-chest / center-front position. "
        f"Stark pure white ink on a 100% pure pitch-black (#000000) background. "
        f"Subject: {front_concept}. "
        f"STRICT RULES: ONE isolated symbol only. Compact, tiny scale, fits inside a small badge area. "
        f"Clean industrial linework, no shading gradients, no halftones, no texture noise. "
        f"Single visual element only - NO scenes, NO multiple objects, NO illustrative composition. "
        f"Think: a single ink stamp, a small monogram, a minimalist sigil, a watch-dial-sized mark. "
        f"Generous black void around the symbol (at least 40% empty space on every side). "
        f"NO shirt mockup, NO model, NO text labels, NO watermark - only the isolated white symbol on pure black. "
        f"Stark, clean, industrial, minimal. Square 1:1 canvas."
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


async def _generate_capsule(settings: dict) -> Capsule:
    theme = resolve_theme(settings)
    text_data = await llm_generate_text(theme["prompt"], settings.get("banned_words") or [])
    capsule_name = text_data.get("capsule_name", "Unnamed Capsule")
    front_concept = text_data.get("front_concept", "")
    back_concept = text_data.get("back_concept", "")
    front_b64, back_b64 = await asyncio.gather(
        llm_generate_image(build_front_prompt(front_concept)),
        llm_generate_image(build_back_prompt(back_concept)),
    )
    description = DESCRIPTION_TEMPLATE.format(
        capsule_name=capsule_name.upper(),
        back_graphic=back_concept.rstrip(".").lower(),
    )
    return Capsule(
        capsule_name=capsule_name,
        title=text_data.get(
            "title",
            f"{capsule_name} - Oversized Back Print T-Shirt | Gothic Industrial Streetwear | Dark Alt Y2K Tee",
        ),
        description=description,
        front_concept=front_concept,
        back_concept=back_concept,
        tags=text_data.get("tags") or [],
        front_image_b64=front_b64,
        back_image_b64=back_b64,
        status="draft",
        consumed=False,
        theme_seed=theme.get("name") or theme.get("key"),
    )


async def _generate_and_store(settings: dict, mark_consumed: bool = False) -> Capsule:
    capsule = await _generate_capsule(settings)
    if mark_consumed:
        capsule.consumed = True
    await capsules_coll.insert_one(capsule.model_dump())
    return capsule


# -----------------------------
# Background workers
# -----------------------------
async def queue_worker():
    """Keep the unconsumed-draft pool topped up to settings.queue_size."""
    logger.info("Queue worker started")
    while True:
        try:
            settings = await get_settings()
            target = int(settings.get("queue_size") or DEFAULT_QUEUE_SIZE)
            pool = await capsules_coll.count_documents({"status": "draft", "consumed": False})
            if pool < target:
                async with generation_lock:
                    pool = await capsules_coll.count_documents({"status": "draft", "consumed": False})
                    if pool < target:
                        try:
                            cap = await _generate_capsule(settings)
                            await capsules_coll.insert_one(cap.model_dump())
                            logger.info(f"Pre-warmed capsule '{cap.capsule_name}' (pool now {pool + 1}/{target})")
                        except Exception:
                            logger.exception("queue worker generation failed")
                            await asyncio.sleep(15)  # back off on error
            await asyncio.sleep(QUEUE_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("Queue worker cancelled")
            break
        except Exception:
            logger.exception("queue worker loop error")
            await asyncio.sleep(5)


async def cleanup_worker():
    """Periodically delete orphan drafts older than TTL."""
    logger.info("Cleanup worker started")
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=DRAFT_TTL_SECONDS)).isoformat()
            result = await capsules_coll.delete_many({"status": "draft", "created_at": {"$lt": cutoff}})
            if result.deleted_count:
                logger.info(f"Cleaned up {result.deleted_count} orphan draft(s)")
            await asyncio.sleep(CLEANUP_TICK_SECONDS)
        except asyncio.CancelledError:
            logger.info("Cleanup worker cancelled")
            break
        except Exception:
            logger.exception("cleanup worker loop error")
            await asyncio.sleep(60)


# -----------------------------
# Endpoints
# -----------------------------
@api_router.get("/")
async def root():
    return {"service": "TwelveHoursCO", "status": "online"}


@api_router.get("/capsules/next", response_model=Capsule)
async def next_capsule():
    """Pop the next pre-warmed draft, or generate one synchronously if queue is empty."""
    doc = await capsules_coll.find_one_and_update(
        {"status": "draft", "consumed": False},
        {"$set": {"consumed": True, "consumed_at": now_iso()}},
        sort=[("created_at", 1)],
        projection={"_id": 0},
        return_document=True,
    )
    if doc:
        return Capsule(**doc)
    # Queue empty: generate synchronously
    settings = await get_settings()
    async with generation_lock:
        cap = await _generate_and_store(settings, mark_consumed=True)
    return cap


@api_router.post("/capsules/generate", response_model=Capsule)
async def generate_capsule_now():
    """Force-generate a fresh capsule synchronously (consumed)."""
    settings = await get_settings()
    try:
        async with generation_lock:
            cap = await _generate_and_store(settings, mark_consumed=True)
        return cap
    except Exception as e:
        logger.exception("force generate failed")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/capsules/queue/status")
async def queue_status():
    pool = await capsules_coll.count_documents({"status": "draft", "consumed": False})
    settings = await get_settings()
    return {"depth": pool, "target": int(settings.get("queue_size") or DEFAULT_QUEUE_SIZE)}


@api_router.post("/capsules/{capsule_id}/approve", response_model=CapsulePublic)
async def approve_capsule(capsule_id: str, payload: Optional[ApprovePayload] = None):
    update: Dict = {"status": "approved", "approved_at": now_iso()}
    if payload:
        if payload.title is not None:
            update["title"] = payload.title
        if payload.tags is not None:
            update["tags"] = payload.tags[:13]
        if payload.capsule_name is not None:
            update["capsule_name"] = payload.capsule_name
    result = await capsules_coll.update_one({"id": capsule_id}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Capsule not found")

    # Auto-push to Printify as draft if configured
    settings = await get_settings()
    if (
        settings.get("printify_auto_push")
        and settings.get("printify_shop_id")
        and settings.get("printify_print_provider_id")
    ):
        full = await capsules_coll.find_one({"id": capsule_id}, {"_id": 0})
        try:
            product = await push_capsule_as_draft(
                shop_id=int(settings["printify_shop_id"]),
                print_provider_id=int(settings["printify_print_provider_id"]),
                capsule=full,
            )
            pid = str(product.get("id") or "")
            await capsules_coll.update_one(
                {"id": capsule_id},
                {"$set": {
                    "printify_product_id": pid,
                    "printify_push_status": "success",
                    "printify_push_error": None,
                }},
            )
            logger.info(f"Pushed capsule {capsule_id} to Printify product {pid}")
        except Exception as e:
            logger.exception("printify push failed")
            await capsules_coll.update_one(
                {"id": capsule_id},
                {"$set": {
                    "printify_push_status": "failed",
                    "printify_push_error": str(e)[:500],
                }},
            )

    doc = await capsules_coll.find_one(
        {"id": capsule_id},
        {"_id": 0, "front_image_b64": 0, "back_image_b64": 0},
    )
    return CapsulePublic(**doc)


@api_router.post("/capsules/{capsule_id}/deny")
async def deny_capsule(capsule_id: str):
    result = await capsules_coll.delete_one({"id": capsule_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Capsule not found")
    return {"ok": True, "id": capsule_id}


@api_router.post("/capsules/{capsule_id}/regenerate-image/{side}", response_model=Capsule)
async def regenerate_image(capsule_id: str, side: str):
    if side not in ("front", "back"):
        raise HTTPException(status_code=400, detail="side must be 'front' or 'back'")
    doc = await capsules_coll.find_one({"id": capsule_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Capsule not found")
    concept = doc.get("front_concept" if side == "front" else "back_concept", "")
    prompt = build_front_prompt(concept) if side == "front" else build_back_prompt(concept)
    try:
        async with generation_lock:
            b64 = await llm_generate_image(prompt)
    except Exception as e:
        logger.exception("regenerate image failed")
        raise HTTPException(status_code=500, detail=str(e))
    if not b64:
        raise HTTPException(status_code=500, detail="Image generation returned empty")
    field = "front_image_b64" if side == "front" else "back_image_b64"
    await capsules_coll.update_one({"id": capsule_id}, {"$set": {field: b64}})
    updated = await capsules_coll.find_one({"id": capsule_id}, {"_id": 0})
    return Capsule(**updated)


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
    raw = base64.b64decode(doc[field])
    mime = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(
        content=raw,
        media_type=mime,
        headers={"Cache-Control": "no-store"},  # so regen shows new image
    )


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
        "front_concept", "back_concept", "theme_seed", "approved_at",
        "front_image_url", "back_image_url",
    ])
    base_url = "/api/capsules"
    for d in docs:
        writer.writerow([
            d.get("id", ""),
            d.get("capsule_name", ""),
            d.get("title", ""),
            d.get("description", ""),
            " | ".join(d.get("tags") or []),
            d.get("front_concept", ""),
            d.get("back_concept", ""),
            d.get("theme_seed", "") or "",
            d.get("approved_at", "") or "",
            f"{base_url}/{d.get('id')}/image/front",
            f"{base_url}/{d.get('id')}/image/back",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=twelvehoursco_approved.csv"},
    )


# -----------------------------
# Settings endpoints
# -----------------------------
@api_router.get("/settings")
async def get_settings_endpoint():
    s = await get_settings()
    return {
        "active_theme": s.get("active_theme", "auto"),
        "custom_themes": s.get("custom_themes") or [],
        "banned_words": s.get("banned_words") or [],
        "queue_size": s.get("queue_size") or DEFAULT_QUEUE_SIZE,
        "built_in_themes": [{"key": t["key"], "name": t["name"]} for t in DEFAULT_THEMES],
        "printify_shop_id": s.get("printify_shop_id"),
        "printify_print_provider_id": s.get("printify_print_provider_id"),
        "printify_auto_push": bool(s.get("printify_auto_push")),
        "printify_token_configured": bool(os.environ.get("PRINTIFY_API_TOKEN")),
    }


@api_router.put("/settings")
async def update_settings(payload: SettingsUpdate):
    update: Dict = {}
    flush_queue = False
    if payload.active_theme is not None:
        update["active_theme"] = payload.active_theme
        flush_queue = True
    if payload.custom_themes is not None:
        update["custom_themes"] = [t.model_dump() for t in payload.custom_themes]
        flush_queue = True
    if payload.banned_words is not None:
        update["banned_words"] = [w for w in payload.banned_words if w and w.strip()]
        flush_queue = True
    if payload.queue_size is not None:
        update["queue_size"] = max(0, min(20, int(payload.queue_size)))
    if payload.printify_shop_id is not None:
        update["printify_shop_id"] = int(payload.printify_shop_id) if payload.printify_shop_id else None
    if payload.printify_print_provider_id is not None:
        update["printify_print_provider_id"] = (
            int(payload.printify_print_provider_id) if payload.printify_print_provider_id else None
        )
    if payload.printify_auto_push is not None:
        update["printify_auto_push"] = bool(payload.printify_auto_push)
    if update:
        await settings_coll.update_one({"id": "config"}, {"$set": update}, upsert=True)
    if flush_queue:
        deleted = await capsules_coll.delete_many({"status": "draft", "consumed": False})
        logger.info(f"Flushed {deleted.deleted_count} pre-warmed drafts on settings update")
    return await get_settings_endpoint()


# -----------------------------
# Printify endpoints
# -----------------------------
@api_router.get("/printify/shops")
async def printify_shops():
    try:
        shops = await list_shops()
        return {"shops": shops}
    except PrintifyError as e:
        raise HTTPException(status_code=502, detail=str(e))


@api_router.get("/printify/print-providers")
async def printify_print_providers():
    try:
        providers = await list_print_providers(GILDAN_5000_BLUEPRINT_ID)
        return {"blueprint_id": GILDAN_5000_BLUEPRINT_ID, "providers": providers}
    except PrintifyError as e:
        raise HTTPException(status_code=502, detail=str(e))


@api_router.post("/capsules/{capsule_id}/push-printify")
async def manual_push(capsule_id: str):
    """Manual on-demand push of an already-approved capsule."""
    settings = await get_settings()
    if not settings.get("printify_shop_id") or not settings.get("printify_print_provider_id"):
        raise HTTPException(
            status_code=400,
            detail="Configure printify_shop_id and printify_print_provider_id in settings first.",
        )
    full = await capsules_coll.find_one({"id": capsule_id}, {"_id": 0})
    if not full:
        raise HTTPException(status_code=404, detail="Capsule not found")
    try:
        product = await push_capsule_as_draft(
            shop_id=int(settings["printify_shop_id"]),
            print_provider_id=int(settings["printify_print_provider_id"]),
            capsule=full,
        )
        pid = str(product.get("id") or "")
        await capsules_coll.update_one(
            {"id": capsule_id},
            {"$set": {
                "printify_product_id": pid,
                "printify_push_status": "success",
                "printify_push_error": None,
            }},
        )
        return {"ok": True, "printify_product_id": pid}
    except Exception as e:
        logger.exception("manual printify push failed")
        await capsules_coll.update_one(
            {"id": capsule_id},
            {"$set": {
                "printify_push_status": "failed",
                "printify_push_error": str(e)[:500],
            }},
        )
        raise HTTPException(status_code=502, detail=str(e))


# -----------------------------
# App wiring
# -----------------------------
app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    global _worker_task, _cleanup_task
    # Reset stale consumed=true drafts that were never approved/denied (e.g. server restart mid-review)
    # If they're old, cleanup_worker handles them. If young, leave them - user will see them again
    # by re-requesting /next. Simpler: just relaunch workers.
    _worker_task = asyncio.create_task(queue_worker())
    _cleanup_task = asyncio.create_task(cleanup_worker())


@app.on_event("shutdown")
async def on_shutdown():
    if _worker_task:
        _worker_task.cancel()
    if _cleanup_task:
        _cleanup_task.cancel()
    client.close()

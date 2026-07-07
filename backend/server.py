"""MidnightRotation - Bulk Product Generator & Listing Approval Dashboard backend.

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
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.cors import CORSMiddleware

from google import genai
from google.genai import types

from printify_client import (
    GILDAN_5000_BLUEPRINT_ID,
    PrintifyError,
    list_print_providers,
    list_shops,
    prioritize_back_mockup,
    publish_product,
    push_capsule_as_draft,
    update_product,
)
from bulk_seo_update import generate_seo

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# Environment
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
genai_client = genai.Client(api_key=GEMINI_API_KEY)

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
if not ADMIN_API_KEY:
    raise RuntimeError("ADMIN_API_KEY env var is required. Set it to a strong random secret.")

_cors_raw = os.environ.get("CORS_ORIGINS", "")
if not _cors_raw.strip():
    raise RuntimeError(
        "CORS_ORIGINS env var is required. "
        "Set it to a comma-separated list of allowed origins, "
        "e.g. 'https://yourapp.vercel.app,http://localhost:3000'"
    )
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]

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
GENERATION_TIMEOUT_SECONDS = 120

SEO_TITLE_FORMULAS = [
    "{capsule_name} - Oversized Back Print T-Shirt | Gothic Industrial Streetwear | Dark Alt Y2K Tee",
    "{capsule_name} | Midnight Rotation Drop | Gothic Back Print Heavy Tee | Industrial Goth Streetwear",
    "{capsule_name} Tee - Dark Alt Streetwear | Oversized Back Graphic | Industrial Gothic Y2K",
    "{capsule_name} - Heavy Cotton Back Print Tee | MidnightRotation | Dark Streetwear | Gothic Industrial",
    "{capsule_name} | {theme_hint} Tee | Oversized Back Print | Dark Academia Streetwear Gift",
    "{capsule_name} Shirt - {theme_hint} Graphic Tee | Gothic Streetwear | Unisex Heavy Cotton",
    "{capsule_name} | Alt Streetwear Tee | {theme_hint} Back Print | Y2K Grunge Gothic Shirt",
    "{capsule_name} - {theme_hint} Tee | Midnight Rotation | Oversized Streetwear | Goth Gift Idea",
    "{capsule_name} Tee | Dark {theme_hint} Graphic | Industrial Streetwear | Back Print Heavy Cotton",
    "{capsule_name} - {theme_hint} Oversized Tee | Gothic Industrial | Dark Alt Streetwear Shirt",
]

# Words pulled from the theme prompt to slot into {theme_hint} so titles
# differentiate by actual design content instead of repeating a fixed phrase.
THEME_HINT_OVERRIDES = {
    "religious_industrial": "Religious Industrial",
    "post_punk_machinery": "Post-Punk",
    "occult_austerity": "Occult",
    "brutalist_cathedral": "Brutalist Cathedral",
    "rusted_shrine": "Rusted Shrine",
    "hydraulic_crucifixion": "Hydraulic",
    "monastic_factory": "Monastic",
    "iron_prayer": "Iron Liturgy",
    "late_night_void": "Late-Night Void",
    "y2k_gothic": "Y2K Gothic",
    "techno_gothic": "Techno Gothic",
    "abandoned_chapel": "Abandoned Chapel",
    "concrete_saints": "Concrete Saint",
    "ash_liturgy": "Ash Liturgy",
    "neon_mortuary": "Neon Mortuary",
    "wire_crown": "Wire Crown",
    "graveyard_assembly": "Graveyard",
    "post_mortem_mechanics": "Post-Mortem",
    "dark_americana": "Dark Americana",
    "digital_decay": "Digital Decay",
    "plague_gothic": "Plague Gothic",
    "brutalist_shrine": "Brutalist Shrine",
    "storm_liturgy": "Storm Liturgy",
    "asylum_plague": "Asylum Plague",
    "bone_church": "Bone Church",
    "crow_sermon": "Crow Sermon",
    "folk_horror": "Folk Horror",
    "deep_sea_gothic": "Deep Sea Gothic",
    "cyber_mortuary": "Cyber Mortuary",
    "diesel_punk_relic": "Diesel Punk Relic",
}

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
    {"key": "dark_americana", "name": "Dark Americana", "prompt": "dark americana decay, rural gothic, rusted crosses, grain silo cathedral, tobacco barn sermon"},
    {"key": "digital_decay", "name": "Digital Decay", "prompt": "digital decay, corrupted circuit liturgy, server rack altar, machine consciousness, AI as false god"},
    {"key": "plague_gothic", "name": "Plague Gothic", "prompt": "plague doctor ritual, memento mori, bone architecture, apothecary altar, medieval death aesthetic"},
    {"key": "brutalist_shrine", "name": "Brutalist Shrine", "prompt": "brutalist shrine, raw concrete sacred space, Soviet monument worship, cast iron devotion"},
    {"key": "storm_liturgy", "name": "Storm Liturgy", "prompt": "storm liturgy, lightning as divine punishment, tornado sacrament, flood baptism, weather as god"},
    {"key": "asylum_plague", "name": "Asylum Plague", "prompt": "plague asylum, abandoned sanitarium decay, plague doctor ward, crumbling medical gothic, quarantine ritual, infected institution"},
    {"key": "bone_church", "name": "Bone Church", "prompt": "ossuary architecture, bone church, catacombs altar, skeleton liturgy, reliquary decay, death chapel"},
    {"key": "crow_sermon", "name": "Crow Sermon", "prompt": "corvid death omen, plague crow messenger, black feather ritual, raven sermon, crow death cult"},
    {"key": "folk_horror", "name": "Folk Horror", "prompt": "wicker effigy, harvest ritual, rural cult ceremony, folk horror sacrifice, pagan industrial decay"},
    {"key": "deep_sea_gothic", "name": "Deep Sea Gothic", "prompt": "abyssal pressure ritual, drowned cathedral, bioluminescent decay, deep ocean altar, sunken church"},
    {"key": "cyber_mortuary", "name": "Cyber Mortuary", "prompt": "digital afterlife, server room funeral, LED mourning ritual, cyber death, data tomb"},
    {"key": "diesel_punk_relic", "name": "Diesel Punk Relic", "prompt": "diesel engine worship, petroleum liturgy, oil slick sacred geometry, industrial fuel ritual"},
]

# App + router
app = FastAPI()
api_router = APIRouter(prefix="/api")
limiter = Limiter(key_func=get_remote_address)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("midnightrotation")

# Concurrency: avoid hammering LLM API
generation_lock = asyncio.Lock()
_worker_task: Optional[asyncio.Task] = None
_cleanup_task: Optional[asyncio.Task] = None
_last_admin_activity: Optional[datetime] = None
ADMIN_IDLE_TIMEOUT_SECONDS = 600  # 10 minutes of no admin requests = considered idle


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
    # Anti-repetition state (internal, not user-configurable)
    recently_used_themes: List[str] = Field(default_factory=list)


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


def build_text_system_prompt(banned_words: List[str], title_formula: str, banned_names: Optional[List[str]] = None) -> str:
    base = (
        "You are a creative director for MidnightRotation, a gothic, industrial grunge, dark alternative streetwear brand. "
        "Your aesthetic is stark white ink on solid black: monolithic, religious-industrial, post-punk, occult austerity, "
        "late-night void, machinery decay, brutalist serif typography, hand-drawn ink illustration.\n\n"
        "You generate ONE shirt design capsule at a time. Return ONLY raw JSON, no prose, no code fences. Schema:\n"
        "{\n"
        "  \"capsule_name\": \"2-3 word evocative name (e.g., 'Iron Vigil', 'Hollow Hours', 'Ash Liturgy', 'Concrete Saints')\",\n"
        f"  \"title\": \"SEO product title following this exact formula: '{title_formula}'\",\n"
        "  \"front_concept\": \"STRICTLY a single, ultra-minimal MICRO-GRAPHIC for the left-chest / center-front position. Must be ONE isolated symbol or compact mark - a single sigil, a small monogram, a tiny industrial icon, a compact occult glyph, a minimal geometric mark, a single rune, a tiny seal, a clean wordmark in 1-2 letters, or a small abstract icon. Stark, clean, industrial linework only. NO scenes. NO multiple elements. NO illustrations of objects or figures. ONE symbol on a void. Keep under 15 words.\",\n"
        "  \"back_concept\": \"One sentence describing a large oversized back graphic that fills the back: detailed, monolithic gothic/industrial imagery. Keep under 25 words.\",\n"
        "  \"tags\": [\"array of EXACTLY 13 SEO tags composed as follows: "
        "(1) MUST include 'Gothic Streetwear' and 'Back Print Shirt'. "
        "(2) At least 2 gift/occasion tags tailored to this specific design — vary the phrasing naturally per capsule "
        "(e.g. 'Gift For Goth', 'Alt Streetwear Gift', 'Unique Tee Gift Idea', 'Gift For Him Alternative', "
        "'Gothic Gift For Her', 'Dark Aesthetic Gift'). "
        "(3) At least 2 broad umbrella aesthetic tags that capture high-volume searches beyond this design's specific theme "
        "(e.g. 'Dark Academia Tshirt', 'Grunge Shirt', 'Alt Clothing', 'Unisex Streetwear', 'Alternative Fashion', 'Indie Goth Tee'). "
        "(4) The remaining 7 tags must be specific to this design's theme, imagery, and mood. "
        "All 13 must read as natural, on-brand Etsy search terms — never generic or repetitive across capsules.\"]\n"
        "}\n\n"
        "For front_concept specifically: think 'a tiny ink stamp', 'a watch-dial-sized mark', 'a single hand-pulled glyph'. NEVER a full illustration. NEVER multiple visual elements. The front is a whisper; the back is a scream.\n\n"
        "For back_concept: be SPECIFIC, dense, monolithic. Reference texture, action, decay, religious or industrial machinery."
    )
    if banned_words:
        joined = ", ".join(f"'{w}'" for w in banned_words)
        base += f"\n\nABSOLUTELY DO NOT use any of these banned words or their close variants anywhere in the output: {joined}. If you would normally use them, substitute with a different, vivid alternative."
    if banned_names:
        joined_names = ", ".join(f'"{n}"' for n in banned_names[:15])
        base += f"\n\nDO NOT reuse any of these recently-generated capsule names: {joined_names}. The capsule_name you return must be completely distinct from all of these."
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
    """Return chosen theme {key/name, prompt} based on settings, avoiding recent repeats in auto mode."""
    active = (settings.get("active_theme") or "auto").strip()
    customs = settings.get("custom_themes") or []
    recently_used = set(settings.get("recently_used_themes") or [])
    all_themes = DEFAULT_THEMES + [
        {"key": f"custom:{t['name']}", "name": t["name"], "prompt": t["prompt"]}
        for t in customs
    ]
    fallback = {"key": "auto", "name": "auto", "prompt": "gothic industrial streetwear"}
    if not all_themes:
        return fallback
    if active != "auto" and active:
        for t in all_themes:
            if t["key"] == active or t["name"].lower() == active.lower():
                return t
    # auto mode: prefer themes not recently used; fall back to full pool only when all exhausted
    fresh = [t for t in all_themes if t["key"] not in recently_used]
    return random.choice(fresh if fresh else all_themes)


# -----------------------------
# AI generation
# -----------------------------
async def llm_generate_text(theme_prompt: str, banned_words: List[str], banned_names: Optional[List[str]] = None, theme_key: Optional[str] = None) -> dict:
    raw_formula = random.choice(SEO_TITLE_FORMULAS)
    theme_hint = THEME_HINT_OVERRIDES.get(theme_key or "", "Industrial Gothic")
    # Bake theme_hint in now so titles differentiate by actual theme content;
    # {capsule_name} is left as a literal placeholder for the LLM to fill in.
    title_formula = raw_formula.replace("{theme_hint}", theme_hint)
    system_prompt = build_text_system_prompt(banned_words, title_formula, banned_names)
    ban_hint = ""
    if banned_words:
        ban_hint = f" Avoid words: {', '.join(banned_words)}."
    prompt_text = f"Generate one design capsule. Lean into this seed theme: '{theme_prompt}'.{ban_hint} Return only JSON."
    response = await asyncio.to_thread(
        genai_client.models.generate_content,
        model="gemini-2.5-flash-lite",
        contents=prompt_text,
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )
    text = response.text or ""
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
    STYLE_PREFIX = "You are an expert graphic designer producing stark white-ink-on-black gothic streetwear print graphics. "
    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-2.5-flash-image",
            contents=STYLE_PREFIX + prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )
    except Exception as exc:
        logger.exception("Image generation failed, skipping: %s", exc)
        return None
    if not response.candidates:
        return None
    for part in response.candidates[0].content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            return base64.b64encode(part.inline_data.data).decode("utf-8")
    return None


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

    # Record theme usage so resolve_theme() can avoid it next time
    recent_themes = list(settings.get("recently_used_themes") or [])
    if theme["key"] not in recent_themes:
        recent_themes.append(theme["key"])
    keep = max(len(DEFAULT_THEMES) // 2, 5)
    await settings_coll.update_one(
        {"id": "config"},
        {"$set": {"recently_used_themes": recent_themes[-keep:]}},
        upsert=True,
    )

    # Fetch recent capsule names to steer Gemini away from name collisions
    recent_docs = await capsules_coll.find(
        {"capsule_name": {"$exists": True}},
        {"capsule_name": 1, "_id": 0},
    ).sort("created_at", -1).limit(15).to_list(15)
    recent_names = [d["capsule_name"] for d in recent_docs if d.get("capsule_name")]

    text_data = await llm_generate_text(
        theme["prompt"],
        settings.get("banned_words") or [],
        banned_names=recent_names,
        theme_key=theme.get("key"),
    )
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
    fallback_hint = THEME_HINT_OVERRIDES.get(theme.get("key") or "", "Industrial Gothic")
    return Capsule(
        capsule_name=capsule_name,
        title=text_data.get("title") or random.choice(SEO_TITLE_FORMULAS).format(
            capsule_name=capsule_name, theme_hint=fallback_hint
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
            if not admin_is_active():
                await asyncio.sleep(QUEUE_TICK_SECONDS)
                continue
            settings = await get_settings()
            target = int(settings.get("queue_size") or DEFAULT_QUEUE_SIZE)
            pool = await capsules_coll.count_documents({"status": "draft", "consumed": False})
            if pool < target:
                async with generation_lock:
                    pool = await capsules_coll.count_documents({"status": "draft", "consumed": False})
                    if pool < target:
                        try:
                            cap = await asyncio.wait_for(
                                _generate_capsule(settings),
                                timeout=GENERATION_TIMEOUT_SECONDS,
                            )
                            await capsules_coll.insert_one(cap.model_dump())
                            logger.info(f"Pre-warmed capsule '{cap.capsule_name}' (pool now {pool + 1}/{target})")
                        except Exception:
                            logger.exception("queue worker generation failed")
                            await asyncio.sleep(60)  # back off on quota/error
                            continue
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
# Auth dependency
# -----------------------------
def admin_is_active() -> bool:
    if _last_admin_activity is None:
        return False
    elapsed = (datetime.now(timezone.utc) - _last_admin_activity).total_seconds()
    return elapsed < ADMIN_IDLE_TIMEOUT_SECONDS


async def require_admin_key(x_admin_key: Optional[str] = Header(default=None)) -> None:
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    global _last_admin_activity
    _last_admin_activity = datetime.now(timezone.utc)


# -----------------------------
# Endpoints
# -----------------------------
@api_router.get("/")
async def root():
    return {"service": "MidnightRotation", "status": "online"}


@api_router.get("/capsules/next", response_model=Capsule)
async def next_capsule(_: None = Depends(require_admin_key)):
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
        cap = await asyncio.wait_for(
            _generate_and_store(settings, mark_consumed=True),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    return cap


@api_router.post("/capsules/generate", response_model=Capsule)
@limiter.limit("10/hour")
async def generate_capsule_now(request: Request, _: None = Depends(require_admin_key)):
    """Force-generate a fresh capsule synchronously (consumed)."""
    settings = await get_settings()
    try:
        async with generation_lock:
            cap = await asyncio.wait_for(
                _generate_and_store(settings, mark_consumed=True),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
        return cap
    except Exception as e:
        logger.exception("force generate failed")
        raise HTTPException(status_code=500, detail=str(e))


@api_router.get("/capsules/queue/status")
async def queue_status():
    pool = await capsules_coll.count_documents({"status": "draft", "consumed": False})
    settings = await get_settings()
    return {
        "depth": pool,
        "target": int(settings.get("queue_size") or DEFAULT_QUEUE_SIZE),
        "worker_active": admin_is_active(),
    }


@api_router.post("/capsules/{capsule_id}/approve", response_model=CapsulePublic)
async def approve_capsule(capsule_id: str, payload: Optional[ApprovePayload] = None, _: None = Depends(require_admin_key)):
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
            shop_id = int(settings["printify_shop_id"])
            product = await push_capsule_as_draft(
                shop_id=shop_id,
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

            # SEO refresh + immediate publish live to Etsy
            has_images = bool(full.get("front_image_b64")) and bool(full.get("back_image_b64"))
            if not has_images:
                logger.warning(
                    f"Skipping auto-publish for capsule {capsule_id} (Printify product {pid}): "
                    "missing front_image_b64 or back_image_b64"
                )
            else:
                try:
                    seo = await generate_seo(
                        full.get("capsule_name") or "",
                        full.get("back_concept") or "",
                    )
                    if seo:
                        new_title = seo.get("title", "")[:140]
                        new_tags = seo.get("tags", [])
                        new_desc = DESCRIPTION_TEMPLATE.format(
                            capsule_name=(full.get("capsule_name") or "").upper(),
                            back_graphic=(full.get("back_concept") or "").rstrip(".").lower(),
                        )
                        await update_product(shop_id, pid, {
                            "title": new_title,
                            "tags": new_tags,
                            "description": new_desc,
                        })
                    await prioritize_back_mockup(shop_id, pid)
                    await publish_product(shop_id, pid)
                    logger.info(f"Published capsule {capsule_id} (Printify product {pid}) live to Etsy")
                except Exception:
                    logger.exception(f"SEO refresh / publish failed for Printify product {pid}")

                # Best-effort: generate a themed atmospheric photo and upload it
                # directly to the matching live Etsy listing. Isolated in its own
                # try/except (including the imports) so a missing Etsy config or
                # any failure here never affects the approve/publish flow above.
                try:
                    from generate_mockups import generate_background_image, infer_theme_prompt
                    from upload_etsy_images import fetch_etsy_listings, match_etsy_listing, upload_listing_image

                    etsy_listings = await fetch_etsy_listings()
                    listing = match_etsy_listing({"title": full.get("title") or ""}, etsy_listings)
                    if not listing:
                        logger.warning(
                            f"No matching Etsy listing found for capsule {capsule_id} "
                            f"(Printify product {pid}); skipping atmospheric image"
                        )
                    else:
                        scene_prompt = infer_theme_prompt({
                            "title": full.get("title") or "",
                            "description": full.get("description") or "",
                            "tags": (full.get("tags") or []) + [full.get("theme_seed") or ""],
                        })
                        image_bytes = await generate_background_image(
                            scene_prompt, full.get("back_concept") or ""
                        )
                        if not image_bytes:
                            logger.warning(
                                f"Gemini returned no atmospheric image for capsule {capsule_id} ({pid})"
                            )
                        else:
                            cname = re.sub(
                                r"[^a-z0-9]+", "_", (full.get("capsule_name") or pid).lower()
                            ).strip("_")[:40] or pid
                            resp = await upload_listing_image(
                                listing["listing_id"], image_bytes, f"{cname}_etsy.png"
                            )
                            if resp.status_code >= 400:
                                logger.warning(
                                    f"Etsy atmospheric image upload failed for {pid}: "
                                    f"{resp.status_code} {resp.text}"
                                )
                            else:
                                logger.info(
                                    f"Uploaded atmospheric image to Etsy listing "
                                    f"{listing['listing_id']} for capsule {capsule_id} (Printify product {pid})"
                                )
                except Exception:
                    logger.exception(
                        f"Etsy atmospheric image upload failed for capsule {capsule_id} (Printify product {pid})"
                    )
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
async def deny_capsule(capsule_id: str, _: None = Depends(require_admin_key)):
    result = await capsules_coll.delete_one({"id": capsule_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Capsule not found")
    return {"ok": True, "id": capsule_id}


@api_router.post("/capsules/{capsule_id}/regenerate-image/{side}", response_model=Capsule)
@limiter.limit("10/hour")
async def regenerate_image(request: Request, capsule_id: str, side: str, _: None = Depends(require_admin_key)):
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
async def export_csv(request: Request):
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
    base_url = str(request.base_url).rstrip("/") + "/api/capsules"
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
        headers={"Content-Disposition": "attachment; filename=midnightrotation_approved.csv"},
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
async def update_settings(payload: SettingsUpdate, _: None = Depends(require_admin_key)):
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
async def printify_shops(_: None = Depends(require_admin_key)):
    try:
        shops = await list_shops()
        return {"shops": shops}
    except PrintifyError as e:
        raise HTTPException(status_code=502, detail=str(e))


@api_router.get("/printify/print-providers")
async def printify_print_providers(_: None = Depends(require_admin_key)):
    try:
        providers = await list_print_providers(GILDAN_5000_BLUEPRINT_ID)
        return {"blueprint_id": GILDAN_5000_BLUEPRINT_ID, "providers": providers}
    except PrintifyError as e:
        raise HTTPException(status_code=502, detail=str(e))


@api_router.post("/capsules/{capsule_id}/push-printify")
async def manual_push(capsule_id: str, _: None = Depends(require_admin_key)):
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
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[o for o in CORS_ORIGINS if "vercel.app" not in o],
    allow_origin_regex=r"https://printify-bulk-gnerator[a-zA-Z0-9\-]*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    global _worker_task, _cleanup_task
    await capsules_coll.create_index([("created_at", -1)])
    # Reset stale consumed=true drafts that were never approved/denied (e.g. server restart mid-review)
    # If they're old, cleanup_worker handles them. If young, leave them - user will see them again
    # by re-requesting /next. Simpler: just relaunch workers.
    # NOTE: queue_worker runs in-process. On Render's free tier the service
    # spins down after 15 min of inactivity and the worker stops; upgrade to
    # a paid Render tier for continuous pre-warming.
    _worker_task = asyncio.create_task(queue_worker())
    _cleanup_task = asyncio.create_task(cleanup_worker())


@app.on_event("shutdown")
async def on_shutdown():
    if _worker_task:
        _worker_task.cancel()
    if _cleanup_task:
        _cleanup_task.cancel()
    client.close()

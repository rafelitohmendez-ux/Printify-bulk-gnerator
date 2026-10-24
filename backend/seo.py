"""Etsy SEO rules for MidnightRotation listings (single source of truth).

Used by server.py (generation + approve), bulk_seo_update.py, autopilot.py and
create_products_from_designs.py. Pure functions, no I/O.

1. Titles lead with high-intent buyer keywords; the poetic design name comes
   after them:  [Primary keyword], [Secondary keyword] - [Design Name] | [Niche style]
   Max 140 chars (Etsy limit).
2. Tags are multi-word buyer search phrases, max 20 chars, 13 per listing.
   AI tags that read like image-prompt fragments ("Ash Cloud Graphic",
   "Bone Soot Design") are rejected and replaced from a buyer-phrase pool.
3. Description: keyword hook in sentence one, fit / print / sizing bullets,
   care instructions, AI-use disclosure (Etsy requires it), favorite-the-shop CTA.
   No fabric weights, no "typography".
"""
import random
import re
from typing import Iterable, List, Optional

ETSY_TITLE_MAX = 140
ETSY_TAG_MAX_LEN = 20
MAX_TAGS = 13
MAX_DESIGN_TAGS = 5

# ---------------------------------------------------------------------------
# Titles: "{primary}, {secondary} - {capsule_name} | {style}"
# {secondary} is a design-specific buyer phrase (e.g. "Plague Doctor Back Print Tee").
# ---------------------------------------------------------------------------
GOTHIC_TITLE_FORMULAS = [
    "Oversized Goth Shirt, {secondary} - {capsule_name} | Dark Industrial Alt Clothing",
    "Gothic Graphic Tee, {secondary} - {capsule_name} | Industrial Goth Streetwear",
    "Goth Back Print Shirt, {secondary} - {capsule_name} | Dark Alt Clothing for Men",
    "Oversized Gothic Tee, {secondary} - {capsule_name} | Post Punk Industrial Apparel",
    "Gothic Back Print Tee, {secondary} - {capsule_name} | Grunge Alt Streetwear",
]
CYBER_TITLE_FORMULAS = [
    "Cyber Goth Shirt, {secondary} - {capsule_name} | Industrial Techno Streetwear",
    "Cybergoth Back Print Tee, {secondary} - {capsule_name} | Techwear Gothic Clothing",
    "Cyberpunk Gothic Shirt, {secondary} - {capsule_name} | Dark Techwear Unisex",
    "Oversized Cyber Goth Tee, {secondary} - {capsule_name} | Rave Goth Streetwear",
]
DEFAULT_SECONDARY = {"gothic": "Back Print Graphic Tee", "cyber": "Back Print Graphic Tee"}
SECONDARY_MAX_LEN = 34

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
REQUIRED_TAGS = ["Oversized Goth Shirt", "Back Print Shirt"]

# Buyer search phrases used to fill slots after required + design tags, in order.
CORE_TAGS = {
    "gothic": [
        "Gothic Graphic Tee", "Graphic Tee Men", "Alt Clothing Men", "Gothic Apparel",
        "Industrial Music Tee", "Goth Streetwear", "Post Punk Shirt", "Grunge Shirt",
        "Goth Gift For Him", "Occult Shirt", "Dark Aesthetic Tee",
    ],
    "cyber": [
        "Cyber Goth Shirt", "Cybergoth Clothing", "Techwear Shirt", "Rave Goth Tee",
        "Industrial Music Tee", "Graphic Tee Men", "Alt Clothing Men", "Gothic Apparel",
        "Cyberpunk Shirt", "Goth Gift For Him", "Gothic Graphic Tee",
    ],
}

# A design tag must name a product so it reads like a buyer search
# ("Plague Doctor Shirt") rather than an AI prompt fragment ("Bone Soot Design").
PRODUCT_WORDS = {
    "shirt", "shirts", "tee", "tees", "tshirt", "t-shirt", "top", "clothing",
    "apparel", "streetwear", "gift", "fashion", "outfit", "merch", "hoodie",
}
FRAGMENT_ENDINGS = {
    "graphic", "graphics", "design", "art", "artwork", "illustration",
    "motif", "imagery", "concept", "print", "texture", "scene",
}

CYBER_WORDS = ("cyber", "techno", "techwear", "glitch", "circuit", "neon", "rave", "data", "server",
               "digital", "pixel", "neural", "y2k", "biohazard", "signal", "chrome", "wired")


def infer_category(text: str) -> str:
    """'cyber' if the text reads cyber/techno, else 'gothic'. For products with no theme metadata."""
    t = (text or "").lower()
    return "cyber" if any(w in t for w in CYBER_WORDS) else "gothic"


def _cat(category: Optional[str]) -> str:
    return "cyber" if category == "cyber" else "gothic"


def title_formulas_for(category: Optional[str]) -> List[str]:
    return CYBER_TITLE_FORMULAS if _cat(category) == "cyber" else GOTHIC_TITLE_FORMULAS


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip()).strip(" -|,")
    return name or "Untitled"


def clean_secondary(phrase: Optional[str], category: Optional[str] = None) -> str:
    """Validate the design-specific keyword; fall back to a generic buyer phrase."""
    p = re.sub(r"\s+", " ", str(phrase or "")).strip(" -|,.")
    p = re.sub(r"[|]", " ", p)
    words = p.split()
    if (not p or len(p) > SECONDARY_MAX_LEN or not 2 <= len(words) <= 5
            or not any(w.lower() in PRODUCT_WORDS for w in words)):
        return DEFAULT_SECONDARY[_cat(category)]
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


def build_title(capsule_name: str, secondary: Optional[str] = None, category: Optional[str] = None,
                rng: Optional[random.Random] = None, formula: Optional[str] = None) -> str:
    rng = rng or random
    formula = formula or rng.choice(title_formulas_for(category))
    title = formula.format(capsule_name=clean_name(capsule_name), secondary=clean_secondary(secondary, category))
    if len(title) <= ETSY_TITLE_MAX:
        return title
    head = title.rsplit(" | ", 1)[0]  # drop the style segment first
    if len(head) <= ETSY_TITLE_MAX:
        return head
    return head[:ETSY_TITLE_MAX].rstrip(" -|,")


def filter_etsy_tags(tags: Iterable[str]) -> List[str]:
    """Drop tags over Etsy's 20-char limit (never truncate mid-word) and case-insensitive duplicates."""
    seen = set()
    kept = []
    for t in tags or []:
        t = re.sub(r"\s+", " ", str(t)).strip()
        if t and len(t) <= ETSY_TAG_MAX_LEN and t.lower() not in seen:
            seen.add(t.lower())
            kept.append(t)
    return kept


def is_buyer_tag(tag: str) -> bool:
    """Multi-word phrase that names a product and doesn't end like an image-prompt fragment."""
    words = re.sub(r"[^a-z0-9' -]", " ", tag.lower()).split()
    if len(words) < 2 or words[-1] in FRAGMENT_ENDINGS:
        return False
    return any(w in PRODUCT_WORDS for w in words)


def _banned(tag: str, banned_words: Iterable[str]) -> bool:
    low = tag.lower()
    return any(w and re.search(rf"\b{re.escape(w.strip().lower())}", low) for w in banned_words or [])


def build_tags(design_tags: Optional[Iterable[str]] = None, category: Optional[str] = None,
               banned_words: Optional[Iterable[str]] = None) -> List[str]:
    """Required tags, then up to MAX_DESIGN_TAGS valid design tags, then the category's core pool. 13 total."""
    banned_words = list(banned_words or [])
    design = [t for t in filter_etsy_tags(design_tags or [])
              if is_buyer_tag(t) and not _banned(t, banned_words)
              and t.lower() not in {r.lower() for r in REQUIRED_TAGS}]
    core = [t for t in CORE_TAGS[_cat(category)] if not _banned(t, banned_words)]
    return filter_etsy_tags(REQUIRED_TAGS + design[:MAX_DESIGN_TAGS] + core)[:MAX_TAGS]


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------
DESCRIPTION_TEMPLATE = """Oversized gothic graphic tee with a full back print. {NAME} brings stark white-ink industrial goth art to your everyday alt wardrobe.

The back carries a large, detailed illustration of {back_subject}. The front keeps it quiet with a small, minimal left-chest mark. Made for late nights, shows, and daily rotation.

THE DETAILS
• Fit: Gildan 5000 Heavy Cotton Tee, with a classic boxy streetwear cut
• Print: DTG printed in bright white ink on solid black for sharp, high-contrast detail
• Material: 100% cotton
• Sizing: True to size. Size up 1–2 for an oversized look. Check the size chart in the listing photos before ordering.

CARE
• Machine wash cold, inside out, with like colors
• Tumble dry low or hang dry
• Do not iron directly on the print
• Do not dry clean

ABOUT THE DESIGN
Original MidnightRotation artwork, created with AI-assisted design tools, then selected, edited and finalized by hand. {photo_note}

MADE TO ORDER
Each shirt is printed after you order. Production and shipping times are shown at checkout.

Favorite the MidnightRotation shop to catch new drops as they enter the rotation."""

FALLBACK_BACK_SUBJECT = "monolithic gothic industrial imagery"

PHOTO_NOTE_MOCKUPS = "Listing photos are product mockups of this exact print on a black Gildan 5000 tee."
PHOTO_NOTE_WITH_AI_SCENE = (
    "Listing photos are product mockups of this exact print on a black Gildan 5000 tee, except the "
    "styled scene photo, which is an AI-generated illustration; use the mockups to judge print size and placement."
)

_LEADING_FILLER = re.compile(
    r"^(?:(?:an?|the)\s+)?(?:(?:large|oversized|detailed|huge|massive|monolithic|aggressive)[,\s]+)*"
    r"(?:back\s+)?(?:illustration|graphic|image|print|depiction|drawing|artwork)\s+(?:of|showing|depicting)\s+",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r",|;|\s+—\s+|\s+-\s+|\s+(?:that|which|while|as)\s+", re.IGNORECASE)


def clean_back_subject(phrase: str, max_words: int = 18) -> str:
    """Turn a back-graphic phrase into a noun phrase for 'a large, detailed illustration of ___.'"""
    p = re.sub(r"\s+", " ", (phrase or "").strip()).strip(" .!;:\"'")
    p = re.sub(r"^(?:depicting|showing|featuring)\s+", "", p, flags=re.IGNORECASE)
    p = _LEADING_FILLER.sub("", p)
    p = re.sub(r"^(?:an?|the)\s+(?=(?:an?|the)\s)", "", p, flags=re.IGNORECASE)  # "an a ..." -> "a ..."
    if len(p.split()) > max_words:
        p = _CLAUSE_SPLIT.split(p, maxsplit=1)[0]
        p = " ".join(p.split()[:max_words])
    p = p.strip(" ,.;:-")
    p = re.sub(r"\btypography\b", "lettering", p, flags=re.IGNORECASE)
    if not p:
        return FALLBACK_BACK_SUBJECT
    if not (len(p) > 1 and p[1].isupper()):  # keep acronyms like "Y2K"
        p = p[0].lower() + p[1:]
    return p


def build_description(capsule_name: str, back_phrase: str, ai_scene_photo: bool = False) -> str:
    """ai_scene_photo: True when the listing also shows an AI-generated lifestyle/scene photo."""
    return DESCRIPTION_TEMPLATE.format(
        NAME=clean_name(capsule_name).upper(),
        back_subject=clean_back_subject(back_phrase),
        photo_note=PHOTO_NOTE_WITH_AI_SCENE if ai_scene_photo else PHOTO_NOTE_MOCKUPS,
    )


_OLD_BACK_RE = re.compile(r"aggressive, oversized (.+?) filling the back", re.DOTALL)
_NEW_BACK_RE = re.compile(r"detailed illustration of (.+?)\. The front keeps", re.DOTALL)


def extract_back_concept(description: str) -> str:
    """Back-graphic phrase from a listing description (old 'THE GRIND' template or the current one)."""
    for rx in (_NEW_BACK_RE, _OLD_BACK_RE):
        m = rx.search(description or "")
        if m:
            return m.group(1).strip()
    return ""


def extract_capsule_name(title: str) -> str:
    """Design name from a listing title.

    Current titles: '{primary}, {secondary} - {name} | {style}'.
    Old titles started with the name: '{name} - ...' / '{name} | ...' / '{name} Tee ...'.
    """
    t = (title or "").strip()
    head = t.split(" - ", 1)[0]
    if "," in head and " - " in t:
        return t.split(" - ", 1)[1].split(" | ", 1)[0].strip()
    name = re.split(r"[-|]", t)[0].strip()
    return re.sub(r"\s+(?:[Tt]ee|[Ss]hirt)$", "", name).strip()

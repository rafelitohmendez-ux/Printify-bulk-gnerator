"""Offline tests for seo.py: keyword-first titles, buyer-phrase tags, clean description."""
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import seo  # noqa: E402


# ---------- Titles ----------
def test_ash_communion_title_is_keyword_first():
    t = seo.build_title("Ash Communion", "Burnt Chalice Graphic Tee", "gothic",
                        formula=seo.GOTHIC_TITLE_FORMULAS[0])
    assert t == ("Oversized Goth Shirt, Burnt Chalice Graphic Tee - Ash Communion | "
                 "Dark Industrial Alt Clothing")
    assert len(t) <= 140


def test_every_formula_starts_with_buyer_keywords():
    for f in seo.GOTHIC_TITLE_FORMULAS + seo.CYBER_TITLE_FORMULAS:
        t = f.format(capsule_name="Hydraulic Crucifixion Liturgy", secondary="Diesel Punk Relic Graphic Tee")
        assert len(t) <= 140, t
        assert not t.startswith("Hydraulic")
        assert re.search(r"goth|cyber", " ".join(t.split()[:3]), re.I), t


def test_title_never_exceeds_limit():
    assert len(seo.build_title("X" * 200, "Back Print Tee", "cyber", random.Random(1))) <= 140


def test_secondary_keyword_validation():
    assert seo.clean_secondary("plague doctor back print tee") == "Plague Doctor Back Print Tee"
    assert seo.clean_secondary("Bone Soot Design") == seo.DEFAULT_SECONDARY["gothic"]  # no product word
    assert seo.clean_secondary("Tee") == seo.DEFAULT_SECONDARY["gothic"]  # too short
    assert seo.clean_secondary("x " * 30 + "tee") == seo.DEFAULT_SECONDARY["gothic"]  # too long
    assert seo.clean_secondary("Y2K Glitch Saint Tee") == "Y2K Glitch Saint Tee"


def test_extract_capsule_name_old_and_new_titles():
    assert seo.extract_capsule_name("Oversized Goth Shirt, Burnt Chalice Graphic Tee - Ash Communion | Dark Alt") == "Ash Communion"
    assert seo.extract_capsule_name("Iron Chapel | Midnight Rotation Drop | Gothic Back Print Heavy Tee") == "Iron Chapel"
    assert seo.extract_capsule_name("Neon Wake Tee - Dark Alt Streetwear | Oversized Back Graphic") == "Neon Wake"
    assert seo.extract_capsule_name("Toxic Sovereign - Cybergoth Oversized Tee | Biohazard Graphic") == "Toxic Sovereign"


def test_infer_category():
    assert seo.infer_category("Static Rite Tee | Cyberpunk Gothic Back Print") == "cyber"
    assert seo.infer_category("Bone Chapel | Gothic Back Print Heavy Tee") == "gothic"


# ---------- Tags ----------
def test_static_tags_fit_etsy_limits():
    for t in seo.REQUIRED_TAGS + seo.CORE_TAGS["gothic"] + seo.CORE_TAGS["cyber"]:
        assert len(t) <= 20, t
        assert len(t.split()) >= 2, t
    for cat in ("gothic", "cyber"):
        assert len(seo.REQUIRED_TAGS) + len(seo.CORE_TAGS[cat]) >= 13


def test_prompt_fragment_tags_rejected():
    tags = seo.build_tags(["Ash Cloud Graphic", "Bone Soot Design", "Skull Shirt", "Chalice Goth Tee"], "gothic")
    assert "Ash Cloud Graphic" not in tags and "Bone Soot Design" not in tags
    assert "Skull Shirt" in tags and "Chalice Goth Tee" in tags
    assert len(tags) == 13 and len({t.lower() for t in tags}) == 13


# ---------- Description ----------
def test_no_doubled_article_or_typography():
    for phrase in [
        "A colossal rusted crucifix fused with hydraulic pistons, oozing black oil.",
        "an illustration of a burnt chalice dissolving into ash",
        "A large, detailed illustration of an iron saint",
        "a saint wrapped in typography and chains",
        "",
    ]:
        d = seo.build_description("Ash Communion", phrase)
        assert not re.search(r"\b(a|an) (a|an)\b", d, re.IGNORECASE), d
        assert "illustration of illustration" not in d
        assert "typography" not in d.lower()
        assert "ASH COMMUNION" in d


def test_description_contents():
    d = seo.build_description("Ash Communion", "a burnt chalice dissolving into ash")
    first_sentence = d.split(".")[0].lower()
    assert "oversized" in first_sentence and "gothic graphic tee" in first_sentence
    for needle in ["Gildan 5000", "white ink", "True to size", "Machine wash cold",
                   "Favorite the MidnightRotation shop"]:
        assert needle in d
    assert not re.search(r"\b\d+(\.\d+)?\s?oz\b", d, re.IGNORECASE)


def test_extract_back_concept_round_trips_and_reads_old_template():
    d = seo.build_description("Ash Communion", "a burnt chalice dissolving into ash")
    assert seo.extract_back_concept(d) == "a burnt chalice dissolving into ash"
    old = ("THE GRIND // IRON CHAPEL\n\nA premium... front and an aggressive, oversized "
           "iron chapel of pistons filling the back.")
    assert seo.extract_back_concept(old) == "iron chapel of pistons"

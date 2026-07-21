"""Stage 3: Copywriter.

Only runs on leads with a real found email. combat_sports uses a deterministic
template built from config/real_specs_combat_sports.json (Hamad's real example
structure) with the LLM scoped to ONLY the opening personalization line, so the
three named models, subject, and CTA never drift from what's verified. moto_apparel
still uses the older open-ended prompt pending its own spec verification.

Usage:
  python scripts/copywriter.py moto_apparel
  python scripts/copywriter.py combat_sports
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.db import get_client
from common.groq_client import generate
from common.parsing import extract_json
from common.regions import language_for_region

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
ASSETS_DIR = BASE_DIR / "assets" / "catalogue"

SIGNATURE = "\n\nHamad, Relian MFG"

# ---------- moto_apparel: unchanged, still open-ended pending spec verification ----------

PROMPT_VERIFIED = """Write a short, specific cold outreach email pitching manufacturing
partnership from Relian MFG (Sialkot, Pakistan) to {brand_name}.

Reference this real, specific detail about them — do not write a generic template:
"{research_notes}"

You may reference these VERIFIED manufacturing capabilities where relevant:
{specs_json}

Rules:
- 120-180 words. No bullet lists of every spec — weave in only what's relevant to a
  reasonable outreach pitch a busy person will actually read.
- Naturally mention that a couple of product photos are attached for reference — one short
  clause, not a separate paragraph.
- End with a light, low-pressure call to action (e.g. offer a sample or a short call).
- Do NOT add any sign-off or signature line — that gets appended separately.
- Output ONLY the email body text (no subject line, no preamble, no markdown).
"""

PROMPT_UNVERIFIED = """Write a short, specific cold outreach email pitching manufacturing
partnership from Relian MFG (Sialkot, Pakistan) to {brand_name}.

Reference this real, specific detail about them — do not write a generic template:
"{research_notes}"

Do NOT state any specific material, certification, MOQ, or lead-time claim — those are not
yet verified. Use only a generic capability statement (e.g. "we manufacture technical
apparel/gear to spec for brands like yours" — no numbers, no named materials).

Rules:
- 100-150 words. Reference the specific detail above naturally.
- Naturally mention that a couple of product photos are attached for reference — one short
  clause, not a separate paragraph.
- End with a light, low-pressure call to action (e.g. offer a sample or a short call).
- Do NOT add any sign-off or signature line — that gets appended separately.
- Output ONLY the email body text (no subject line, no preamble, no markdown).
"""

SUBJECT_TEMPLATES = {
    "moto_apparel": "Manufacturing partner for {brand_name}?",
}

# ---------- combat_sports: deterministic template (per-language), LLM only writes the
# opening line. DE/FR/ES/IT templates are AI-drafted, not yet native-speaker-reviewed —
# see CHANGELOG. Every generated icebreaker is run through validate_icebreaker() below
# before use, in every language, so an off-register or hallucinated line never ships.

EMAIL_TEMPLATES_PATH = CONFIG_DIR / "email_templates_combat_sports.json"

ICEBREAKER_PROMPTS = {
    "en": """Write ONE short, plain sentence (max 25 words) opening a cold
email to "{brand_name}". Ground it in this real detail if usable: "{research_notes}".
Never imply their current supplier/product is inferior. If the detail isn't
usable, write a soft honest opener instead. Output ONLY that one sentence.""",
    "de": """Schreiben Sie GENAU EINEN kurzen, sachlichen Satz (max. 25 Wörter)
als Einstieg einer Geschäfts-E-Mail an "{brand_name}". Verwenden Sie AUSSCHLIESSLICH
die formelle Anrede (Sie/Ihr/Ihnen) — niemals "du". Kein Marketing-Superlativ,
kein "revolutionär". Beziehen Sie sich auf dieses reale Detail, falls brauchbar:
"{research_notes}". Implizieren Sie NIEMALS, dass der aktuelle Lieferant/das
Produkt minderwertig ist. Antworten Sie NUR mit diesem einen Satz.""",
    "fr": """Écrivez EXACTEMENT une phrase courte et factuelle (max 25 mots)
pour ouvrir un e-mail professionnel à "{brand_name}". Utilisez UNIQUEMENT le
vouvoiement (vous/votre) — jamais "tu". Pas de superlatifs marketing, pas de
"révolutionnaire". Appuyez-vous sur ce détail réel si utilisable :
"{research_notes}". N'impliquez JAMAIS que leur fournisseur/produit actuel est
inférieur. Répondez UNIQUEMENT avec cette phrase.""",
    "es": """Escriba EXACTAMENTE una frase breve y objetiva (máximo 25 palabras)
para abrir un correo profesional a "{brand_name}". Use ÚNICAMENTE el registro
formal (usted/su) — nunca "tú". Sin superlativos de marketing, sin
"revolucionario". Básese en este detalle real si es útil:
"{research_notes}". NUNCA dé a entender que su proveedor/producto actual es
inferior. Responda ÚNICAMENTE con esa frase.""",
    "it": """Scriva ESATTAMENTE una frase breve e oggettiva (massimo 25 parole)
per aprire un'email professionale a "{brand_name}". Usi SOLO il registro
formale (Lei/Suo) — mai "tu". Nessun superlativo di marketing, nessun
"rivoluzionario". Si basi su questo dettaglio reale se utile:
"{research_notes}". Non implichi MAI che il fornitore/prodotto attuale sia
inferiore. Risponda SOLO con quella frase.""",
}

# Every language's forbidden-informal check, plus the length/script checks in
# validate_icebreaker(), run on EVERY generated icebreaker regardless of language.
FORBIDDEN_INFORMAL = {
    "de": [" du ", " dich ", " dir ", " dein", " deine"],
    "fr": [" tu ", " te ", " toi ", " ton ", " ta ", " tes "],
    "es": [" tú ", " tuyo", " tuya"],
    "it": [" tu ", " tuo", " tua", " tuoi", " tue"],
}

# Safe, honest, formal-register openers used only if two generation attempts both fail
# validation — no personalization claim, so they're never a lie regardless of the lead.
FALLBACK_ICEBREAKERS = {
    "en": "I came across your business and wanted to reach out directly.",
    "de": "Ich bin auf Ihr Unternehmen aufmerksam geworden und wollte Sie direkt kontaktieren.",
    "fr": "Votre entreprise a attiré notre attention et nous souhaitions vous contacter directement.",
    "es": "Conocimos su empresa y quisimos ponernos en contacto con usted directamente.",
    "it": "Abbiamo scoperto la sua azienda e desideravamo contattarla direttamente.",
}


def validate_icebreaker(lang: str, text: str) -> bool:
    if not text or len(text.strip()) < 10 or len(text.split()) > 40:
        return False
    if re.search(r"[Ѐ-ӿ一-鿿぀-ヿ]", text):
        return False  # hallucinated Cyrillic/CJK/Japanese script
    lowered = f" {text.lower()} "
    for term in FORBIDDEN_INFORMAL.get(lang, []):
        if term in lowered:
            return False
    return True


def generate_icebreaker(brand_name: str, research_notes: str, lang: str) -> str:
    prompt = ICEBREAKER_PROMPTS[lang].format(brand_name=brand_name, research_notes=research_notes)

    for _attempt in range(2):
        text = generate(prompt, max_tokens=100).strip()
        if validate_icebreaker(lang, text):
            return text

    print(
        f"[copywriter] icebreaker validation FAILED twice for lang={lang!r} "
        f"brand={brand_name!r} — using safe fallback line"
    )
    return FALLBACK_ICEBREAKERS[lang]


def load_email_templates() -> dict:
    return json.loads(EMAIL_TEMPLATES_PATH.read_text(encoding="utf-8"))


def build_combat_sports_email(brand_name: str, research_notes: str, target_language: str) -> tuple[str, str]:
    template = load_email_templates()[target_language]
    opening = generate_icebreaker(brand_name, research_notes, target_language)

    models_block = "\n".join(f"{i}. {m}" for i, m in enumerate(template["models"], start=1))

    # Lookbook attachment mention only exists in English so far — never carried into the
    # DE/FR/ES/IT templates untranslated, since that would leak English into the email.
    lookbook_path = ASSETS_DIR / "lookbook_hand_wraps.pdf"
    next_step_line = (
        "\n\n**Next Step:** I have attached our Lookbook for hand wraps."
        if target_language == "en" and lookbook_path.exists()
        else ""
    )

    body = (
        f"{template['greeting']}\n\n"
        f"{opening}\n\n"
        f"{template['intro']}\n\n{models_block}"
        f"{next_step_line}\n\n"
        f"{template['cta']}"
        f"\n\n{template['signoff']}"
    )
    return template["subject"], body


# ---------- shared: image selection ----------

IMAGE_SELECT_PROMPT = """A lead named "{brand_name}" is getting a manufacturing pitch email.
What we know about them: "{research_notes}"

Here is our available product photo library for this vertical (filename: description):
{catalogue_list}

Pick the 1-2 images most relevant to this specific lead (e.g. if they mention gloves, pick
glove photos; if unclear, pick a representative mix). Reply with ONLY a fenced ```json code
block: a JSON array of 1-2 filename strings taken exactly from the list above. No other text.
"""


def load_specs(vertical: str) -> dict:
    path = CONFIG_DIR / f"real_specs_{vertical}.json"
    return json.loads(path.read_text())


def load_catalogue(vertical: str) -> list[dict]:
    path = CONFIG_DIR / f"catalogue_{vertical}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def select_images(vertical: str, brand_name: str, research_notes: str, catalogue: list[dict]) -> list[str]:
    if not catalogue:
        return []
    catalogue_list = "\n".join(f"- {c['filename']}: {c['description']}" for c in catalogue)
    prompt = IMAGE_SELECT_PROMPT.format(
        brand_name=brand_name, research_notes=research_notes, catalogue_list=catalogue_list
    )
    valid_filenames = {c["filename"] for c in catalogue}
    try:
        raw = generate(prompt, max_tokens=256)
        picked = extract_json(raw)
    except Exception as exc:  # noqa: BLE001 — bad image pick shouldn't block the draft
        print(f"[copywriter] image selection failed for {brand_name!r}: {exc}")
        return []
    return [f for f in picked if f in valid_filenames][:2]


def run(vertical: str) -> None:
    db = get_client()
    specs = load_specs(vertical)
    verified = specs.get("verified_by_hamad", False)
    catalogue = load_catalogue(vertical)

    leads = (
        db.table("outreach_leads")
        .select("*")
        .eq("vertical", vertical)
        .eq("status", "researched")
        .eq("contact_method", "email")
        .not_.is_("contact_email", "null")
        .not_.is_("research_notes", "null")
        .execute()
        .data
    )
    print(f"[copywriter] vertical={vertical} pending={len(leads)} verified_by_hamad={verified}")

    for lead in leads:
        try:
            notes_suffix = ""
            target_language = None

            if vertical == "combat_sports" and verified:
                target_language = language_for_region(lead["region"])
                subject, body = build_combat_sports_email(
                    lead["brand_name"], lead["research_notes"], target_language
                )
            elif verified:
                prompt = PROMPT_VERIFIED.format(
                    brand_name=lead["brand_name"],
                    research_notes=lead["research_notes"],
                    specs_json=json.dumps(specs, indent=2),
                )
                body = generate(prompt).strip() + SIGNATURE
                subject = SUBJECT_TEMPLATES[vertical].format(brand_name=lead["brand_name"])
            else:
                prompt = PROMPT_UNVERIFIED.format(
                    brand_name=lead["brand_name"], research_notes=lead["research_notes"]
                )
                body = generate(prompt).strip() + SIGNATURE
                subject = SUBJECT_TEMPLATES.get(
                    vertical, "Manufacturing partner for {brand_name}?"
                ).format(brand_name=lead["brand_name"])
                notes_suffix = " AWAITING SPEC VERIFICATION."
        except Exception as exc:  # noqa: BLE001 — e.g. Groq rate limit; retry next run, don't
            # crash the whole job and skip every remaining lead plus the Sender/Summary steps.
            print(f"[copywriter] failed for {lead['domain']}: {exc}")
            continue

        images = select_images(vertical, lead["brand_name"], lead["research_notes"], catalogue)

        update_payload = {
            "draft_subject": subject,
            "draft_body": body,
            "catalogue_images": images,
            "research_notes": lead["research_notes"] + notes_suffix,
            "status": "drafted",
            "drafted_at": datetime.now(timezone.utc).isoformat(),
        }
        if target_language:
            update_payload["target_language"] = target_language

        db.table("outreach_leads").update(update_payload).eq("id", lead["id"]).execute()

        print(f"[copywriter] drafted {lead['domain']} images={images} target_language={target_language}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("moto_apparel", "combat_sports"):
        sys.exit("Usage: python scripts/copywriter.py <moto_apparel|combat_sports>")
    run(sys.argv[1])

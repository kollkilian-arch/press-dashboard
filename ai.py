import os
import json
import re
from openai import OpenAI
import categorizer
import database as db

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_MODEL = "openai/gpt-oss-120b:free"
AVAILABLE_MODELS = [
    ("openai/gpt-oss-120b:free", "GPT OSS 120B (OpenAI) – aktuell ausgewählt"),
    ("openrouter/free",          "OpenRouter Auto – bestes verfügbares Free-Modell"),
]

CATEGORIES = ("eigene_produkte", "markt", "wettbewerber", "sonstige")
CATEGORY_LABELS = {
    "eigene_produkte": "Eigene Produkte",
    "markt":           "Markt & Regulierung",
    "wettbewerber":    "Wettbewerber",
    "sonstige":        "Sonstiges",
}

PROMPT_ARTICLE = """Du bist Marktintelligenz-Analyst einer deutschen Versicherungsgesellschaft.

Analysiere den folgenden Artikel gruendlich:

TITEL: {title}
INHALT: {snippet}

---

AUFGABE: Schreibe eine ausfuehrliche Analyse mit mindestens 150 Woertern im Feld "summary".
Die Analyse besteht aus 2-3 Absaetzen, getrennt durch eine Leerzeile (\\n\\n):

WICHTIGE GROUNDING-REGELN:
- Nutze ausschliesslich Fakten, Zahlen, Namen, Daten und Ereignisse, die im TITEL oder INHALT stehen.
- Erfinde keine Fakten, Zahlen, Zitate, Quellen, Ursachen oder Folgen hinzu.
- Wenn eine Information im bereitgestellten Text fehlt oder unklar ist, schreibe das transparent statt zu spekulieren.
- Verwende Markt- oder Branchenwissen nur zur vorsichtigen Einordnung, aber nicht als Quelle fuer neue konkrete Tatsachen.
- Pruefe vor der Antwort intern, ob jede konkrete Aussage im summary-Feld durch TITEL oder INHALT gedeckt ist.

  Absatz 1 – Was ist passiert?
  Schildere die Kernaussage ausfuehrlich: konkrete Fakten, Zahlen, beteiligte Unternehmen/Behoerden, Zeitpunkt.
  Mindestens 3-4 Saetze.

  Absatz 2 – Kontext und Einordnung
  Warum ist das relevant? Einordnung in den groesseren Markt-, Regulierungs- oder Wettbewerbskontext.
  Was hat dazu gefuehrt, was ist der Hintergrund?
  Mindestens 3-4 Saetze.

  Absatz 3 – Bedeutung fuer die Versicherungsbranche (nur wenn relevant)
  Welche konkreten Auswirkungen oder Handlungsimplikationen ergeben sich fuer Versicherer?
  Was sollte die Branche beobachten oder pruefen?

Antworte ausschliesslich mit dem folgenden JSON-Objekt – kein Text davor oder danach:
{{
  "summary": "<Absatz 1>\\n\\n<Absatz 2>\\n\\n<Absatz 3>",
  "category": "eigene_produkte oder markt oder wettbewerber oder sonstige",
  "priority": "hoch oder mittel oder niedrig",
  "tags": ["tag1", "tag2", "tag3"]
}}

Kategorien:
- eigene_produkte: eigene Produkte, Leistungen oder Aktivitaeten des eigenen Hauses
- markt: Markttrends, Regulierung, BaFin, GDV, Branche allgemein
- wettbewerber: Konkurrenten (Allianz, AXA, Generali, Zurich, Munich Re, Talanx, HDI, Ergo, R+V, Debeka usw.)
- sonstige: alles andere

Prioritaet:
- hoch: Regulierungsaenderung, Konkurrenz-Krise oder Markteinführung, BaFin/GDV-Bekanntmachung, direkter Markteinfluss
- mittel: Relevante Branchennews, Wettbewerber-Update, bemerkenswerter Trend
- niedrig: Allgemeine Hintergrundinfo, Routine-Pressemitteilung, geringe Relevanz

Tags: 3-5 kleingeschriebene deutsche Schlagwoerter, keine Sonderzeichen."""

PROMPT_REPORT = """Du bist Marktintelligenz-Analyst einer deutschen Versicherungsgesellschaft.

Erstelle einen Tagesbericht fuer den {date} ({total} Artikel aus verschiedenen Quellen).

{articles_text}

Antworte ausschliesslich mit einem JSON-Objekt (kein Markdown, keine Erklaerungen):
{{
  "zusammenfassung": "3-5 Saetze Executive Summary des Tages",
  "abschnitte": [
    {{
      "titel": "Abschnittsname",
      "kategorie": "markt oder wettbewerber oder eigene_produkte oder sonstige",
      "inhalt": "2-4 Saetze mit konkreten Fakten, Unternehmen und Zahlen"
    }}
  ],
  "top_themen": ["Thema 1", "Thema 2", "Thema 3", "Thema 4", "Thema 5"],
  "einschaetzung": "1-2 Saetze strategische Einschaetzung fuer die Versicherungsbranche"
}}

Nur Abschnitte fuer Kategorien mit vorhandenen Artikeln. Schreibe auf Deutsch."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    key = db.get_setting("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY", "")
    return key.strip()


def _get_model() -> str:
    return db.get_setting("openrouter_model") or DEFAULT_MODEL


def _make_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=_get_api_key(),
        timeout=float(os.environ.get("OPENROUTER_TIMEOUT", "45")),
    )


def _call(prompt: str, system: str = None, max_tokens: int = None,
          json_mode: bool = False, temperature: float = 0.6) -> str:
    """Send a prompt, return the raw text response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = dict(
        model=_get_model(),
        messages=messages,
        temperature=temperature,
    )
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = _make_client().chat.completions.create(**kwargs)
    except Exception as exc:
        if json_mode and "response_format" in str(exc).lower():
            return _call(
                prompt,
                system=system,
                max_tokens=max_tokens,
                json_mode=False,
                temperature=temperature,
            )
        raise
    content = response.choices[0].message.content
    if not content or not content.strip():
        finish = getattr(response.choices[0], "finish_reason", "unknown")
        raise ValueError(
            f"Modell hat eine leere Antwort zurückgegeben (finish_reason={finish}). "
            "Bitte erneut versuchen oder in Einstellungen ein anderes Modell wählen."
        )
    return content


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()


def _parse_json(text: str) -> dict:
    """
    Parse JSON from the model response robustly:
    1. Strip markdown fences (```json … ```)
    2. Try direct parse
    3. Fall back to extracting the first {...} block from the text
       (some models wrap JSON in a sentence)
    """
    text = _strip_markdown_fences(text)

    # Direct parse — the happy path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract the first JSON object embedded in surrounding prose
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    preview = text[:300] if len(text) > 300 else text
    raise ValueError(
        f"Modell hat kein gültiges JSON zurückgegeben. "
        f"Antwort (Anfang): {preview!r}"
    )


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    if "429" in msg or "rate" in msg.lower() or "RESOURCE_EXHAUSTED" in msg:
        m = re.search(r"(\d+)\s*s(?:econds?)?", msg, re.IGNORECASE)
        wait = f" Bitte {m.group(1)} Sekunden warten." if m else ""
        return f"Rate-Limit erreicht (429).{wait} Tipp: sparsameres Modell in Einstellungen wählen."
    if "401" in msg or "unauthorized" in msg.lower():
        return "Ungültiger API-Schlüssel. Bitte unter Einstellungen prüfen."
    if "402" in msg or "payment" in msg.lower():
        return "Kein Guthaben auf dem OpenRouter-Konto. Free-Modelle erfordern kein Guthaben."
    return msg


def _fallback_tags(text: str) -> list:
    stopwords = {
        "oder", "und", "der", "die", "das", "den", "dem", "des", "ein", "eine",
        "einer", "eines", "mit", "für", "zur", "zum", "auf", "aus", "bei",
        "von", "vor", "nach", "sich", "sind", "ist", "als", "auch", "wird",
        "werden", "durch", "über", "mehr",
    }
    words = re.findall(r"[a-zäöüß]{4,}", text.lower())
    tags = []
    for word in words:
        if word not in stopwords and word not in tags:
            tags.append(word)
        if len(tags) >= 5:
            break
    return tags or ["analyse"]


def _normalize_article_data(data: dict, title: str, snippet: str,
                            fallback_summary: str = "") -> dict:
    summary = str(data.get("summary", "")).strip()
    if not summary:
        summary = _strip_markdown_fences(fallback_summary)
    if not summary:
        summary = "Keine verwertbare KI-Zusammenfassung erhalten."

    category = str(data.get("category", "")).strip()
    if category not in CATEGORIES:
        category = categorizer.classify(f"{title} {snippet}", "sonstige")

    priority = str(data.get("priority", "niedrig")).strip().lower()
    if priority not in ("hoch", "mittel", "niedrig"):
        priority = "niedrig"

    tags = [
        str(t).lower().strip()
        for t in data.get("tags", [])
        if str(t).strip()
    ][:5]
    if not tags:
        tags = _fallback_tags(f"{title} {snippet} {summary}")

    return {"summary": summary, "category": category, "priority": priority, "tags": tags}


def _repair_article_json(raw_text: str, title: str, snippet: str) -> dict:
    repair_prompt = f"""Die folgende Antwort sollte ein JSON-Objekt sein, ist aber Freitext.

Wandle sie in genau dieses JSON-Schema um:
{{
  "summary": "ausfuehrliche Analyse in 2-3 Absaetzen",
  "category": "eigene_produkte oder markt oder wettbewerber oder sonstige",
  "priority": "hoch oder mittel oder niedrig",
  "tags": ["tag1", "tag2", "tag3"]
}}

Nutze den Freitext als summary. Erfinde keine Fakten hinzu.
Antworte ausschliesslich mit gueltigem JSON.

TITEL: {title}
RSS-INHALT: {snippet}

FREITEXT:
{raw_text}
"""
    text = _call(
        repair_prompt,
        system="Du reparierst Modellantworten zu gueltigem JSON. Antworte nur mit JSON.",
        max_tokens=1200,
        json_mode=True,
        temperature=0,
    )
    return _parse_json(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    return bool(_get_api_key())


def analyse_article(title: str, snippet: str) -> dict:
    if not _get_api_key():
        raise ValueError("Kein API-Schlüssel konfiguriert. Bitte unter Einstellungen hinterlegen.")

    prompt = PROMPT_ARTICLE.format(
        title=title,
        snippet=snippet or "(kein Inhalt verfügbar)",
    )
    system = (
        "Du bist ein erfahrener Marktintelligenz-Analyst einer deutschen Versicherungsgesellschaft. "
        "Du erstellst stets ausfuehrliche, strukturierte Analysen. "
        "Du darfst konkrete Fakten nur aus dem bereitgestellten Titel und Inhalt verwenden. "
        "Wenn der Inhalt keine Grundlage fuer eine konkrete Aussage liefert, kennzeichne das als unklar "
        "und spekuliere nicht. "
        "Kurze Antworten sind nicht akzeptabel – schreibe immer mindestens 200 Woerter im summary-Feld, "
        "aufgeteilt in 2-3 Absaetze. Antworte ausschliesslich mit einem gueltigen JSON-Objekt."
    )
    try:
        text = _call(prompt, system=system, max_tokens=1500, json_mode=True)
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc

    try:
        data = _parse_json(text)
    except ValueError:
        try:
            data = _repair_article_json(text, title, snippet)
        except Exception:
            data = {
                "summary": _strip_markdown_fences(text),
                "category": categorizer.classify(f"{title} {snippet}", "sonstige"),
                "priority": "niedrig",
                "tags": _fallback_tags(f"{title} {snippet} {text}"),
            }
    return _normalize_article_data(data, title, snippet, fallback_summary=text)


def generate_daily_report(articles: list, date: str) -> dict:
    if not _get_api_key():
        raise ValueError("Kein API-Schlüssel konfiguriert. Bitte unter Einstellungen hinterlegen.")
    if not articles:
        raise ValueError("Keine Artikel für diesen Tag gefunden.")

    grouped: dict = {}
    for a in articles:
        grouped.setdefault(a["category"], []).append(a)

    blocks = []
    for cat in ("markt", "wettbewerber", "eigene_produkte", "sonstige"):
        items = grouped.get(cat, [])
        if not items:
            continue
        label = CATEGORY_LABELS.get(cat, cat)
        blocks.append(f"\n## {label} ({len(items)} Artikel)")
        for i, a in enumerate(items[:8], 1):
            snippet = (a["content_snippet"] or "")[:120]
            blocks.append(
                f"{i}. [{a['source_name'] or 'unbekannt'}] {a['title']}\n   {snippet}"
            )

    prompt = PROMPT_REPORT.format(
        date=date,
        total=len(articles),
        articles_text="\n".join(blocks),
    )
    try:
        text = _call(prompt, json_mode=True)
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc

    try:
        data = _parse_json(text)
    except ValueError:
        repair_prompt = f"""Wandle die folgende Tagesbericht-Antwort in gueltiges JSON um.
Nutze genau die Felder zusammenfassung, abschnitte, top_themen und einschaetzung.
Antworte ausschliesslich mit JSON.

ANTWORT:
{text}
"""
        try:
            data = _parse_json(_call(repair_prompt, max_tokens=1200, json_mode=True, temperature=0))
        except Exception:
            raise ValueError(
                "Modell hat kein gültiges JSON für den Tagesbericht zurückgegeben. "
                "Bitte erneut versuchen oder ein anderes Modell wählen."
            )
    data["zusammenfassung"] = str(data.get("zusammenfassung", "")).strip()
    data["einschaetzung"]   = str(data.get("einschaetzung", "")).strip()
    data["top_themen"]      = [str(t).strip() for t in data.get("top_themen", []) if t][:7]
    data["abschnitte"]      = [
        {
            "titel":    str(s.get("titel", "")).strip(),
            "kategorie": str(s.get("kategorie", "sonstige")).strip(),
            "inhalt":   str(s.get("inhalt", "")).strip(),
        }
        for s in data.get("abschnitte", [])
    ]
    return data

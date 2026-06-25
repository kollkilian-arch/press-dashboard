import os
import json
import re
import time
import hashlib
import math
import unicodedata
from collections import Counter, defaultdict
from openai import OpenAI
import categorizer
import database as db

_NO_FULLTEXT = db.NO_FULLTEXT   # sentinel: fulltext unavailable, no AI summary

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

LEGACY_FREE_MODEL_ARTICLE_FETCH = "google/gemma-4-31b-it:free"
DEFAULT_MODEL_ARTICLE_FETCH = "google/gemini-2.5-flash-lite"
DEFAULT_MODEL_ARTICLE_SUMMARY = "google/gemini-2.5-flash-lite"
DEFAULT_MODEL_DAILY_REPORT = "deepseek/deepseek-v4-flash"
DEFAULT_MODEL_ASSISTANT = DEFAULT_MODEL_ARTICLE_SUMMARY
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_ARTICLE_SUMMARY_FALLBACK_MODELS = [
    DEFAULT_MODEL_ARTICLE_FETCH,
    DEFAULT_MODEL_DAILY_REPORT,
]

MODEL_ARTICLE_FETCH = DEFAULT_MODEL_ARTICLE_FETCH
MODEL_ARTICLE_SUMMARY = DEFAULT_MODEL_ARTICLE_SUMMARY
MODEL_DAILY_REPORT = DEFAULT_MODEL_DAILY_REPORT
MODEL_ASSISTANT = DEFAULT_MODEL_ASSISTANT
ARTICLE_SUMMARY_FALLBACK_MODELS = DEFAULT_ARTICLE_SUMMARY_FALLBACK_MODELS

DEFAULT_MODEL = DEFAULT_MODEL_ARTICLE_FETCH

MODEL_SETTINGS = {
    "article_fetch": (
        "openrouter_model_article_fetch",
        "OPENROUTER_MODEL_ARTICLE_FETCH",
        DEFAULT_MODEL_ARTICLE_FETCH,
    ),
    "article_summary": (
        "openrouter_model_article_summary",
        "OPENROUTER_MODEL_ARTICLE_SUMMARY",
        DEFAULT_MODEL_ARTICLE_SUMMARY,
    ),
    "daily_report": (
        "openrouter_model_daily_report",
        "OPENROUTER_MODEL_DAILY_REPORT",
        DEFAULT_MODEL_DAILY_REPORT,
    ),
    "assistant": (
        "openrouter_model_assistant",
        "OPENROUTER_MODEL_ASSISTANT",
        DEFAULT_MODEL_ASSISTANT,
    ),
}

_RATE_LIMIT_COOLDOWNS = {}
_EMBEDDING_LOCAL_FALLBACK_UNTIL = 0

OPENROUTER_DEFAULT_HEADERS = {
    "HTTP-Referer": "http://localhost:5001",
    "X-OpenRouter-Title": "Press Dashboard",
}

MODEL_EXTRA_BODY = {
    LEGACY_FREE_MODEL_ARTICLE_FETCH: {
        "reasoning": {"enabled": True},
    },
    "openrouter/free": {
        "reasoning": {"enabled": True},
    },
    "deepseek/deepseek-v4-flash": {
        "reasoning": {"enabled": True},
    },
}

OPENROUTER_MODEL_CHOICES = [
    ("openrouter/free", "OpenRouter Free Router"),
    ("google/gemma-4-31b-it:free", "Gemma 4 31B (free)"),
    ("meta-llama/llama-3.3-70b-instruct:free", "Llama 3.3 70B Instruct (free)"),
    ("moonshotai/kimi-k2.6:free", "Kimi K2.6 (free)"),
    ("google/gemini-2.5-flash-lite", "Gemini 2.5 Flash Lite"),
    ("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash"),
]

ASSISTANT_STOPWORDS = {
    "aber", "alle", "als", "also", "am", "an", "auch", "auf", "aus", "bei",
    "bin", "bis", "da", "das", "dass", "dem", "den", "der", "des", "die",
    "dies", "diese", "dieser", "dieses", "du", "ein", "eine", "einem",
    "einen", "einer", "eines", "er", "es", "etwas", "für", "geben", "geht",
    "hat", "haben", "ich", "im", "in", "info", "infos", "ist", "ja", "mit",
    "nach", "oder", "sagen", "sagt", "sind", "so", "thema", "und", "uns",
    "von", "vor", "war", "was", "welche", "welcher", "welches", "wenn",
    "wer", "wie", "wir", "wo", "zu", "zum", "zur", "über",
}

ASSISTANT_SYNONYMS = {
    "altersvorsorge": [
        "rente", "rentenversicherung", "lebensversicherung", "leben",
        "vorsorge", "bav", "betriebliche altersversorgung", "riester",
        "ruerup", "rürup", "private rente", "ruhestand",
    ],
    "makler": [
        "vermittler", "vertrieb", "broker", "versicherungsmakler",
        "berater", "beratung", "aussendienst", "außendienst",
    ],
    "provision": [
        "courtagen", "courtage", "vergütung", "verguetung",
        "abschlusskosten", "honorar", "vertriebsvergütung",
    ],
    "regulierung": [
        "bafin", "aufsicht", "gesetz", "gesetzgebung", "gdv",
        "richtlinie", "compliance", "pflicht",
    ],
    "wettbewerber": [
        "allianz", "axa", "generali", "zurich", "ergo", "hdi",
        "talanx", "debeka", "r+v", "signal iduna",
    ],
    "kranken": [
        "pkv", "gkv", "pflege", "gesundheit", "krankenvollversicherung",
        "zusatzversicherung", "beitragserhoehung", "beitragserhöhung",
    ],
}

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

AUFGABE: Schreibe eine praegnante KI-Analyse im Feld "summary".
Die Analyse soll insgesamt etwa 5-6 Saetze umfassen. Fuer bessere Lesbarkeit
darfst du 2-4 kurze Bullet Points verwenden, wenn sich die Inhalte dadurch
klarer strukturieren lassen.

WICHTIGE GROUNDING-REGELN:
- Nutze ausschliesslich Fakten, Zahlen, Namen, Daten und Ereignisse, die im TITEL oder INHALT stehen.
- Erfinde keine Fakten, Zahlen, Zitate, Quellen, Ursachen oder Folgen hinzu.
- Wenn eine Information im bereitgestellten Text fehlt oder unklar ist, schreibe das transparent statt zu spekulieren.
- Verwende Markt- oder Branchenwissen nur zur vorsichtigen Einordnung, aber nicht als Quelle fuer neue konkrete Tatsachen.
- Pruefe vor der Antwort intern, ob jede konkrete Aussage im summary-Feld durch TITEL oder INHALT gedeckt ist.

  Inhalt:
  - Kernaussage: Was ist passiert? Nenne nur belegte Fakten, Zahlen, beteiligte Unternehmen/Behoerden und Zeitpunkte.
  - Einordnung: Warum ist das fuer Markt, Regulierung, Wettbewerb oder Versicherer relevant?
  - Implikation: Was sollte die Versicherungsbranche beobachten oder pruefen, falls aus dem Text ableitbar?
  - Wenn Informationen fehlen, sage knapp, dass sie im bereitgestellten Text nicht erkennbar sind.

Antworte ausschliesslich mit dem folgenden JSON-Objekt – kein Text davor oder danach:
{{
  "summary": "<5-6 Saetze oder kurze Bullet Points mit \\n- ...>",
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

PROMPT_ARTICLE_FETCH = """Du bereinigst aus einer URL geladene Artikeldaten fuer ein Pressedashboard.

URL: {url}
BISHERIGER TITEL: {title}
BISHERIGE QUELLE: {source_name}
BISHERIGES DATUM: {published_at}
BISHERIGE BESCHREIBUNG: {content_snippet}

ROHER ARTIKELTEXT:
{full_text}

AUFGABE:
- Extrahiere nur Informationen, die im bereitgestellten Text oder den Metadaten stehen.
- Ignoriere Navigation, Werbung, Cookie-Texte, Newsletter-Teaser, Related Links und andere Seitenelemente.
- Erfinde keine Fakten, Titel, Daten, Quellen oder Tags.
- Wenn etwas fehlt, gib fuer Textfelder einen leeren String und fuer tags eine leere Liste zurueck.
- content_snippet ist eine sachliche Kurzbeschreibung mit maximal 500 Zeichen.

Antworte ausschliesslich mit diesem JSON-Objekt:
{{
  "title": "bereinigter Artikeltitel",
  "source_name": "Medium oder Herausgeber",
  "content_snippet": "kurze sachliche Beschreibung",
  "published_at": "YYYY-MM-DD HH:MM:SS oder leer",
  "category": "eigene_produkte oder markt oder wettbewerber oder sonstige",
  "tags": ["tag1", "tag2", "tag3"]
}}"""

PROMPT_PIN_ANALYSE = """Du bist erfahrener Marktintelligenz-Analyst einer deutschen Versicherungsgesellschaft.

Analysiere den folgenden Artikel praxisorientiert fuer das interne Pressedashboard:

TITEL: {title}
INHALT: {snippet}

GRUNDPRINZIP:
- Fakten, Zahlen, Ereignisse und genannte Unternehmen muessen aus dem Artikel stammen.
- Einordnung, Kontext und Implikationen darfst du mit solidem Branchenwissen anreichern –
  kennzeichne Einschaetzungen klar als solche (z.B. „duerfte", „typischerweise", „koennte").
- Keine freien Erfindungen von Zahlen, Personen oder Ereignissen, die nicht im Text stehen.

Erstelle folgende Felder:

1. zusammenfassung: Schreibe IMMER als Bullet Points im elliptischen Stil (\\n- ...).
   Elliptisch bedeutet: telegrafisch, ohne Fuellwoerter, jeder Bullet ein eigenstaendiger
   Informationspunkt. Beispiel: „BaFin verhaengt Bussgeld – Verstoss gegen Solvency II"
   statt „Die BaFin hat ein Bussgeld verhaengt, weil das Unternehmen gegen Solvency II verstossen hat."
   3-5 Bullets, Struktur:
   - Was ist passiert / entschieden / veroeffentlicht? (Fakten, Zahlen, Akteure)
   - Markt- oder Regulierungskontext: Einordnung in Wettbewerb, Trend oder Regulierung
   - Was ist bemerkenswert, neu oder strategisch relevant?
   - Ggf. Ausblick oder offene Frage
   Floskeln wie „Der Artikel berichtet…" oder „Es handelt sich um…" sind verboten.

2. geschaeftsfeld: Primaeres Versicherungsgeschaeftsfeld:
   - "Leben": Lebens-, Renten-, Berufsunfaehigkeitsversicherung, Altersvorsorge, Risikoleben
   - "Kranken": Kranken-, PKV-, GKV-, Pflegeversicherung, Gesundheitsthemen
   - "Sonstiges": Sach-, Haftpflicht-, Kfz-, Markt-, Regulierungsthemen, sonstiges

3. implikationen: 2-4 handlungsorientierte Punkte fuer Versicherungsunternehmen.
   Denke als interner Analyst: Was muss beobachtet, geprueft, angepasst oder beachtet werden?
   Nutze Branchenwissen um konkrete Handlungsrelevanz herzuleiten – bleib nah am Thema.
   Format: kurze Bullet Points (\\n- ...) oder knapper Fliesstext.

4. kategorie:
   - "eigene_produkte": eigene Produkte oder das eigene Haus
   - "markt": Markttrends, Regulierung, BaFin, GDV, Branche allgemein
   - "wettbewerber": Konkurrenten (Allianz, AXA, Generali, Zurich, Munich Re, Talanx, HDI, Ergo, R+V, Debeka)
   - "sonstige": alles andere

5. tags: 3-5 kleingeschriebene deutsche Schlagwoerter, keine Sonderzeichen.

{radar_sector_block}

Antworte ausschliesslich mit diesem JSON-Objekt – kein Text davor oder danach:
{{
  "zusammenfassung": "<praezise Analyse>",
  "geschaeftsfeld": "Leben oder Kranken oder Sonstiges",
  "implikationen": "<handlungsorientierte Punkte>",
  "kategorie": "eigene_produkte oder markt oder wettbewerber oder sonstige",
  "tags": ["tag1", "tag2", "tag3"],
  "radar_sector": "<einer der vorgegebenen Trendradar-Sektoren oder leer>"
}}"""

PROMPT_REPORT = """Du bist Marktintelligenz-Analyst einer deutschen Versicherungsgesellschaft.

Erstelle einen {report_type} fuer den Zeitraum {date} auf Basis der folgenden {total} Artikel.

WICHTIGE GROUNDING-REGELN:
- Verwende AUSSCHLIESSLICH Informationen, die in den unten aufgefuehrten Artikeln stehen.
- Erfinde keine Zahlen, Ereignisse, Unternehmensnamen, Zitate oder Zusammenhaenge.
- Wenn die Artikellage zu einem Thema duenn ist, schreibe das transparent statt zu extrapolieren.
- Jede konkrete Aussage im Bericht muss durch mindestens einen der Artikel gedeckt sein.
- Verwende keine Allgemeinplaetze oder Hintergrundwissen als eigenstaendige Fakten.

{articles_text}

Antworte ausschliesslich mit einem JSON-Objekt (kein Markdown, keine Erklaerungen):
{{
  "zusammenfassung": "3-5 Saetze Executive Summary, ausschliesslich aus Artikelinhalten abgeleitet",
  "abschnitte": [
    {{
      "titel": "Abschnittsname",
      "kategorie": "markt oder wettbewerber oder eigene_produkte oder sonstige",
      "inhalt": "2-4 Saetze mit konkreten Fakten ausschliesslich aus den bereitgestellten Artikeln"
    }}
  ],
  "top_themen": ["Thema 1", "Thema 2", "Thema 3", "Thema 4", "Thema 5"],
  "einschaetzung": "1-2 Saetze strategische Einschaetzung, nur auf Basis der bereitgestellten Artikel"
}}

Schreibe auf Deutsch. Nur Abschnitte fuer Kategorien mit vorhandenen Artikeln.
Keine freien Erfindungen – strikt nur aus den bereitgestellten Texten."""

PROMPT_TREND_RADAR = """Du bist Foresight-Analyst fuer ein internes Pressedashboard einer deutschen Versicherungsgesellschaft.

Erstelle einen Trendradar nach diesem Prinzip:
- Die Artikel sind Inputs/Signale.
- Clustere verwandte Signale zu konkreten, tragfaehigen Themen.
- Gruppiere diese Themen in wenige breitere Sektoren.
- Positioniere jedes Thema in genau einem Handlungshorizont:
  - "Act": unmittelbarer Handlungs- oder Pruefbedarf
  - "Prepare": absehbare strategische Vorbereitung sinnvoll
  - "Monitor": fruehes Signal, weiter beobachten

{sectors_block}
CLUSTERING-REGELN (gegen Ueberfrachtung):
- Jedes Topic benoetigt mindestens 3 Artikel als Belege. Topics mit weniger Artikeln werden weggelassen oder mit einem verwandten Topic zusammengefasst.
- Lieber 8 aussagekraeftige Topics als 15 kleinteilige. Fasse thematisch aehnliche Signale mutig zusammen.
- Nutze den vorklassifizierten Trendradar-Sektor der Artikel als starke Orientierung fuer die Topic-Zuordnung, aber pruefe die inhaltliche Passung anhand von Titel, Analyse, Quelle, Datum und Tags.
- Artikel aus verschiedenen Geschaeftsfeldern (z.B. Kranken vs. Leben) duerfen nur dann in einem Topic gebuendelt werden, wenn ein direkter inhaltlicher Zusammenhang im Text nachweisbar ist – nicht allein wegen oberflaechlicher Aehnlichkeit (z.B. beide erwaehnen Gesundheitspruefung oder Kuendigung).
- Pruefe jeden article_id-Eintrag: Passt Titel und Geschaeftsfeld dieses Artikels zum Topic-Namen? Wenn nicht, entferne die ID oder bilde ein eigenes Topic.
- Verwende keine artikelindividuellen Implikationen als Input oder Begruendung. Der Trendradar soll aus den Signalen selbst entstehen; strategische Implikationen werden erst auf Topic-Ebene aus dem erkannten Muster abgeleitet.

WICHTIGE GROUNDING-REGELN:
- Verwende ausschliesslich die unten aufgefuehrten Artikel.
- article_ids muessen exakt aus den bereitgestellten IDs stammen.
- Erfinde keine Artikel, Quellen, Zahlen oder Ereignisse.
- Sektoren sind breitere Themenfelder, Topics sind konkrete Entwicklungen.

FILTERKONTEXT:
{filter_context}

ARTIKEL:
{articles_text}

Antworte ausschliesslich mit gueltigem JSON:
{{
  "title": "KI-Trendradar",
  "sectors": ["Sektor 1", "Sektor 2", "Sektor 3", "Sektor 4"],
  "topics": [
    {{
      "name": "Kurzer Topic-Name",
      "sector": "einer der sectors",
      "horizon": "Act oder Prepare oder Monitor",
      "summary": "2-3 Saetze: Benenne das erkannte Muster ueber mehrere Signale hinweg und leite daraus die strategische Implikation fuer Versicherer ab – nicht aus Einzelartikel-Implikationen.",
      "evidence": "Knapp: welche Signale/Quellen stuetzen das Thema",
      "confidence": 0-100,
      "article_ids": [1, 2, 3]
    }}
  ]
}}

Zielgroesse:
- 3-6 sectors
- 5-12 topics, je nach Material
- Mindestens 3 Artikel pro Topic
- Topic-Namen maximal 60 Zeichen
- Schreibe korrekte Umlaute (ä, ü, ö, ß) in allen Feldern – keine ASCII-Ersetzungen wie ae, ue, oe oder ss.
- Deutsch schreiben."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    key = db.get_setting("openrouter_api_key") or os.environ.get("OPENROUTER_API_KEY", "")
    return key.strip()


def _get_configured_model(feature: str) -> str:
    setting_key, env_key, default = MODEL_SETTINGS[feature]
    value = db.get_setting(setting_key) or os.environ.get(env_key, "") or default
    return value.strip() or default


def _get_article_summary_fallback_models() -> list:
    raw = (
        db.get_setting("openrouter_article_summary_fallback_models")
        or os.environ.get("OPENROUTER_ARTICLE_SUMMARY_FALLBACK_MODELS", "")
    )
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return list(DEFAULT_ARTICLE_SUMMARY_FALLBACK_MODELS)


def get_model_settings() -> dict:
    return {
        "article_fetch": _get_configured_model("article_fetch"),
        "article_summary": _get_configured_model("article_summary"),
        "daily_report": _get_configured_model("daily_report"),
        "assistant": _get_configured_model("assistant"),
    }


def get_feature_models() -> list:
    settings = get_model_settings()
    fallbacks = _get_article_summary_fallback_models()
    return [
        ("Article Fetching & HTML Cleaning", settings["article_fetch"]),
        ("Article Summaries", settings["article_summary"]),
        ("Article Summary Fallbacks", ", ".join(fallbacks) if fallbacks else "Keine"),
        ("Daily Reports & Briefs", settings["daily_report"]),
        ("Pinned Article Assistant", settings["assistant"]),
        ("Semantic Retrieval Embeddings", get_embedding_model()),
    ]


def get_model_choices() -> list:
    choices = list(OPENROUTER_MODEL_CHOICES)
    known = {model for model, _label in choices}
    for model in get_model_settings().values():
        if model and model not in known:
            choices.append((model, f"Aktuell: {model}"))
            known.add(model)
    return choices


FEATURE_MODELS = get_feature_models


def _get_model(model: str = None) -> str:
    return model or DEFAULT_MODEL


def _make_client() -> OpenAI:
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=_get_api_key(),
        timeout=float(os.environ.get("OPENROUTER_TIMEOUT", "120")),
        max_retries=int(os.environ.get("OPENROUTER_MAX_RETRIES", "0")),
        default_headers=_openrouter_headers(),
    )


def _openrouter_headers() -> dict:
    return {
        "HTTP-Referer": (
            db.get_setting("openrouter_http_referer")
            or os.environ.get("OPENROUTER_HTTP_REFERER")
            or OPENROUTER_DEFAULT_HEADERS["HTTP-Referer"]
        ),
        "X-OpenRouter-Title": (
            db.get_setting("openrouter_app_title")
            or os.environ.get("OPENROUTER_APP_TITLE")
            or OPENROUTER_DEFAULT_HEADERS["X-OpenRouter-Title"]
        ),
    }


def _get_embedding_api_key() -> str:
    return (
        db.get_setting("openai_embedding_api_key")
        or os.environ.get("OPENAI_EMBEDDING_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    ).strip()


def _get_embedding_base_url() -> str:
    return (
        db.get_setting("openai_embedding_base_url")
        or os.environ.get("OPENAI_EMBEDDING_BASE_URL", "")
    ).strip()


def get_embedding_model() -> str:
    return (
        db.get_setting("openai_embedding_model")
        or os.environ.get("OPENAI_EMBEDDING_MODEL", "")
        or DEFAULT_EMBEDDING_MODEL
    ).strip()


def _preferred_embedding_model_id() -> str:
    if time.time() < _EMBEDDING_LOCAL_FALLBACK_UNTIL:
        return "local-hash-v1"
    return get_embedding_model() if _get_embedding_api_key() else "local-hash-v1"


def _make_embedding_client() -> OpenAI:
    kwargs = {
        "api_key": _get_embedding_api_key(),
        "timeout": float(os.environ.get("OPENAI_EMBEDDING_TIMEOUT", "45")),
        "max_retries": int(os.environ.get("OPENAI_EMBEDDING_MAX_RETRIES", "1")),
    }
    base_url = _get_embedding_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _extra_body_for_model(model: str) -> dict:
    return MODEL_EXTRA_BODY.get(_cooldown_key(model), {}).copy()


def _cooldown_key(model: str) -> str:
    return (model or DEFAULT_MODEL).strip().lower()


def _extract_retry_delay_seconds(exc: Exception) -> int:
    msg = str(exc)
    patterns = (
        r"retry[-_\s]?after[:=\s]+(\d+)",
        r"try again in\s+(\d+)\s*s",
        r"(\d+)\s*s(?:econds?)?",
    )
    for pattern in patterns:
        match = re.search(pattern, msg, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return int(os.environ.get("OPENROUTER_RATE_LIMIT_COOLDOWN", "120"))


def _rate_limit_cooldown_remaining(model: str) -> int:
    until = _RATE_LIMIT_COOLDOWNS.get(_cooldown_key(model), 0)
    remaining = int(until - time.time())
    if remaining <= 0:
        _RATE_LIMIT_COOLDOWNS.pop(_cooldown_key(model), None)
        return 0
    return remaining


def _remember_rate_limit(model: str, exc: Exception) -> None:
    delay = max(1, _extract_retry_delay_seconds(exc))
    cap = int(os.environ.get("OPENROUTER_RATE_LIMIT_COOLDOWN_MAX", "600"))
    _RATE_LIMIT_COOLDOWNS[_cooldown_key(model)] = time.time() + min(delay, cap)


def _allow_rate_limit_fallbacks() -> bool:
    value = os.environ.get("OPENROUTER_TRY_FALLBACKS_ON_RATE_LIMIT", "").strip().lower()
    return value in {"1", "true", "yes", "ja", "on"}


def _is_unsupported_json_mode_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(term in msg for term in ("response_format", "json_object", "structured output"))


def _is_unsupported_extra_body_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "reasoning" in msg and any(
        term in msg
        for term in ("unsupported", "not supported", "unrecognized", "invalid", "unknown")
    )


def _call(prompt: str, system: str = None, max_tokens: int = None,
          json_mode: bool = False, temperature: float = 0.6,
          model: str = None, _skip_extra_body: bool = False) -> str:
    """Send a prompt, return the raw text response."""
    model_name = _get_model(model)
    cooldown = _rate_limit_cooldown_remaining(model_name)
    if cooldown:
        raise RuntimeError(
            f"Rate-Limit erreicht (429). Bitte {cooldown} Sekunden warten. "
            "OpenRouter hat dieses Modell gerade begrenzt."
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = dict(
        model=model_name,
        messages=messages,
        temperature=temperature,
    )
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    extra_body = {} if _skip_extra_body else _extra_body_for_model(model_name)
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        response = _make_client().chat.completions.create(**kwargs)
    except Exception as exc:
        if _is_rate_limit_error(exc):
            _remember_rate_limit(model_name, exc)
            raise
        if extra_body and _is_unsupported_extra_body_error(exc):
            return _call(
                prompt,
                system=system,
                max_tokens=max_tokens,
                json_mode=json_mode,
                temperature=temperature,
                model=model,
                _skip_extra_body=True,
            )
        if json_mode and _is_unsupported_json_mode_error(exc):
            return _call(
                prompt,
                system=system,
                max_tokens=max_tokens,
                json_mode=False,
                temperature=temperature,
                model=model,
                _skip_extra_body=_skip_extra_body,
            )
        raise
    content = response.choices[0].message.content
    if not content or not content.strip():
        finish = getattr(response.choices[0], "finish_reason", "unknown")
        raise ValueError(
            f"Modell hat eine leere Antwort zurückgegeben (finish_reason={finish}). "
            "Bitte erneut versuchen."
        )
    return content


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()


def _cut_degenerate_tail(text: str) -> str:
    text = str(text or "")
    # Some routed free models can collapse into repeated markup-like tokens.
    text = re.sub(r"(?:</){4,}[\s\S]*$", "", text)
    text = re.sub(r"(?:<\|/?[a-z0-9_-]+\|>){3,}[\s\S]*$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _extract_summary_from_malformed_json(text: str) -> str:
    text = _cut_degenerate_tail(_strip_markdown_fences(text))
    match = re.search(r'"summary"\s*:\s*', text)
    if not match:
        return text

    raw = text[match.end():]
    key_match = re.search(r'"\s*,\s*"(?:category|priority|tags|model_used)"\s*:', raw)
    if key_match:
        raw = raw[:key_match.start()]

    raw = raw.strip()
    if raw.startswith('"'):
        raw = raw[1:]
    raw = re.sub(r'"\s*,\s*"', "\n\n", raw)
    raw = re.sub(r'"\s*}\s*$', "", raw)
    raw = raw.strip()

    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw.replace("\\n\\n", "\n\n").replace("\\n", "\n")


def _clean_generated_summary(text: str) -> str:
    text = _cut_degenerate_tail(_strip_markdown_fences(str(text or "")))
    if not text:
        return ""

    if re.search(r'^\s*\{?\s*"summary"\s*:', text):
        text = _extract_summary_from_malformed_json(text)

    text = _cut_degenerate_tail(text)
    text = text.replace("\\n\\n", "\n\n").replace("\\n", "\n")
    text = re.sub(r'^\s*\{\s*"?summary"?\s*:\s*"?', "", text)
    text = re.sub(r'"\s*,\s*"(?:category|priority|tags|model_used)"[\s\S]*$', "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip(" \t\r\n\"',{}")

    if text and not re.search(r"[.!?…]$", text):
        last_sentence = max(text.rfind("."), text.rfind("!"), text.rfind("?"), text.rfind("…"))
        if last_sentence >= 30:
            text = text[:last_sentence + 1]

    return text.strip()


def _parse_json(text: str) -> dict:
    """
    Parse JSON from the model response robustly:
    1. Strip markdown fences (```json … ```)
    2. Try direct parse
    3. Fall back to extracting the first {...} block from the text
       (some models wrap JSON in a sentence)
    """
    text = _cut_degenerate_tail(_strip_markdown_fences(text))

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
        return (
            f"Rate-Limit erreicht (429).{wait} Bitte später erneut versuchen. "
            "Das passiert besonders häufig bei kostenlosen OpenRouter-Modellen."
        )
    if "401" in msg or "unauthorized" in msg.lower():
        return "Ungültiger API-Schlüssel. Bitte unter Einstellungen prüfen."
    if "402" in msg or "payment" in msg.lower():
        return "Kein Guthaben auf dem OpenRouter-Konto. Bitte Guthaben oder ein kostenlos verfügbares Modell prüfen."
    if "500" in msg or "internal server error" in msg.lower():
        return (
            "OpenRouter oder das gewählte Modell meldet gerade einen internen Fehler. "
            "Bitte erneut versuchen oder in den Einstellungen ein anderes Modell wählen."
        )
    return msg


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate" in msg or "resource_exhausted" in msg


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
    summary = _clean_generated_summary(data.get("summary", ""))
    if not summary:
        summary = _clean_generated_summary(fallback_summary)
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

    model_used = str(data.get("model_used", "")).strip()

    return {
        "summary": summary,
        "category": category,
        "priority": priority,
        "tags": tags,
        "model_used": model_used,
    }


def _normalize_article_object(data: dict) -> dict:
    title = str(data.get("title", "")).strip()
    source_name = str(data.get("source_name", "")).strip()
    content_snippet = str(data.get("content_snippet", "")).strip()[:500]
    published_at = str(data.get("published_at", "")).strip()
    if published_at and not re.match(r"^\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?$", published_at):
        published_at = ""

    category = str(data.get("category", "sonstige")).strip()
    if category not in CATEGORIES:
        category = "sonstige"

    tags = [
        str(t).lower().strip()
        for t in data.get("tags", [])
        if str(t).strip()
    ][:5]

    return {
        "title": title,
        "source_name": source_name,
        "content_snippet": content_snippet,
        "published_at": published_at,
        "category": category,
        "tags": tags,
    }


def _repair_article_json(raw_text: str, title: str, snippet: str,
                         model: str = None) -> dict:
    repair_prompt = f"""Die folgende Antwort sollte ein JSON-Objekt sein, ist aber Freitext.

Wandle sie in genau dieses JSON-Schema um:
{{
  "summary": "praegnante Analyse mit etwa 5-6 Saetzen oder kurzen Bullet Points",
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
        model=model,
    )
    return _parse_json(text)


def _match_preset_sector(raw_value, preset_sectors: list) -> str:
    """Return the configured sector label matching raw_value, or an empty string."""
    value = str(raw_value or "").strip()
    if not value or not preset_sectors:
        return ""
    for sector in preset_sectors:
        if value == sector:
            return sector
    value_key = value.casefold()
    for sector in preset_sectors:
        if value_key == sector.casefold():
            return sector
    return ""


def _normalize_pin_data(data: dict, title: str, snippet: str,
                        preset_sectors: list = None) -> dict:
    """Normalise the JSON returned by PROMPT_PIN_ANALYSE."""
    preset_sectors = preset_sectors or []
    zusammenfassung = _clean_generated_summary(data.get("zusammenfassung", ""))
    if not zusammenfassung:
        zusammenfassung = "Keine verwertbare KI-Zusammenfassung erhalten."

    geschaeftsfeld = str(data.get("geschaeftsfeld", "Sonstiges")).strip()
    if geschaeftsfeld not in ("Leben", "Kranken", "Sonstiges"):
        geschaeftsfeld = "Sonstiges"

    implikationen = _cut_degenerate_tail(str(data.get("implikationen", "")).strip())

    kategorie = str(data.get("kategorie", "sonstige")).strip()
    if kategorie not in CATEGORIES:
        kategorie = categorizer.classify(f"{title} {snippet}", "sonstige")

    tags = [
        str(t).lower().strip()
        for t in data.get("tags", [])
        if str(t).strip()
    ][:5]
    if not tags:
        tags = _fallback_tags(f"{title} {snippet} {zusammenfassung}")

    radar_sector = _match_preset_sector(data.get("radar_sector"), preset_sectors)

    return {
        "zusammenfassung": zusammenfassung,
        "geschaeftsfeld": geschaeftsfeld,
        "implikationen": implikationen,
        "kategorie": kategorie,
        "tags": tags,
        "radar_sector": radar_sector,
    }


def _article_ids_from_value(value) -> list:
    if isinstance(value, list):
        raw_ids = value
    elif isinstance(value, str):
        raw_ids = re.findall(r"\d+", value)
    else:
        raw_ids = []
    ids = []
    for raw_id in raw_ids:
        try:
            article_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if article_id not in ids:
            ids.append(article_id)
    return ids


def _normalize_radar_data(data: dict, valid_article_ids: set) -> dict:
    raw_sectors = data.get("sectors", [])
    sectors = []
    if isinstance(raw_sectors, list):
        for sector in raw_sectors:
            name = str(sector.get("name") if isinstance(sector, dict) else sector).strip()
            if name and name not in sectors:
                sectors.append(name[:48])

    horizon_aliases = {
        "act": "Act",
        "aktion": "Act",
        "handeln": "Act",
        "prepare": "Prepare",
        "vorbereiten": "Prepare",
        "monitor": "Monitor",
        "beobachten": "Monitor",
    }

    topics = []
    raw_topics = data.get("topics", [])
    if not isinstance(raw_topics, list):
        raw_topics = []
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        article_ids = [
            article_id for article_id in _article_ids_from_value(item.get("article_ids"))
            if article_id in valid_article_ids
        ]
        if len(article_ids) < 3:
            continue

        name = str(item.get("name") or "").strip()[:60]
        if not name:
            name = "Trendthema"
        sector = str(item.get("sector") or "").strip()[:48]
        if not sector:
            sector = "Sonstiges"
        if sector not in sectors:
            sectors.append(sector)

        horizon_key = str(item.get("horizon") or "Monitor").strip().lower()
        horizon = horizon_aliases.get(horizon_key, horizon_aliases.get(horizon_key.split()[0], "Monitor"))
        try:
            confidence = int(item.get("confidence", 70))
        except (TypeError, ValueError):
            confidence = 70
        confidence = max(0, min(100, confidence))

        topics.append({
            "name": name,
            "sector": sector,
            "horizon": horizon,
            "summary": _cut_degenerate_tail(str(item.get("summary") or "").strip())[:700],
            "evidence": _cut_degenerate_tail(str(item.get("evidence") or "").strip())[:500],
            "confidence": confidence,
            "article_ids": article_ids,
        })

    if not sectors and topics:
        sectors = sorted({topic["sector"] for topic in topics})

    return {
        "title": str(data.get("title") or "KI-Trendradar").strip()[:80],
        "sectors": sectors[:8],
        "topics": topics[:24],
    }


def _build_radar_article_blocks(articles: list) -> str:
    blocks = []
    for article in articles:
        summary = (article.get("ai_summary") or "").strip()
        if summary == _NO_FULLTEXT:
            summary = ""
        snippet = (article.get("content_snippet") or "").strip()
        text_parts = []
        if summary:
            text_parts.append(f"KI-Analyse: {summary[:900]}")
        if snippet and not summary:
            text_parts.append(f"Snippet: {snippet[:700]}")
        tags = article.get("tags") or ""
        date_value = (article.get("published_at") or article.get("fetched_at") or "")[:10]
        radar_sector = (article.get("radar_sector") or "").strip()
        radar_sector_line = (
            f"Vorklassifizierter Trendradar-Sektor: {radar_sector}"
            if radar_sector else
            "Vorklassifizierter Trendradar-Sektor: nicht gesetzt"
        )
        blocks.append(
            "\n".join([
                f"ID: {article['id']}",
                f"Titel: {article['title']}",
                f"Quelle: {article.get('source_name') or 'unbekannt'}",
                f"Datum: {date_value or 'unbekannt'}",
                f"Kategorie: {article.get('category') or 'sonstige'}",
                f"Geschaeftsfeld: {article.get('geschaeftsfeld') or 'nicht gesetzt'}",
                radar_sector_line,
                f"Tags: {tags or 'keine'}",
                *text_parts,
            ])
        )
    return "\n\n---\n\n".join(blocks)


def _normalize_for_search(text: str) -> str:
    text = str(text or "").casefold()
    text = text.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9+]+", " ", text)


def _search_tokens(text: str) -> list:
    normalized = _normalize_for_search(text)
    tokens = []
    for token in re.findall(r"[a-z0-9+]{3,}", normalized):
        if token in ASSISTANT_STOPWORDS:
            continue
        for suffix in ("ungen", "heiten", "keit", "isch", "liche", "licher", "liches", "ende", "ern", "en", "er", "es", "e", "s"):
            if len(token) > len(suffix) + 4 and token.endswith(suffix):
                token = token[:-len(suffix)]
                break
        if token and token not in ASSISTANT_STOPWORDS:
            tokens.append(token)
    return tokens


def _expanded_query_text(question: str) -> str:
    tokens = set(_search_tokens(question))
    expansions = []
    normalized_question = _normalize_for_search(question)
    for key, synonyms in ASSISTANT_SYNONYMS.items():
        key_norm = _normalize_for_search(key).strip()
        if key_norm in normalized_question or key_norm in tokens:
            expansions.extend(synonyms)
            continue
        synonym_norms = [_normalize_for_search(s).strip() for s in synonyms]
        if any(s and s in normalized_question for s in synonym_norms):
            expansions.append(key)
            expansions.extend(synonyms)
    return " ".join([question, *expansions])


def _query_terms(question: str) -> set:
    return set(_search_tokens(_expanded_query_text(question)))


def _hashing_embedding(text: str, dim: int = 512) -> list:
    terms = _search_tokens(_expanded_query_text(text))
    features = Counter(terms)
    normalized = _normalize_for_search(text)
    compact = normalized.replace(" ", "")
    for i in range(max(0, len(compact) - 2)):
        features[f"tri:{compact[i:i+3]}"] += 0.25
    vector = [0.0] * dim
    for feature, weight in features.items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1 if digest[4] % 2 == 0 else -1
        vector[bucket] += sign * float(weight)
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _embed_texts(texts: list) -> tuple:
    global _EMBEDDING_LOCAL_FALLBACK_UNTIL
    clean_texts = [str(text or "").strip() for text in texts]
    if not clean_texts:
        return [], _preferred_embedding_model_id()

    key = _get_embedding_api_key()
    model = get_embedding_model()
    if key and time.time() >= _EMBEDDING_LOCAL_FALLBACK_UNTIL:
        try:
            response = _make_embedding_client().embeddings.create(
                model=model,
                input=clean_texts,
            )
            by_index = sorted(response.data, key=lambda item: item.index)
            return [item.embedding for item in by_index], model
        except Exception:
            if os.environ.get("ASSISTANT_DISABLE_LOCAL_EMBEDDINGS", "").lower() in {"1", "true", "yes"}:
                raise
            _EMBEDDING_LOCAL_FALLBACK_UNTIL = time.time() + int(
                os.environ.get("ASSISTANT_EMBEDDING_FALLBACK_SECONDS", "300")
            )

    return [_hashing_embedding(text) for text in clean_texts], "local-hash-v1"


def _cosine_similarity(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    norm_a = math.sqrt(sum(float(v) * float(v) for v in a[:n]))
    norm_b = math.sqrt(sum(float(v) * float(v) for v in b[:n]))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _lexical_score(question_terms: set, text: str) -> float:
    if not question_terms:
        return 0.0
    text_terms = set(_search_tokens(text))
    if not text_terms:
        return 0.0
    overlap = len(question_terms & text_terms) / max(1, len(question_terms))
    fuzzy_hits = 0
    for q in question_terms:
        if q in text_terms:
            continue
        if any(q in t or t in q for t in text_terms if len(q) >= 4 and len(t) >= 4):
            fuzzy_hits += 1
    fuzzy = fuzzy_hits / max(1, len(question_terms))
    return min(1.0, overlap + fuzzy * 0.5)


def _article_retrieval_text(article: dict) -> str:
    summary = (article.get("ai_summary") or "").strip()
    if summary == _NO_FULLTEXT:
        summary = ""
    parts = [
        f"Titel: {article.get('title') or ''}",
        f"Quelle: {article.get('source_name') or ''}",
        f"Datum: {(article.get('published_at') or article.get('fetched_at') or '')[:10]}",
        f"Geschaeftsfeld: {article.get('geschaeftsfeld') or ''}",
        f"Kategorie: {article.get('category') or ''}",
        f"Tags: {article.get('tags') or ''}",
        f"Zusammenfassung: {summary}",
        f"Implikationen: {article.get('ai_implications') or ''}",
        f"Snippet: {article.get('content_snippet') or ''}",
        f"Volltext: {article.get('full_text') or ''}",
    ]
    return "\n".join(part for part in parts if part.strip())


def _split_article_chunks(article: dict, max_chars: int = 1700, overlap: int = 220) -> list:
    text = re.sub(r"\n{3,}", "\n\n", _article_retrieval_text(article)).strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            split_at = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if split_at > start + 500:
                end = split_at + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks[:8]


def refresh_pinned_article_chunks() -> dict:
    """Ensure pinned articles have fresh retrieval chunks and embeddings."""
    articles = db.get_pinned_articles_for_assistant()
    existing_rows = db.get_article_chunks_for_pinned()
    existing_by_article = defaultdict(list)
    for row in existing_rows:
        existing_by_article[int(row["article_id"])].append(row)

    desired_model = _preferred_embedding_model_id()
    changed = []
    for article in articles:
        article_id = int(article["id"])
        contents = _split_article_chunks(article)
        hashes = [
            hashlib.sha256(content.encode("utf-8")).hexdigest()
            for content in contents
        ]
        existing = sorted(existing_by_article.get(article_id, []), key=lambda r: r["chunk_index"])
        existing_hashes = [row["content_hash"] for row in existing]
        existing_models = {row.get("embedding_model") or "" for row in existing}
        if existing_hashes == hashes and existing_models == {desired_model}:
            continue
        changed.append((article_id, contents, hashes))

    embedded_chunks = 0
    for article_id, contents, hashes in changed:
        embeddings, used_model = _embed_texts(contents)
        chunks = []
        for content, content_hash, embedding in zip(contents, hashes, embeddings):
            chunks.append({
                "content": content,
                "content_hash": content_hash,
                "embedding_json": json.dumps(embedding),
                "embedding_model": used_model,
            })
        db.replace_article_chunks(article_id, chunks)
        embedded_chunks += len(chunks)

    return {
        "articles": len(articles),
        "refreshed_articles": len(changed),
        "embedded_chunks": embedded_chunks,
        "embedding_model": _preferred_embedding_model_id(),
    }


def _parse_embedding_json(value: str) -> list:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def retrieve_pinned_article_context(question: str, max_articles: int = 8, max_chunks: int = 14) -> list:
    refresh_pinned_article_chunks()
    rows = db.get_article_chunks_for_pinned()
    if not rows:
        return []

    question_text = _expanded_query_text(question)
    q_embeddings, q_model = _embed_texts([question_text])
    q_embedding = q_embeddings[0] if q_embeddings else []
    q_terms = _query_terms(question)

    scored = []
    for row in rows:
        content = row["content"] or ""
        article_text = " ".join([
            row.get("title") or "",
            row.get("source_name") or "",
            row.get("tags") or "",
            row.get("category") or "",
            row.get("geschaeftsfeld") or "",
        ])
        lexical = _lexical_score(q_terms, f"{article_text}\n{content}")
        vector = 0.0
        if row.get("embedding_model") == q_model:
            vector = max(0.0, _cosine_similarity(q_embedding, _parse_embedding_json(row.get("embedding_json"))))
        metadata_boost = min(0.12, _lexical_score(q_terms, article_text) * 0.12)
        if vector:
            score = (vector * 0.72) + (lexical * 0.28) + metadata_boost
        else:
            score = lexical + metadata_boost
        if score > 0.08:
            scored.append((score, vector, lexical, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    grouped = {}
    for score, vector, lexical, row in scored[:max_chunks * 3]:
        article_id = int(row["article_id"])
        item = grouped.setdefault(article_id, {
            "id": article_id,
            "title": row["title"],
            "url": row["url"],
            "source_name": row["source_name"],
            "date": (row["published_at"] or row["fetched_at"] or "")[:10],
            "category": row["category"],
            "geschaeftsfeld": row["geschaeftsfeld"],
            "tags": row["tags"],
            "score": 0.0,
            "chunks": [],
        })
        item["score"] = max(item["score"], score)
        if len(item["chunks"]) < 3:
            item["chunks"].append({
                "text": row["content"][:1800],
                "score": round(score, 4),
                "semantic_score": round(vector, 4),
                "lexical_score": round(lexical, 4),
            })

    contexts = sorted(grouped.values(), key=lambda item: item["score"], reverse=True)
    return contexts[:max_articles]


def _build_assistant_context_blocks(contexts: list) -> str:
    blocks = []
    for article in contexts:
        chunks = "\n\n".join(
            f"Auszug {idx + 1}:\n{chunk['text']}"
            for idx, chunk in enumerate(article["chunks"])
        )
        blocks.append("\n".join([
            f"[A{article['id']}] {article['title']}",
            f"Quelle: {article.get('source_name') or 'unbekannt'}",
            f"Datum: {article.get('date') or 'unbekannt'}",
            f"Geschaeftsfeld: {article.get('geschaeftsfeld') or 'nicht gesetzt'}",
            f"Kategorie: {article.get('category') or 'sonstige'}",
            f"Tags: {article.get('tags') or 'keine'}",
            f"URL: {article.get('url') or ''}",
            chunks,
        ]))
    return "\n\n---\n\n".join(blocks)


def _normalize_assistant_answer(data: dict, contexts: list) -> dict:
    valid_ids = {int(item["id"]) for item in contexts}
    answer = _cut_degenerate_tail(str(data.get("answer") or "").strip())
    article_ids = [
        int(article_id)
        for article_id in _article_ids_from_value(data.get("article_ids"))
        if int(article_id) in valid_ids
    ]
    for cited_id in re.findall(r"\[A(\d+)\]", answer):
        article_id = int(cited_id)
        if article_id in valid_ids and article_id not in article_ids:
            article_ids.append(article_id)
    coverage = str(data.get("coverage") or "teilweise").strip().lower()
    if coverage not in ("gut", "teilweise", "keine"):
        coverage = "teilweise"
    suggestions = [
        str(item).strip()
        for item in data.get("suggested_queries", [])
        if str(item).strip()
    ][:3]
    return {
        "answer": answer,
        "coverage": coverage,
        "article_ids": article_ids,
        "suggested_queries": suggestions,
    }


def answer_pinned_question(question: str) -> dict:
    if not _get_api_key():
        raise ValueError("Kein API-Schluessel konfiguriert. Bitte unter Einstellungen hinterlegen.")

    question = str(question or "").strip()
    if not question:
        raise ValueError("Bitte eine Frage eingeben.")

    contexts = retrieve_pinned_article_context(question)
    if not contexts:
        return {
            "answer": "Dazu finde ich in den gepinnten Artikeln keine belastbaren Informationen.",
            "coverage": "keine",
            "article_ids": [],
            "articles": [],
            "suggested_queries": [],
            "retrieval": {"embedding_model": _preferred_embedding_model_id(), "context_count": 0},
        }

    prompt = f"""Du bist ein interner FAQ- und Recherche-Assistent fuer ein deutsches Pressedashboard.

Beantworte die Frage ausschliesslich auf Basis der unten stehenden gepinnten Artikel.

FRAGE:
{question}

GEPINNTE ARTIKEL / SUCHKONTEXT:
{_build_assistant_context_blocks(contexts)}

REGELN:
- Nutze nur Aussagen, die in den bereitgestellten Artikelauszuegen stehen.
- Zitiere jede konkrete Aussage mit Artikelbelegen im Format [A123].
- Wenn die Artikellage duenn ist, sage das klar.
- Erfinde keine Zitate, Zahlen, Namen, Ursachen oder Schlussfolgerungen.
- Wenn die Frage nach Meinungen fragt (z.B. Makler), nenne nur, was in den Artikeln dazu steht.
- Schreibe auf Deutsch, knapp und gut lesbar.

Antworte ausschliesslich mit gueltigem JSON:
{{
  "answer": "Antwort mit Quellenbelegen wie [A123]",
  "coverage": "gut oder teilweise oder keine",
  "article_ids": [123],
  "suggested_queries": ["optionale Folgefrage 1", "optionale Folgefrage 2"]
}}"""
    system = (
        "Du beantwortest Fragen zu einem kuratierten Pressedashboard. "
        "Du bist strikt quellengebunden und antwortest ausschliesslich mit JSON."
    )
    try:
        text = _call(
            prompt,
            system=system,
            max_tokens=1400,
            json_mode=True,
            temperature=0.15,
            model=_get_configured_model("assistant"),
        )
        data = _parse_json(text)
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc

    result = _normalize_assistant_answer(data, contexts)
    if not result["answer"]:
        result["answer"] = "Dazu finde ich in den gepinnten Artikeln keine belastbaren Informationen."
        result["coverage"] = "keine"
    referenced = set(result["article_ids"])
    if referenced:
        articles = [item for item in contexts if int(item["id"]) in referenced]
    else:
        articles = contexts[:4]
    result["articles"] = [
        {
            "id": item["id"],
            "title": item["title"],
            "url": item["url"],
            "source_name": item["source_name"],
            "date": item["date"],
            "score": round(float(item["score"]), 4),
        }
        for item in articles
    ]
    result["retrieval"] = {
        "embedding_model": _preferred_embedding_model_id(),
        "context_count": len(contexts),
    }
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    return bool(_get_api_key())


def analyse_article_for_pin(title: str, snippet: str, model: str = None) -> dict:
    """Run the full AI analysis triggered when a user pins an article.

    Returns a dict with keys:
        zusammenfassung, geschaeftsfeld, implikationen, kategorie, tags,
        radar_sector, model_used
    """
    if not _get_api_key():
        raise ValueError("Kein API-Schluessel konfiguriert. Bitte unter Einstellungen hinterlegen.")

    preset_sectors = get_radar_preset_sectors()
    if preset_sectors:
        sector_list = "\n".join(f"- {sector}" for sector in preset_sectors)
        radar_sector_block = (
            "6. radar_sector: Ordne den Artikel genau einem der vorgegebenen Trendradar-Sektoren zu.\n"
            "   Verwende exakt eine der folgenden Schreibweisen, keine neue Kategorie:\n"
            f"{sector_list}"
        )
    else:
        radar_sector_block = (
            "6. radar_sector: Kein Trendradar-Sektor vorgegeben. Gib einen leeren String zurueck."
        )

    prompt = PROMPT_PIN_ANALYSE.format(
        title=title,
        snippet=snippet or "(kein Inhalt verfuegbar)",
        radar_sector_block=radar_sector_block,
    )
    system = (
        "Du bist Marktintelligenz-Analyst einer deutschen Versicherungsgesellschaft. "
        "Antworte ausschliesslich mit gueltigem JSON. "
        "Verwende nur Fakten aus dem bereitgestellten Titel und Inhalt."
    )
    primary_model = model or _get_configured_model("article_summary")
    models_to_try = [primary_model, *_get_article_summary_fallback_models()]
    models_to_try = list(dict.fromkeys(models_to_try))

    last_exc = None
    used_model = primary_model
    text = None
    for candidate in models_to_try:
        try:
            text = _call(
                prompt,
                system=system,
                max_tokens=1100,
                json_mode=True,
                temperature=0.2,
                model=candidate,
            )
            used_model = candidate
            break
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit_error(exc):
                if _allow_rate_limit_fallbacks():
                    continue
                break
            raise RuntimeError(_friendly_error(exc)) from exc
    else:
        raise RuntimeError(_friendly_error(last_exc)) from last_exc
    if text is None and last_exc:
        raise RuntimeError(_friendly_error(last_exc)) from last_exc

    try:
        data = _parse_json(text)
    except ValueError:
        # Repair attempt
        try:
            data = _repair_article_json(text, title, snippet, model=used_model)
        except Exception:
            data = {
                "zusammenfassung": _strip_markdown_fences(text),
                "geschaeftsfeld": "Sonstiges",
                "implikationen": "",
                "kategorie": categorizer.classify(f"{title} {snippet}", "sonstige"),
                "tags": _fallback_tags(f"{title} {snippet} {text}"),
            }

    result = _normalize_pin_data(data, title, snippet, preset_sectors=preset_sectors)
    result["model_used"] = used_model
    return result


def extract_article_object(url: str, fetched: dict) -> dict:
    if not _get_api_key():
        raise ValueError("Kein API-Schlüssel konfiguriert. Bitte unter Einstellungen hinterlegen.")

    prompt = PROMPT_ARTICLE_FETCH.format(
        url=url,
        title=fetched.get("title") or "",
        source_name=fetched.get("source_name") or "",
        published_at=fetched.get("published_at") or "",
        content_snippet=fetched.get("content_snippet") or "",
        full_text=fetched.get("full_text") or "(kein Volltext verfügbar)",
    )
    system = (
        "Du extrahierst Artikeldaten aus bereits geladenem HTML-Text. "
        "Nutze nur bereitgestellte Fakten und antworte ausschliesslich mit gueltigem JSON."
    )
    try:
        text = _call(
            prompt,
            system=system,
            max_tokens=900,
            json_mode=True,
            temperature=0,
            model=_get_configured_model("article_fetch"),
        )
        return _normalize_article_object(_parse_json(text))
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc


def analyse_article(title: str, snippet: str,
                    model: str = None) -> dict:
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
        "Schreibe im summary-Feld knapp und gut lesbar: insgesamt etwa 5-6 Saetze, optional mit kurzen "
        "Bullet Points. Antworte ausschliesslich mit einem gueltigen JSON-Objekt."
    )
    primary_model = model or _get_configured_model("article_summary")
    models_to_try = [primary_model] if model else [
        primary_model,
        *_get_article_summary_fallback_models(),
    ]
    models_to_try = list(dict.fromkeys(models_to_try))
    last_exc = None
    used_model = models_to_try[0]
    text = None
    for candidate in models_to_try:
        try:
            text = _call(
                prompt,
                system=system,
                max_tokens=900,
                json_mode=True,
                temperature=0.2,
                model=candidate,
            )
            used_model = candidate
            break
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit_error(exc):
                if _allow_rate_limit_fallbacks():
                    continue
                break
            raise RuntimeError(_friendly_error(exc)) from exc
    else:
        raise RuntimeError(_friendly_error(last_exc)) from last_exc
    if text is None and last_exc:
        raise RuntimeError(_friendly_error(last_exc)) from last_exc

    try:
        data = _parse_json(text)
    except ValueError:
        try:
            data = _repair_article_json(text, title, snippet, model=used_model)
        except Exception:
            data = {
                "summary": _strip_markdown_fences(text),
                "category": categorizer.classify(f"{title} {snippet}", "sonstige"),
                "priority": "niedrig",
                "tags": _fallback_tags(f"{title} {snippet} {text}"),
            }
    data["model_used"] = used_model
    return _normalize_article_data(data, title, snippet, fallback_summary=text)


def get_radar_preset_sectors() -> list:
    raw = db.get_setting("radar_preset_sectors", "")
    return [s.strip() for s in raw.splitlines() if s.strip()]


def generate_trend_radar(articles: list, filters: dict = None, model: str = None) -> dict:
    if not _get_api_key():
        raise ValueError("Kein API-Schlüssel konfiguriert. Bitte unter Einstellungen hinterlegen.")
    if not articles:
        raise ValueError("Keine gepinnten Artikel für diesen Radar gefunden.")

    valid_article_ids = {int(article["id"]) for article in articles}
    filters = filters or {}
    filter_parts = []
    if filters.get("category"):
        filter_parts.append(f"Kategorie={filters['category']}")
    if filters.get("geschaeftsfeld"):
        filter_parts.append(f"Geschaeftsfeld={filters['geschaeftsfeld']}")
    if filters.get("days"):
        filter_parts.append(f"Zeitraum=letzte {filters['days']} Tage")
    filter_context = ", ".join(filter_parts) if filter_parts else "Alle gepinnten Artikel"

    preset_sectors = get_radar_preset_sectors()
    if preset_sectors:
        sector_list = "\n".join(f"- {s}" for s in preset_sectors)
        sectors_block = (
            f"VORGEGEBENE SEKTOREN (verbindlich):\n"
            f"Verwende ausschließlich diese {len(preset_sectors)} Sektoren – erfinde keine neuen "
            f"und lasse keinen weg. Weise jeden Topic genau einem davon zu:\n"
            f"{sector_list}\n"
        )
    else:
        sectors_block = ""

    prompt = PROMPT_TREND_RADAR.format(
        filter_context=filter_context,
        articles_text=_build_radar_article_blocks(articles),
        sectors_block=sectors_block,
    )
    system = (
        "Du erstellst einen belastbaren Foresight-Trendradar. "
        "Antworte ausschliesslich mit gueltigem JSON und verwende nur bereitgestellte article_ids."
    )
    primary_model = model or _get_configured_model("daily_report")
    models_to_try = [primary_model, *_get_article_summary_fallback_models()]
    models_to_try = list(dict.fromkeys(models_to_try))

    last_exc = None
    used_model = primary_model
    text = None
    for candidate in models_to_try:
        try:
            text = _call(
                prompt,
                system=system,
                max_tokens=10000,
                json_mode=True,
                temperature=0.25,
                model=candidate,
            )
            used_model = candidate
            break
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit_error(exc):
                if _allow_rate_limit_fallbacks():
                    continue
                break
            raise RuntimeError(_friendly_error(exc)) from exc
    else:
        raise RuntimeError(_friendly_error(last_exc)) from last_exc
    if text is None and last_exc:
        raise RuntimeError(_friendly_error(last_exc)) from last_exc

    try:
        data = _parse_json(text)
    except ValueError as exc:
        raise ValueError(
            "Modell hat kein gültiges JSON für den Trendradar zurückgegeben. "
            "Bitte erneut versuchen oder ein anderes Modell wählen."
        ) from exc

    result = _normalize_radar_data(data, valid_article_ids)
    if not result["topics"]:
        raise ValueError("KI konnte keine belastbaren Themen mit Artikelbelegen bilden.")
    result["model_used"] = used_model
    return result


def generate_daily_report(articles: list, date: str, mode: str = "daily") -> dict:
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
            # Prefer stored AI analysis; fall back to RSS snippet only if absent
            summary = (a["ai_summary"] or "").strip()
            if summary == _NO_FULLTEXT:
                summary = ""          # fulltext was unavailable – treat as empty
            implications = (a["ai_implications"] or "").strip()
            fallback = (a["content_snippet"] or "")[:200]

            lines = [f"{i}. [{a['source_name'] or 'unbekannt'}] {a['title']}"]
            if summary:
                lines.append(f"   Zusammenfassung: {summary}")
            if implications:
                lines.append(f"   Implikationen: {implications}")
            if not summary and not implications:
                # No AI analysis available – give the model at least the snippet
                lines.append(f"   {fallback}")
            blocks.append("\n".join(lines))

    report_type = "Wochenbericht" if mode == "weekly" else "Tagesbericht"
    prompt = PROMPT_REPORT.format(
        report_type=report_type,
        date=date,
        total=len(articles),
        articles_text="\n".join(blocks),
    )
    try:
        text = _call(prompt, json_mode=True, model=_get_configured_model("daily_report"))
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
            data = _parse_json(_call(
                repair_prompt,
                max_tokens=1200,
                json_mode=True,
                temperature=0,
                model=_get_configured_model("daily_report"),
            ))
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

#!/usr/bin/env python3
"""
AI Daily Brief — dagelijkse pijplijn.

  1. Nieuws ophalen   (RSS-feeds, laatste 24 uur)
  2. Script schrijven (Claude API)
  3. Inspreken        (ElevenLabs, stem Lola, eleven_v4)
  4. Publiceren       (map site/ → GitHub Pages: mp3 + feed.json + speler)

Omgevingsvariabelen:
  ANTHROPIC_API_KEY, ELEVENLABS_API_KEY   verplicht (GitHub secrets)
  SITE_URL     publieke URL van de site, bv. https://hans.github.io/ai-daily-brief
  FORCE        "true" = altijd genereren, tijdcheck overslaan (handmatige run)
  CLAUDE_MODEL optioneel, standaard claude-sonnet-5-5
  VOICE_ID     optioneel, standaard Lola
"""
import calendar
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

# ── Instellingen ────────────────────────────────────────────────────────────
TZ = ZoneInfo("Europe/Amsterdam")
VOICE_ID = os.environ.get("VOICE_ID", "MQCqYs9QBU817XwwHCTz")  # Lola
TTS_MODEL = "eleven_v4"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
FORCE = os.environ.get("FORCE", "").lower() == "true"
RUN_HOUR = 6          # vanaf dit lokale uur mag er gegenereerd worden
KEEP_EPISODES = 7     # zoveel afleveringen blijven online
MIN_CHARS, MAX_CHARS = 2500, 9500   # eleven_v4 accepteert max. 10.000 tekens

ROOT = Path(__file__).parent
OUT = ROOT / "site"
WEB = ROOT / "web"

# Betrouwbare bronnen. Een feed die faalt wordt overgeslagen, niet fataal.
FEEDS = [
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://arstechnica.com/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
]

UA = {"User-Agent": "AI-Daily-Brief/1.0 (+github actions)"}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def set_output(name: str, value: str) -> None:
    """Geeft een waarde door aan de volgende stap in GitHub Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


# ── 0. Bestaande site ophalen ───────────────────────────────────────────────
def load_feed() -> list[dict]:
    """Leest de huidige feed.json van de live site (leeg bij eerste run)."""
    if not SITE_URL:
        return []
    try:
        r = requests.get(f"{SITE_URL}/feed.json", params={"t": int(time.time())},
                         headers=UA, timeout=20)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json().get("episodes", [])
    except Exception as e:
        log(f"bestaande feed niet gelezen ({e}); begin met lege feed")
        return []


# ── 1. Nieuws ophalen ───────────────────────────────────────────────────────
def fetch_news(hours: int = 24, limit: int = 40) -> list[dict]:
    cutoff = time.time() - hours * 3600
    items, seen = [], set()
    for url in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:
            log(f"feed overgeslagen: {url} ({e})")
            continue
        source = feed.feed.get("title", url)
        n = 0
        for entry in feed.entries:
            t = entry.get("published_parsed") or entry.get("updated_parsed")
            if not t:
                continue
            ts = calendar.timegm(t)  # feedparser levert UTC
            title = (entry.get("title") or "").strip()
            if ts < cutoff or not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:500]
            items.append({"source": source, "title": title, "summary": summary,
                          "link": entry.get("link", ""), "ts": ts})
            n += 1
        log(f"{n:>2} berichten uit {source}")
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


# ── 2. Script schrijven ─────────────────────────────────────────────────────
SYSTEM = """You write "AI Daily Brief", a five-minute morning business news bulletin about artificial intelligence, read aloud by a professional news anchor.

Rules:
- English. Plain spoken text only: no markdown, headings, bullet points, URLs or emoji.
- 650 to 750 words (about five minutes spoken).
- Open with exactly: "Good morning. It's {date_spoken}, and this is your AI Daily Brief."
- Then one sentence previewing the top stories.
- Cover four to six stories, most business-relevant first: money, markets, enterprise adoption, regulation, security, major product launches. Skip gossip and minor updates.
- For each story: what happened, then one or two sentences on why it matters for businesses.
- Use ONLY facts from the supplied news items. Never invent numbers, quotes, dates or names. If something is uncertain, attribute it ("according to TechCrunch").
- Write numbers and dates the way they are spoken ("twenty-fifth", "a hundred million dollars").
- You may use these audio tags sparingly: [short pause] between stories, [serious] before security or legal news.
- Close with exactly: "That's your brief for today. Back tomorrow at eight."

Return the bulletin as JSON matching the given schema."""

# Gestructureerde uitvoer (output_config.format): de API garandeert dat het
# antwoord geldige JSON volgens dit schema is, ook als het script aanhalings-
# tekens bevat. (Afgedwongen tool-aanroepen worden door dit model niet
# ondersteund.)
SCHEMA = {
    "type": "object",
    "properties": {
        "script": {"type": "string", "description": "The full spoken text."},
        "stories": {
            "type": "array",
            "description": "The stories covered, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short headline."},
                    "link": {"type": "string", "description": "URL of the source item."},
                },
                "required": ["title", "link"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["script", "stories"],
    "additionalProperties": False,
}


def write_script(items: list[dict], today: dt.date, attempts: int = 2) -> dict:
    date_spoken = f"{today:%A}, {today:%B} {today.day}"
    news = "\n\n".join(
        f"[{i}] {it['source']}: {it['title']}\n{it['summary']}\nLink: {it['link']}"
        for i, it in enumerate(items, 1)
    )
    for attempt in range(1, attempts + 1):
        try:
            return _write_script_once(news, date_spoken)
        except Exception as e:
            # Een fout in het verzoek zelf (4xx, behalve 429) wordt niet beter
            # van opnieuw proberen.
            m = re.match(r"Claude API (\d+)", str(e))
            permanent = m and m.group(1).startswith("4") and m.group(1) != "429"
            if attempt == attempts or permanent:
                raise
            log(f"script mislukt ({e}); poging {attempt + 1}/{attempts}")
            time.sleep(10)


def _write_script_once(news: str, date_spoken: str) -> dict:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            # Ruim, want het model denkt eerst na en dat telt mee. Te krap
            # betekent een afgekapt antwoord.
            "max_tokens": 16000,
            "system": SYSTEM.replace("{date_spoken}", date_spoken),
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user",
                          "content": f"Today's news items (last 24 hours):\n\n{news}"}],
        },
        timeout=300,
    )
    if not r.ok:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    body = r.json()
    stop = body.get("stop_reason")
    if stop == "max_tokens":
        raise RuntimeError("Claude stopte halverwege (max_tokens bereikt)")
    if stop == "refusal":
        raise RuntimeError("Claude weigerde het script te schrijven")
    text = "".join(b.get("text", "") for b in body["content"] if b["type"] == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"antwoord van Claude is geen geldige JSON ({e})")
    if not isinstance(data.get("script"), str):
        raise RuntimeError("Claude leverde geen bruikbaar script")

    script = data["script"].strip()
    if not MIN_CHARS <= len(script) <= MAX_CHARS:
        raise ValueError(f"script heeft {len(script)} tekens, verwacht {MIN_CHARS}–{MAX_CHARS}")
    log(f"script: {len(script)} tekens, {len(script.split())} woorden")
    return {"script": script, "stories": data.get("stories", [])}


# ── 3. Inspreken ────────────────────────────────────────────────────────────
def speak(script: str, path: Path, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"].strip()},
            json={"text": script, "model_id": TTS_MODEL},
            timeout=300,
        )
        if r.ok:
            path.write_bytes(r.content)
            log(f"audio: {len(r.content) / 1e6:.1f} MB, "
                f"kosten {r.headers.get('character-cost', '?')} credits")
            return
        # 429 (rate limit) en 5xx zijn tijdelijk → opnieuw; 4xx anders is fataal
        if r.status_code == 429 or r.status_code >= 500:
            log(f"ElevenLabs {r.status_code}, poging {attempt}/{attempts}; wacht 60 s")
            time.sleep(60)
            continue
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:300]}")
    raise RuntimeError("ElevenLabs bleef falen na meerdere pogingen")


# ── 4. Publiceren ───────────────────────────────────────────────────────────
def build_site(episodes: list[dict], new: dict, new_audio: Path) -> None:
    """Bouwt site/: speler + feed.json + de laatste KEEP_EPISODES mp3's."""
    episodes = [e for e in episodes if e["date"] != new["date"]]
    episodes = [new] + episodes
    keep, drop = episodes[:KEEP_EPISODES], episodes[KEEP_EPISODES:]

    shutil.copytree(WEB, OUT, dirs_exist_ok=True)
    (OUT / "episodes").mkdir(parents=True, exist_ok=True)
    shutil.move(new_audio, OUT / new["audio"])

    # Oudere afleveringen die blijven: opnieuw van de live site halen,
    # zodat de repo zelf nooit mp3's hoeft te bevatten.
    for ep in keep[1:]:
        dest = OUT / ep["audio"]
        try:
            r = requests.get(f"{SITE_URL}/{ep['audio']}", headers=UA, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
        except Exception as e:
            log(f"oude aflevering {ep['date']} niet meegenomen ({e})")
            keep.remove(ep)
    if drop:
        log(f"{len(drop)} oude aflevering(en) offline gehaald")

    feed = {"title": "AI Daily Brief",
            "updated": dt.datetime.now(TZ).isoformat(timespec="seconds"),
            "episodes": keep}
    (OUT / "feed.json").write_text(json.dumps(feed, indent=2, ensure_ascii=False))
    log(f"site gebouwd: {len(keep)} aflevering(en) online")


# ── Hoofdprogramma ──────────────────────────────────────────────────────────
def main() -> int:
    now = dt.datetime.now(TZ)
    today = now.date()
    set_output("published", "false")

    # GitHub-cron rekent in UTC en start meerdere keren per ochtend. Runs vóór
    # 6:00 Nederlandse tijd slaan over; de eerste run daarna maakt de aflevering,
    # latere runs zien dat die er al is. Zo klopt het zomer én winter, en vangt
    # een volgende run het op als GitHub er een vertraagt of overslaat.
    if not FORCE and now.hour < RUN_HOUR:
        log(f"het is {now:%H:%M} in Amsterdam; deze run slaat over")
        return 0

    episodes = load_feed()
    if not FORCE and any(e["date"] == today.isoformat() for e in episodes):
        log("aflevering van vandaag staat al online; klaar")
        return 0

    items = fetch_news()
    log(f"{len(items)} nieuwsberichten verzameld")
    if len(items) < 3:
        raise RuntimeError("te weinig nieuws gevonden; controleer de feeds")

    result = write_script(items, today)

    tmp = ROOT / "episode.mp3"
    speak(result["script"], tmp)

    new = {
        "date": today.isoformat(),
        "title": f"AI Daily Brief — {today:%A %-d %B %Y}",
        "audio": f"episodes/{today.isoformat()}.mp3",
        "stories": result["stories"],
        "script": result["script"],
    }
    build_site(episodes, new, tmp)
    set_output("published", "true")
    log("klaar ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"MISLUKT: {e}")
        sys.exit(1)

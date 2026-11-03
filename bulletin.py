#!/usr/bin/env python3
"""
AI Daily Brief — dagelijkse pijplijn.

  1. Nieuws ophalen   (RSS-feeds, laatste 24 uur; zo nodig 48 of 72 uur)
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
    "https://arstechnica.com/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "https://www.theguardian.com/technology/artificialintelligenceai/rss",
    "https://www.theregister.com/software/ai_ml/headlines.atom",
]
# VentureBeat is verwijderd: die weigerde GitHub elke ochtend (429).

# Hoeveel berichten het script minimaal wil hebben, en hoe ver het daarvoor
# terug mag kijken. Na een stil weekend is 24 uur vaak te weinig.
MIN_ITEMS = 8
WINDOWS_HOURS = (24, 48, 72)

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
def load_feed() -> dict:
    """Leest de huidige feed.json van de live site ({} bij de eerste run).
    Kan hij niet gelezen worden, dan stopt de run liever dan dat hij een site
    zonder oude afleveringen publiceert; een latere run probeert het opnieuw."""
    if not SITE_URL:
        return {}
    try:
        data = download(f"{SITE_URL}/feed.json?t={int(time.time())}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return {}
        raise RuntimeError(f"bestaande feed.json niet leesbaar ({e})")
    except requests.RequestException as e:
        raise RuntimeError(f"bestaande feed.json niet leesbaar ({e})")
    return json.loads(data)


# ── 1. Nieuws ophalen ───────────────────────────────────────────────────────
def fetch_news(exclude_links: set[str] = frozenset(), limit: int = 40) -> list[dict]:
    """Haalt AI-nieuws op. Begint met de laatste 24 uur en kijkt verder terug
    (48, dan 72 uur) zolang er minder dan MIN_ITEMS berichten zijn. Berichten
    die al in een recente aflevering zaten, worden overgeslagen."""
    now = time.time()
    oldest = now - max(WINDOWS_HOURS) * 3600
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
            link = entry.get("link", "")
            if ts < oldest or not title or title.lower() in seen or link in exclude_links:
                continue
            seen.add(title.lower())
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:500]
            items.append({"source": source, "title": title, "summary": summary,
                          "link": link, "ts": ts})
            n += 1
        log(f"{n:>2} berichten uit {source} (laatste {max(WINDOWS_HOURS)} uur)")
    items.sort(key=lambda x: x["ts"], reverse=True)

    for hours in WINDOWS_HOURS:
        chosen = [it for it in items if it["ts"] >= now - hours * 3600]
        if len(chosen) >= MIN_ITEMS:
            break
    log(f"{len(chosen)} nieuwe berichten uit de laatste {hours} uur gebruikt")
    return chosen[:limit]


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
                          "content": f"Recent news items (newest first; prefer the most recent):\n\n{news}"}],
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
def download(url: str, attempts: int = 4) -> bytes:
    """Downloadt een bestand van de live site. Tijdelijke fouten (503, time-out,
    afgebroken verbinding) komen bij GitHub Pages af en toe voor; daarom een
    paar pogingen met een oplopende pauze. Een 404 is definitief."""
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, headers=UA, timeout=120)
            r.raise_for_status()
            return r.content
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise
            err = e
        except requests.RequestException as e:
            err = e
        if attempt < attempts:
            wait = 5 * 2 ** (attempt - 1)   # 5, 10, 20 seconden
            log(f"download mislukt ({err}); poging {attempt + 1}/{attempts} over {wait} s")
            time.sleep(wait)
    raise err
LOST_NOTICE_DAYS = 7   # zo lang blijft een melding over een verloren aflevering staan


def short_reason(e: Exception) -> str:
    """Korte, leesbare reden voor de melding in de speler."""
    code = getattr(getattr(e, "response", None), "status_code", None)
    if code == 503:
        return "temporary server error (503)"
    if code:
        return f"server error ({code})"
    if isinstance(e, requests.Timeout):
        return "download timed out"
    return "download failed"


def build_site(episodes: list[dict], new: dict, new_audio: Path,
               lost_before: list[dict]) -> None:
    """Bouwt site/: speler + feed.json + de laatste KEEP_EPISODES mp3's.
    Afleveringen die niet opnieuw gedownload konden worden, komen in de lijst
    "lost", zodat de speler er een melding over kan tonen."""
    episodes = [e for e in episodes if e["date"] != new["date"]]
    episodes = [new] + episodes
    keep, drop = episodes[:KEEP_EPISODES], episodes[KEEP_EPISODES:]
    today = dt.datetime.now(TZ).date()

    shutil.copytree(WEB, OUT, dirs_exist_ok=True)
    (OUT / "episodes").mkdir(parents=True, exist_ok=True)
    shutil.move(new_audio, OUT / new["audio"])

    # Oudere afleveringen die blijven: opnieuw van de live site halen,
    # zodat de repo zelf nooit mp3's hoeft te bevatten.
    lost_now = []
    for ep in keep[1:]:
        dest = OUT / ep["audio"]
        try:
            dest.write_bytes(download(f"{SITE_URL}/{ep['audio']}"))
        except Exception as e:
            log(f"oude aflevering {ep['date']} niet meegenomen ({e})")
            keep.remove(ep)
            lost_now.append({"date": ep["date"], "lost_on": today.isoformat(),
                             "reason": short_reason(e)})
    if drop:
        log(f"{len(drop)} oude aflevering(en) offline gehaald")

    # Eerdere meldingen bewaren zolang ze jonger zijn dan LOST_NOTICE_DAYS.
    cutoff = (today - dt.timedelta(days=LOST_NOTICE_DAYS)).isoformat()
    lost = lost_now + [l for l in lost_before
                       if l.get("lost_on", "") >= cutoff
                       and l["date"] not in {x["date"] for x in lost_now}]
    if lost_now:
        log(f"melding in de speler voor: {', '.join(l['date'] for l in lost_now)}")

    feed = {"title": "AI Daily Brief",
            "updated": dt.datetime.now(TZ).isoformat(timespec="seconds"),
            "episodes": keep,
            "lost": lost}
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

    current = load_feed()
    episodes = current.get("episodes", [])
    if not FORCE and any(e["date"] == today.isoformat() for e in episodes):
        log("aflevering van vandaag staat al online; klaar")
        return 0

    # Verhalen uit de laatste twee afleveringen niet opnieuw brengen.
    recent = {st.get("link") for ep in episodes[:2] for st in ep.get("stories", [])}
    items = fetch_news(exclude_links={l for l in recent if l})
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
    build_site(episodes, new, tmp, current.get("lost", []))
    set_output("published", "true")
    log("klaar ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"MISLUKT: {e}")
        sys.exit(1)

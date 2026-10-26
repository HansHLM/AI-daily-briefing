# AI Daily Brief

Een dagelijks AI-nieuwsbulletin van vijf minuten, voorgelezen door Lola (ElevenLabs `eleven_v4`), elke ochtend vóór 8:00 klaar in een app op je iPhone-beginscherm.

## Hoe het werkt

```
GitHub Actions (7:30)  →  bulletin.py  →  GitHub Pages  →  iPhone
                            │
                            ├─ 1. nieuws ophalen   RSS-feeds, laatste 24 uur
                            ├─ 2. script schrijven Claude API
                            ├─ 3. inspreken        ElevenLabs API, stem Lola
                            └─ 4. site bouwen      speler + feed.json + mp3's
```

| Bestand | Wat het doet |
|---|---|
| `bulletin.py` | De pijplijn: alle vier stappen |
| `.github/workflows/daily.yml` | De wekker: start het script elke ochtend en publiceert het resultaat |
| `web/` | De speler voor je iPhone (één HTML-bestand, plus iconen) |
| `requirements.txt` | De twee Python-pakketten die het script nodig heeft |

**Geen mp3's in de repo.** Elke ochtend haalt het script de afleveringen van de afgelopen zes dagen op van de live site, voegt de nieuwe toe en publiceert alles opnieuw. De repo blijft daardoor klein, en er staan altijd zeven afleveringen online.

**Zomer- en wintertijd.** GitHub rekent in UTC. De workflow start daarom om 5:30 én 6:30 UTC, en het script laat alleen de run door die om 7:xx Nederlandse tijd valt. Zo is de aflevering het hele jaar rond 7:35 klaar.

## Eenmalig instellen (± 15 minuten)

### 1. API-sleutels aanmaken

- **ElevenLabs:** elevenlabs.io → *Developers* → *API Keys* → *Create*. Beperk de sleutel tot alleen **Text to Speech** en stel een **creditlimiet** in, bijvoorbeeld 150.000 per maand. Een fout in het script kan dan nooit je hele tegoed opmaken.
- **Claude:** console.anthropic.com → *API Keys* → *Create Key*. Eén script per dag kost hier een paar cent.

### 2. Repository maken

1. Maak op GitHub een nieuwe repository, bijvoorbeeld `ai-daily-brief`. *Public* is nodig voor gratis GitHub Pages.
2. Upload alle bestanden uit deze map, ook de verborgen map `.github`.

### 3. Sleutels als secrets toevoegen

*Settings* → *Secrets and variables* → *Actions* → *New repository secret*:

| Naam | Waarde |
|---|---|
| `ANTHROPIC_API_KEY` | je Claude-sleutel |
| `ELEVENLABS_API_KEY` | je ElevenLabs-sleutel |

De sleutels staan zo nooit in de code en zijn ook in de logs onzichtbaar.

### 4. GitHub Pages aanzetten

*Settings* → *Pages* → *Source*: kies **GitHub Actions**.

### 5. Eerste aflevering maken

*Actions* → *AI Daily Brief* → *Run workflow*. Laat het vinkje "Nu genereren" aan staan. Na een paar minuten staat de site op:

```
https://<jouw-gebruikersnaam>.github.io/ai-daily-brief/
```

### 6. Op je iPhone zetten

Open die link in **Safari** → deelknop → **Zet op beginscherm**. Daarna opent hij als een gewone app, zonder adresbalk.

## Aanpassen

- **Bronnen:** de lijst `FEEDS` bovenaan `bulletin.py`. Een feed die niet werkt wordt overgeslagen.
- **Toon en lengte:** de instructies in `SYSTEM` in `bulletin.py`.
- **Andere stem:** zet een variabele `VOICE_ID` in de workflow, of pas de standaardwaarde aan.
- **Aantal afleveringen online:** `KEEP_EPISODES`.

## Als het misgaat

Bij een mislukte run krijg je van GitHub een e-mail. De speler blijft dan gewoon de laatste goede aflevering tonen. De log onder *Actions* laat precies zien bij welke stap het misging:

| Melding | Oorzaak |
|---|---|
| `ElevenLabs 401` | API-sleutel ongeldig of verlopen |
| `ElevenLabs 429` | tijdelijk te druk; het script probeert het zelf nog twee keer |
| `te weinig nieuws gevonden` | feeds onbereikbaar; controleer de lijst `FEEDS` |
| `script heeft … tekens` | Claude schreef te kort of te lang; de volgende dag is het meestal weer goed |

## Kosten

Ongeveer 4.500 tekens per aflevering, dus zo'n 140.000 ElevenLabs-credits per maand. Daarnaast een paar cent per dag voor Claude. GitHub Actions en Pages zijn gratis voor publieke repositories.

Let op: GitHub Pages is openbaar. Wie de link kent, kan de afleveringen beluisteren.

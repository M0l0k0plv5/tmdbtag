#!/usr/bin/env python3
"""tmdbtag — append Jellyfin-readable TMDB ids to movie files.

Parses scene release names (Some.Film.German.2019.1080p.BluRay.x264-GRP),
looks the movie up on TMDB and renames the file:

    Some.Film.German.2019.1080p.BluRay.x264-GRP.mkv
 -> Some.Film.German.2019.1080p.BluRay.x264-GRP [tmdbid-12345].mkv

Jellyfin reads the `[tmdbid-...]` straight from the file or folder name and
matches the right movie by id — the original release name is preserved.

Standard library only, no dependencies. TMDBTAG_LANG=de for German output.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://api.themoviedb.org/3"
CONFIG_DIR = Path.home() / ".config" / "tmdbtag"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "renames.jsonl"

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".mts",
    ".wmv", ".mpg", ".mpeg", ".divx", ".flv", ".webm", ".iso", ".vob",
}

# Tokens, ab denen der Titel im Release-Namen zu Ende ist.
STOP_WORDS = {
    # Quelle
    "bluray", "blu-ray", "bdrip", "brrip", "bdremux", "remux", "dvdrip", "dvd",
    "dvdr", "dvd5", "dvd9", "web", "webrip", "web-dl", "webdl", "hdtv", "pdtv",
    "hdrip", "tvrip", "hddvd", "vhsrip", "cam", "camrip", "ts", "tc", "r5",
    "screener", "dvdscr", "bdscr", "uhd", "hdr", "hdr10", "dv", "dolby",
    "vision", "sdr", "hybrid", "amzn", "nf", "dsnp", "atvp", "hmax", "itunes",
    "netflix", "disney",
    # Codec
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "vc1",
    "av1", "10bit", "8bit", "hi10p", "hi10",
    # Audio
    "dts", "dtshd", "dts-hd", "dtsma", "truehd", "atmos", "ac3", "eac3",
    "dd", "ddp", "dd5", "ddp5", "aac", "mp3", "flac", "lpcm", "opus", "2ch",
    "6ch", "8ch",
    # Sprache / Release-Flags
    "german", "ger", "deutsch", "english", "eng", "french", "italian",
    "spanish", "multi", "multisub", "multisubs", "subbed", "dubbed", "dubbing",
    "synced", "dl", "ml", "omu", "ov", "subs", "sub", "forced",
    "proper", "repack", "rerip", "internal", "limited", "unrated", "uncut",
    "uncensored", "extended", "theatrical", "remastered", "restored", "imax",
    "complete", "custom", "readnfo", "nfofix", "dirfix", "dc", "se",
    "retail", "festival", "workprint", "open", "matte", "3d", "sbs", "hsbs",
}

STOP_RE = [
    re.compile(r"^\d{3,4}[pi]$"),          # 1080p, 720p, 1080i
    re.compile(r"^\d+bit$"),
    re.compile(r"^(dd|ddp|dts|ac3|eac3|aac)[\d.+]*$"),
    re.compile(r"^[hx]\.?26[45]$"),
    re.compile(r"^(cd|disc|disk|part|pt)\d+$"),
    re.compile(r"^\d{1,2}\.\d$"),          # 5.1 / 7.1
    re.compile(r"^s\d{1,2}(e\d{1,3})?$", re.I),
]

# Tokens, die genauso gut echte Titelwörter sein können ("Spider Web",
# "Open Water", "Cam"). Sie beenden den Titel nur, wenn kein Jahr im Namen
# steht — mit Jahr ist dieses die verlässlichere Grenze.
# "dc"/"se" stehen bewusst nicht drin: sie sind immer Director's Cut bzw.
# Special Edition und würden sonst an den Titel geraten ("Leon Der Profi DC").
SOFT_STOP = {
    "web", "ts", "tc", "dv", "cam", "nf", "open", "matte",
    "complete", "vision", "multi", "hybrid", "disney", "netflix", "itunes",
    "sub", "subs", "forced", "festival", "retail", "custom", "3d", "r5",
}

ROMAN = {"ii": 2, "iii": 3, "iv": 4, "vi": 6, "vii": 7, "viii": 8, "ix": 9}

TAG_RE = re.compile(r"\[(tmdbid|tmdb)-\d+\]", re.I)
YEAR_RE = re.compile(r"^\(?((?:19|20)\d{2})\)?$")

ISO = "%Y-%m-%dT%H:%M:%S"


# --------------------------------------------------------------------------- #
# Sprache
# --------------------------------------------------------------------------- #

# Die Oberfläche ist englisch; TMDBTAG_LANG=de schaltet auf Deutsch um.
# Schlüssel ist der englische Text, damit fehlende Übersetzungen einfach
# durchfallen statt einen Platzhalter anzuzeigen.
_DE = {
    "TMDB: API key rejected (401). ": "TMDB: API-Key ungültig (401). ",
    "Check `tmdbtag --set-key`.": "Prüfe `tmdbtag --set-key`.",
    "repeatedly no answer (rate limit?)": "wiederholt keine Antwort (evtl. Rate-Limit)",
    "uncertain → deferred: ": "unsicher → offen: ",
    "no TMDB match → deferred": "keine TMDB-Treffer → offen",
    "   no confident match and no TTY to ask → skipped":
        "   keine sichere Zuordnung, kein TTY für Rückfrage → übersprungen",
    "no TMDB match": "keine TMDB-Treffer",
    "Matches:": "Treffer:",
    "   number / [s]kip / [i]d / [n]ew search / [q]uit: ":
        "   Nummer / [s]kip / [i]d eingeben / [n]eu suchen / [q]uit: ",
    "   TMDB id: ": "   TMDB-ID: ",
    "   id not found": "   ID nicht gefunden",
    "   search term: ": "   Suchbegriff: ",
    "deferred": "übersprungen",
    "uncertain": "unsicher",
    "no match": "keine Treffer",
    "no TTY": "kein TTY",
    "network error: {e}": "Netzwerkfehler: {e}",
    "   NFO exists and is not a metadata file: {name} → skipped (--force overwrites)":
        "   NFO existiert und ist keine Metadaten-Datei: {name} → übersprungen (--force überschreibt)",
    "   NFO already exists: {name} → skipped (--force overwrites)":
        "   NFO existiert bereits: {name} → übersprungen (--force überschreibt)",
    "not found: {p}": "nicht gefunden: {p}",
    "\r   scanning … {n} video files": "\r   scanne … {n} Videodateien",
    "\r   querying TMDB … {done}/{total}": "\r   frage TMDB ab … {done}/{total}",
    "No TMDB API key.\n": "Kein TMDB-API-Key.\n",
    "  Get one for free at https://www.themoviedb.org/settings/api\n"
    "  then store it:      tmdbtag --set-key YOUR_KEY\n"
    "  or:                 export TMDB_API_KEY=YOUR_KEY":
        "  Hol dir einen (gratis) unter https://www.themoviedb.org/settings/api\n"
        "  und speichere ihn:  tmdbtag --set-key DEIN_KEY\n"
        "  oder:               export TMDB_API_KEY=DEIN_KEY",
    "key stored in {path}": "Key gespeichert in {path}",
    "No report at {path}. Run `tmdbtag --batch <dir>` first.":
        "Kein Report unter {path}. Erst `tmdbtag --batch <Ordner>` laufen lassen.",
    "gone since the report was written: {name}": "aus Report verschwunden: {name}",
    "No tagged files found.": "Keine getaggten Dateien gefunden.",
    "checking {n} tagged files": "{n} getaggte Dateien werden geprüft",
    "id {mid} does not exist on TMDB": "ID {mid} existiert auf TMDB nicht",
    "{size:.2f} GB, {mins} min": "{size:.2f} GB, {mins} min",
    "NFO agrees (#{id})": "NFO stimmt überein (#{id})",
    "not a video file, and no matching one alongside: {p}":
        "keine Videodatei, und daneben liegt keine passende: {p}",
    "   using {name} instead": "   nehme stattdessen {name}",
    "   tag it? number / [s]kip / [q]uit: ":
        "   taggen? Nummer / [s]kip / [q]uit: ",
    "Drag a file into the terminal and press Enter. [q] quits.":
        "Datei ins Terminal ziehen und Enter drücken. [q] beendet.",
    "that is a directory — pass it as an argument instead":
        "das ist ein Ordner — den bitte als Argument übergeben",
    "analyse the given files in detail and offer to tag them; without any path, take files dragged into the terminal":
        "die angegebenen Dateien ausführlich analysieren und das Taggen anbieten; ohne Pfad Dateien entgegennehmen, die ins Terminal gezogen werden",
    "no film, but TV series #{id} \"{name}\" — belongs in the shows library":
        "kein Film, aber TV-Serie #{id} \"{name}\" — gehört in die Serien-Bibliothek",
    "TV series #{id}, not a film": "TV-Serie #{id}, kein Film",
    "{n} duplicate id(s)": "{n} doppelt vergebene ID(s)",
    "   id {mid} on {n} files": "   ID {mid} auf {n} Dateien",
    "set this TMDB id on the given files directly, without searching "
    "(replaces an existing tag)":
        "diese TMDB-ID direkt auf die Dateien setzen, ohne Suche "
        "(ersetzt einen vorhandenen Tag)",
    "id {mid} unknown": "ID {mid} unbekannt",
    "NFO overrides the filename: {nfo_id} vs {file_id}":
        "NFO schlägt den Dateinamen: {nfo_id} statt {file_id}",
    "   Jellyfin reads {name} first — rename or delete it":
        "   Jellyfin liest zuerst {name} — umbenennen oder löschen",
    "NFO id {nfo_id} contradicts filename {file_id}":
        "NFO-ID {nfo_id} widerspricht Dateiname {file_id}",
    "title mismatch (similarity {sim})": "Titel passt nicht (Ähnlichkeit {sim})",
    "year {year} vs TMDB {ry}": "Jahr {year} vs. TMDB {ry}",
    "runtime {mins} min vs TMDB {rt} min": "Laufzeit {mins} min vs. TMDB {rt} min",
    "runtime {mins} min → ": "Laufzeit {mins} min → ",
    "file: \"{title}\" ({year})   TMDB #{mid}: \"{mt}\" ({ry})":
        "Datei: \"{title}\" ({year})   TMDB #{mid}: \"{mt}\" ({ry})",
    "   … {idx}/{total} checked, {n} suspect": "   … {idx}/{total} geprüft, {n} verdächtig",
    "All {n} tags look plausible.": "Alle {n} Tags plausibel.",
    "{n} of {total} suspect": "{n} von {total} verdächtig",
    "   Fix them: tmdbtag --from-report --force":
        "   Korrigieren: tmdbtag --from-report --force",
    "No rename log found.": "Kein Rename-Log vorhanden.",
    "missing, skipped: {name}": "fehlt, übersprungen: {name}",
    "Report lists no existing files any more.":
        "Report enthält keine vorhandenen Dateien mehr.",
    "{n} file(s) from {path}": "{n} Datei(en) aus {path}",
    "No video files found.": "Keine Videodateien gefunden.",
    "{n} already tagged → skipped": "{n} bereits getaggt → übersprungen",
    "Nothing to do — everything is already tagged.":
        "Nichts zu tun — alles bereits getaggt.",
    "{n} to process": "{n} zu bearbeiten",
    "  (mode: {mode})": "  (Modus: {mode})",
    "tag in name: #{id} {title}": "Tag im Namen: #{id} {title}",
    "detected: \"{title}\"": "erkannt: \"{title}\"",
    " (no year)": " (kein Jahr)",
    "   TMDB unreachable: {e} → skipped": "   TMDB nicht erreichbar: {e} → übersprungen",
    "Five consecutive errors — aborting. Everything tagged so far is kept; "
    "another run continues from there.":
        "Fünf Fehler in Folge — Abbruch. Bereits Getaggtes bleibt erhalten, "
        "ein erneuter Lauf macht dort weiter.",
    "   rename failed: {e}": "   Umbenennen fehlgeschlagen: {e}",
    "{n} tagged": "{n} getaggt",
    "{n} skipped": "{n} übersprungen",
    "{n} errors": "{n} Fehler",
    "Done: ": "Fertig: ",
    "  (dry run)": "  (Trockenlauf)",
    "{n} cases deferred": "{n} Fälle offen",
    "{n} cases would be deferred": "{n} Fälle blieben offen",
    "NFO set aside: {name}": "NFO beiseitegelegt: {name}",
    "   could not move the NFO: {e}": "   NFO konnte nicht verschoben werden: {e}",
    "{n} NFO(s) set aside": "{n} NFO(s) beiseitegelegt",
    "   Undo: tmdbtag --undo {n}": "   Rückgängig: tmdbtag --undo {n}",
    "   Now in Jellyfin: Library -> Scan with 'Replace metadata'":
        "   Jetzt in Jellyfin: Bibliothek -> Scannen mit 'Metadaten ersetzen'",
    "with --verify: move NFOs that contradict the filename aside to .nfo.bak "
    "so Jellyfin falls back to the name":
        "mit --verify: NFOs, die dem Dateinamen widersprechen, nach .nfo.bak "
        "verschieben, damit Jellyfin den Namen nutzt",
    "TMDB API key (not echoed): ": "TMDB-API-Key (wird nicht angezeigt): ",
    "No key entered.": "Kein Key eingegeben.",
    "store the API key permanently (omit the value to be prompted, "
    "keeping it out of the shell history)":
        "API-Key dauerhaft speichern (ohne Wert wird abgefragt, dann "
        "landet er nicht in der Shell-History)",
    "   Follow up: tmdbtag --from-report": "   Nacharbeiten: tmdbtag --from-report",
    "Nothing left open — removed stale report {name}.":
        "Nichts mehr offen — alter Report {name} entfernt.",
    "Now in Jellyfin: Library → Scan (tick 'Replace metadata' if needed).":
        "Jetzt in Jellyfin: Bibliothek → Scannen (ggf. 'Metadaten ersetzen').",
    " ~{m}:{s:02d} left": " ~{m}:{s:02d} verbleibend",
    "auto → ": "auto → ",
    # --help
    "Appends Jellyfin-readable [tmdbid-…] tags to movie files.":
        "Hängt Jellyfin-lesbare [tmdbid-…]-Tags an Filmdateien an.",
    "files and/or directories": "Dateien und/oder Ordner",
    "show what would happen, change nothing": "nur anzeigen, nichts ändern",
    "accept confident matches without asking":
        "sichere Treffer ohne Rückfrage übernehmen",
    "always take the best match (never ask)":
        "immer den besten Treffer nehmen (keine Rückfragen)",
    "run unattended: tag confident matches, write the rest to a report "
    "instead of asking":
        "unbeaufsichtigt durchlaufen: nur sichere Treffer taggen, "
        "unsichere in einen Report schreiben statt zu fragen",
    "report of deferred cases (default: ~/.config/tmdbtag/offen.jsonl)":
        "Report der offenen Fälle (Standard: ~/.config/tmdbtag/offen.jsonl)",
    "work through the files listed in the report instead of rescanning":
        "nur die Dateien aus dem Report abarbeiten statt neu zu scannen",
    "check existing [tmdbid-…] tags against the filename":
        "bestehende [tmdbid-…]-Tags gegen den Dateinamen gegenprüfen",
    "timeout per TMDB request in seconds (default 15)":
        "Timeout je TMDB-Anfrage in Sekunden (Standard 15)",
    "parallel TMDB lookups up front (default 6, 1 disables)":
        "parallele TMDB-Abfragen vorab (Standard 6, 1 schaltet ab)",
    "suffix (default): original name + tag; clean: 'Title (Year) [tmdbid-…]'":
        "suffix (Standard): Originalname + Tag; clean: 'Titel (Jahr) [tmdbid-…]'",
    "also tag the containing movie folder": "übergeordneten Filmordner ebenfalls taggen",
    "also append [imdbid-…]": "zusätzlich [imdbid-…] anhängen",
    "also write a <movie>.nfo carrying the TMDB id":
        "zusätzlich eine <film>.nfo mit der TMDB-ID schreiben",
    "leave filenames untouched (pair with --nfo)":
        "Dateinamen unangetastet lassen (sinnvoll mit --nfo)",
    "rename subtitles/NFO along (default on)":
        "Untertitel/NFO mit umbenennen (Standard an)",
    "also process files that already carry a tag":
        "auch Dateien bearbeiten, die schon einen Tag haben",
    "TMDB metadata language (default de-DE)": "TMDB-Sprache (Standard de-DE)",
    "minimum size in MB when scanning directories (default 50)":
        "Mindestgröße in MB beim Ordner-Scan (Standard 50)",
    "TMDB API key (v3) or read access token (v4)":
        "TMDB API-Key (v3) oder Read-Access-Token (v4)",
    "undo the last N renames": "letzte N Umbenennungen rückgängig",
}

LANG = "de" if os.environ.get("TMDBTAG_LANG", "").lower().startswith("de") else "en"


def _(s: str) -> str:
    return _DE.get(s, s) if LANG == "de" else s


# --------------------------------------------------------------------------- #
# Ausgabe
# --------------------------------------------------------------------------- #

def _c(code):
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None:
        return lambda s: f"\033[{code}m{s}\033[0m"
    return lambda s: str(s)


bold, dim, green, yellow, red, cyan = (_c(x) for x in ("1", "2", "32", "33", "31", "36"))


def info(msg):
    # flush: bei Umleitung in Datei/Pipe puffert Python sonst blockweise und
    # der Lauf sieht minutenlang aus, als täte er nichts.
    print(msg, flush=True)


def warn(msg):
    print(yellow("! ") + msg, flush=True)


def err(msg):
    print(red("✗ ") + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# TMDB-Client
# --------------------------------------------------------------------------- #

class TmdbUnavailable(Exception):
    """Netzwerk-/Serverproblem bei einer einzelnen Anfrage.

    Bei einem Lauf über tausende Dateien darf ein Aussetzer nicht den ganzen
    Durchlauf beenden — die Datei wird übersprungen, der Rest läuft weiter.
    """


class Tmdb:
    def __init__(self, key: str, lang: str = "de-DE", timeout: int = 15):
        self.key = key.strip()
        self.bearer = self.key.startswith("ey")  # v4 read access token
        self.lang = lang
        self.timeout = timeout
        self._cache: dict[str, dict] = {}

    def _get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        if not self.bearer:
            params["api_key"] = self.key
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        if url in self._cache:
            return self._cache[url]

        headers = {"Accept": "application/json", "User-Agent": "tmdbtag/1.0"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.key}"

        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                self._cache[url] = data
                return data
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 + attempt * 2)
                    continue
                if e.code == 401:
                    raise SystemExit(red(_("TMDB: API key rejected (401). ")
                                         + _("Check `tmdbtag --set-key`.")))
                if e.code == 404:
                    return {}
                if 500 <= e.code < 600:
                    time.sleep(1 + attempt)
                    continue
                raise TmdbUnavailable(f"HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 2:
                    raise TmdbUnavailable(str(e)) from e
                time.sleep(1 + attempt)
        raise TmdbUnavailable(_("repeatedly no answer (rate limit?)"))

    def search(self, query: str, year: int | None, lang: str | None = None) -> list[dict]:
        data = self._get("/search/movie", query=query, year=year,
                         language=lang or self.lang, include_adult="true")
        return data.get("results", []) or []

    def details(self, movie_id: int) -> dict:
        return self._get(f"/movie/{movie_id}", language=self.lang)

    def search_tv(self, query: str, year: int | None) -> list[dict]:
        data = self._get("/search/tv", query=query, first_air_date_year=year,
                         language=self.lang)
        return data.get("results", []) or []


# --------------------------------------------------------------------------- #
# Release-Name-Parser
# --------------------------------------------------------------------------- #

def is_stop(token: str, hard_only: bool = False) -> bool:
    t = token.lower().strip("-")
    if not t:
        return True
    head = t.split("-")[0]  # x264-GROUP
    if hard_only and (t in SOFT_STOP or head in SOFT_STOP):
        return False
    if t in STOP_WORDS or head in STOP_WORDS:
        return True
    for rx in STOP_RE:
        if rx.match(t) or rx.match(head):
            return True
    return False


def tokenize(name: str) -> list[str]:
    s = TAG_RE.sub(" ", name)
    # Jahr in eckigen Klammern retten, bevor Junk-Tags wie [RARBG] fliegen
    s = re.sub(r"[\[{]\s*((?:19|20)\d{2})\s*[\]}]", r" \1 ", s)
    s = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", s)
    s = s.replace("(", " ( ").replace(")", " ) ")
    s = re.sub(r"[._+~]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return [t for t in s.split(" ") if t and t not in {"-", "–", "(", ")"}]


def parse_release(name: str) -> tuple[str, int | None]:
    """Liefert (Suchtitel, Jahr) aus einem Release-/Dateinamen."""
    tokens = tokenize(name)
    if not tokens:
        return name, None

    max_year = datetime.now().year + 2
    year, year_idx = None, None
    for i, tok in enumerate(tokens):
        if i == 0:
            continue  # ein Jahr an Position 0 ist Teil des Titels (z. B. "1917")
        m = YEAR_RE.match(tok)
        if m and int(m.group(1)) <= max_year:
            year, year_idx = int(m.group(1)), i  # letztes plausibles Jahr gewinnt

    # Steht ein Jahr im Namen, ist es die verlässlichere Grenze: mehrdeutige
    # Tokens davor gehören dann meist zum Titel ("Spider.Web.…2023.…WEB.H265").
    stop_idx = None
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        if is_stop(tok, hard_only=year is not None):
            stop_idx = i
            break

    end = min(x for x in (year_idx, stop_idx, len(tokens)) if x is not None)
    end = max(end, 1)
    title = " ".join(tokens[:end]).strip(" -–")
    title = re.sub(r"\s+", " ", title)
    return title, year


def nfc(s: str) -> str:
    """macOS mischt NFC- und NFD-Dateinamen; für Vergleiche immer NFC nutzen."""
    return unicodedata.normalize("NFC", s)


def norm(s: str) -> str:
    s = (s or "").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\b(the|a|an|der|die|das|ein|eine|le|la|les|il|el)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    # Fortsetzungen werden mal römisch, mal arabisch geschrieben:
    # "Der Pate 02" vs. "Der Pate - Teil II". i/v/x bleiben außen vor,
    # die sind als eigenständige Wörter zu mehrdeutig ("Malcolm X").
    s = re.sub(r"\b(ii|iii|iv|vi|vii|viii|ix)\b",
               lambda m: str(ROMAN[m.group()]), s)
    s = re.sub(r"\b0+(\d)", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def fold_umlauts(s: str) -> str:
    """Scene-Releases schreiben 'Päpstin' als 'Paepstin'. Beide Seiten auf die
    kürzere Form falten, damit sie vergleichbar werden. Bewusst grob — auf
    beide Seiten angewandt schadet ein Fehltreffer wie 'queen'→'qun' nicht,
    weil similarity() zusätzlich die ungefaltete Variante prüft."""
    return re.sub(r"(ae|oe|ue)", lambda m: m.group()[0], norm(s))


def unfold_umlauts(s: str) -> str:
    """Gegenrichtung für die TMDB-Suche: 'Paepstin' → 'Päpstin'."""
    return (s.replace("ae", "ä").replace("oe", "ö").replace("ue", "ü")
             .replace("Ae", "Ä").replace("Oe", "Ö").replace("Ue", "Ü"))


def contains_title(needle: str, haystack: str) -> bool:
    """Steckt der eine Titel als zusammenhängende Wortfolge im anderen?

    Dateinamen tragen oft einen Zusatz, den TMDB nicht führt
    ("23 Nichts ist so wie es scheint" vs. "23", "Stephen Kings Der Nebel"
    vs. "Der Nebel"). Reine Zeichenähnlichkeit hält das für einen Fehler.
    """
    a, b = norm(needle).split(), norm(haystack).split()
    if not a or len(a) > len(b):
        return False
    if len("".join(a)) < 3:
        # sehr kurze Titel ("23", "M") nur am Anfang gelten lassen, sonst
        # passt "2" in jedes "… Teil 2"
        return b[:len(a)] == a
    return any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


def title_agrees(a: str, b: str) -> bool:
    """Passen zwei Titel plausibel zusammen — Ähnlichkeit oder Teilmenge."""
    return (similarity(a, b) >= 0.60
            or contains_title(a, b) or contains_title(b, a))


def similarity(a: str, b: str) -> float:
    plain = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    folded = difflib.SequenceMatcher(None, fold_umlauts(a), fold_umlauts(b)).ratio()
    return max(plain, folded)


def year_of(result: dict) -> int | None:
    d = result.get("release_date") or ""
    return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None


# --------------------------------------------------------------------------- #
# Suche mit Fallbacks
# --------------------------------------------------------------------------- #

def source_name(f: Path) -> str:
    """Woraus der Suchtitel gelesen wird — Dateiname, sonst der Ordner."""
    if (len(re.sub(r"[^a-zA-Z]", "", f.stem)) < 5
            or f.stem.lower() in {"movie", "video", "film", "vts_01_1", "index"}):
        return f.parent.name
    return f.stem


def prefetch(tmdb: Tmdb, files: list[Path], workers: int) -> None:
    """Suchergebnisse für alle Dateien vorab parallel in den Cache holen.

    Die TMDB-Abfragen sind reine Wartezeit; sie nacheinander abzuarbeiten
    dominiert die Laufzeit eines Erstlaufs. Das Umbenennen selbst bleibt
    einfädig — Dateisystemarbeit soll nachvollziehbar und abbrechbar sein.
    """
    if workers < 2 or len(files) < 4:
        return
    from concurrent.futures import ThreadPoolExecutor

    done = 0
    show = sys.stdout.isatty()

    def warm(f: Path):
        title, year = parse_release(source_name(f))
        try:
            find_candidates(tmdb, title, year)
        except TmdbUnavailable:
            pass          # der Hauptlauf meldet den Fehler an der richtigen Stelle

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(warm, files):
            done += 1
            if show and done % 5 == 0:
                print(dim(_("\r   querying TMDB … {done}/{total}").format(
                    done=done, total=len(files))), end="", flush=True)
    if show:
        print("\r" + " " * 46 + "\r", end="", flush=True)


def prefetch_details(tmdb: Tmdb, ids: list[int], workers: int) -> None:
    """Filmdetails für bekannte IDs vorab parallel in den Cache holen.

    --verify braucht je Datei genau eine Detailabfrage; nacheinander
    dominiert das die Laufzeit einer Prüfung über tausende Dateien.
    """
    if workers < 2 or len(ids) < 4:
        return
    from concurrent.futures import ThreadPoolExecutor

    done = 0
    show = sys.stdout.isatty()
    uniq = list(dict.fromkeys(ids))

    def warm(mid: int):
        try:
            tmdb.details(mid)
        except TmdbUnavailable:
            pass          # der Hauptlauf meldet den Fehler an der richtigen Stelle

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _unused in pool.map(warm, uniq):
            done += 1
            if show and done % 10 == 0:
                print(dim(_("\r   querying TMDB … {done}/{total}")
                          .format(done=done, total=len(uniq))), end="", flush=True)
    if show:
        print("\r" + " " * 46 + "\r", end="", flush=True)


def find_candidates(tmdb: Tmdb, title: str, year: int | None) -> list[dict]:
    seen, out = set(), []

    def add(results):
        for r in results:
            if r.get("id") not in seen:
                seen.add(r["id"])
                out.append(r)

    attempts = []
    if year:
        attempts += [(title, year, "de-DE"), (title, year, "en-US")]
    attempts += [(title, None, "de-DE"), (title, None, "en-US")]

    # TMDB findet "Papstin" → "Päpstin" (Diakritika werden gefaltet), aber
    # nicht die Scene-Schreibweise "Paepstin". Also zusätzlich rückübersetzen.
    umlaut = unfold_umlauts(title)
    if umlaut != title:
        attempts.insert(1 if year else 0, (umlaut, year, "de-DE"))
        attempts.append((umlaut, None, "de-DE"))

    words = title.split()
    if len(words) > 2:  # letzte Wörter abschneiden (oft Junk im Release-Namen)
        attempts.append((" ".join(words[:-1]), year, "de-DE"))
    if len(words) > 3:
        attempts.append((" ".join(words[:-2]), year, "de-DE"))

    for q, y, lang in attempts:
        if not q.strip():
            continue
        add(tmdb.search(q, y, lang))
        # Nur abbrechen, wenn wirklich etwas Gutes dabei ist — nicht schon bei
        # drei beliebigen Treffern, sonst kommen die Fallback-Schreibweisen nie dran.
        if any(score(r, title, year) >= 0.95 for r in out):
            break
    return out


def score(result: dict, title: str, year: int | None) -> float:
    s = max(similarity(title, result.get("title", "")),
            similarity(title, result.get("original_title", "")))
    ry = year_of(result)
    if year and ry:
        if ry == year:
            s += 0.25
        elif abs(ry - year) == 1:
            s += 0.08
        else:
            s -= 0.25
    pop = min(result.get("popularity", 0) / 200.0, 0.05)
    return s + pop


# --------------------------------------------------------------------------- #
# Auswahl
# --------------------------------------------------------------------------- #

def by_runtime(tmdb: Tmdb, cands: list[dict], video: Path,
               title: str = "", year: int | None = None) -> tuple[int, dict] | None:
    """Den Kandidaten wählen, dessen Laufzeit zur Datei passt.

    Laufzeit allein reicht nicht: neben "Eden" (130 min) liegt "Martin Eden"
    (129 min), neben "Maria" (112 min) liegt "Salve Maria" (111 min). Passt
    nur einer, entscheidet die Laufzeit; passen mehrere, muss zusätzlich der
    Titel klar für einen sprechen. Sonst bleibt der Fall offen.

    TMDB liefert in Suchergebnissen keine Laufzeit, das kostet je Kandidat
    eine Detailabfrage — vertretbar, weil es nur im Zweifelsfall passiert.
    """
    mins = media_duration(video)
    if not mins:
        return None
    near, far = [], []
    for c in cands:
        try:
            rt = (tmdb.details(c["id"]) or {}).get("runtime") or 0
        except TmdbUnavailable:
            return None
        if not rt:
            continue
        diff = abs(rt - mins)
        # 6 min Spielraum für Schnittfassungen, PAL-Speedup und Werbepausen
        (near if diff <= 6 else far).append((diff, c))
    if not near:
        return None

    if len(near) == 1:
        best_diff, best = near[0]
        if far and min(d for d, _ in far) - best_diff < 8:
            return None                  # zweiter Kandidat zu dicht dran
        return mins, best

    # Mehrere passen zur Laufzeit — dann muss der Titel den Ausschlag geben
    ranked = sorted(near, key=lambda p: score(p[1], title, year), reverse=True)
    first, second = score(ranked[0][1], title, year), score(ranked[1][1], title, year)
    if first - second < 0.15:
        return None
    return mins, ranked[0][1]


def fmt_result(r: dict) -> str:
    y = year_of(r) or "????"
    t = r.get("title") or r.get("original_title") or "?"
    ot = r.get("original_title") or ""
    extra = f" / {ot}" if ot and norm(ot) != norm(t) else ""
    ov = (r.get("overview") or "").replace("\n", " ")
    ov = (ov[:90] + "…") if len(ov) > 90 else ov
    return f"{bold(t)}{dim(extra)} ({y})  {dim('#' + str(r['id']))}\n      {dim(ov)}"


def ask_user(ranked: list[dict], tmdb: Tmdb, defer) -> dict | None:
    """Die interaktive Auswahl — herausgelöst, damit choose() die Entscheidung
    trifft und nicht auch noch die Bedienung führt."""
    while True:
        try:
            ans = input(dim(_("   number / [s]kip / [i]d / [n]ew search / [q]uit: "))).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SystemExit(1)
        if ans in ("q", "quit"):
            raise SystemExit(0)
        if ans in ("", "s", "skip"):
            # bewusst übersprungen — bleibt trotzdem im Report, sonst geht die
            # Datei bei einem --from-report-Durchgang verloren
            defer(_("deferred"))
            return None
        if ans in ("i", "id"):
            raw = input(dim(_("   TMDB id: "))).strip()
            m = re.search(r"\d+", raw)
            if not m:
                continue
            d = tmdb.details(int(m.group()))
            if not d:
                warn(_("   id not found"))
                continue
            info("   " + green("→ ") + fmt_result(d).split("\n")[0])
            return d
        if ans in ("n", "neu", "search"):
            q = input(dim(_("   search term: "))).strip()
            if q:
                ranked = sorted(find_candidates(tmdb, q, None),
                                key=lambda r: score(r, q, None), reverse=True)
                for i, r in enumerate(ranked[:6], 1):
                    info(f"   {cyan(str(i))}) {fmt_result(r)}")
            continue
        if ans.isdigit() and 1 <= int(ans) <= min(6, len(ranked)):
            return ranked[int(ans) - 1]


def choose(cands: list[dict], title: str, year: int | None,
           mode: str, tmdb: Tmdb, unresolved: list | None = None,
           video: Path | None = None) -> dict | None:
    """mode: auto | ask | yes | batch"""
    ranked = sorted(cands, key=lambda r: score(r, title, year), reverse=True)

    if ranked:
        top = ranked[0]
        top_score = score(top, title, year)
        runner = score(ranked[1], title, year) if len(ranked) > 1 else 0.0
        confident = top_score >= 0.90 and (top_score - runner) >= 0.12

        # Titel und Jahr geben nichts mehr her: dann entscheidet die tatsächliche
        # Laufzeit der Datei. Zwei gleichnamige Filme desselben Jahres sind
        # anders nicht zu trennen — genau daran ist "Maria" (2024) gescheitert.
        if not confident and video is not None and len(ranked) > 1:
            picked = by_runtime(tmdb, ranked[:4], video, title, year)
            if picked:
                mins, cand = picked
                info("   " + green(_("runtime {mins} min → ").format(mins=mins))
                     + fmt_result(cand).split("\n")[0])
                if mode in ("yes", "auto", "batch"):
                    return cand
                ranked = [cand] + [r for r in ranked if r["id"] != cand["id"]]
                top, confident = cand, True

        if mode == "yes" or (mode in ("auto", "batch") and confident):
            prefix = "auto → " if mode in ("auto", "batch") else "→ "
            info("   " + green(prefix) + fmt_result(top).split("\n")[0])
            return top

    def tv_hint() -> dict | None:
        """Gibt es den Titel als Serie statt als Film?

        Miniserien haben oft gar keinen Film-Eintrag. Ohne diesen Hinweis
        greift die Suche zum ähnlichsten Film und tagt den falschen.
        """
        try:
            shows = tmdb.search_tv(title, year)
        except (TmdbUnavailable, AttributeError):
            return None
        for sh in shows[:3]:
            name = sh.get("name") or sh.get("original_name") or ""
            if title_agrees(title, name) or title_agrees(title, sh.get("original_name") or ""):
                return sh
        return None

    def defer(reason: str):
        """Unsicheren Fall für die spätere Nachbearbeitung vormerken."""
        if unresolved is not None and video is not None:
            unresolved.append({
                "file": str(video), "title": title, "year": year, "reason": reason,
                "candidates": [{"id": r["id"], "title": r.get("title"),
                                "year": year_of(r)} for r in ranked[:3]],
            })

    if mode == "batch":
        if not ranked or score(ranked[0], title, year) < 0.60:
            sh = tv_hint()
            if sh:
                info("   " + yellow(_("no film, but TV series #{id} \"{name}\" — "
                                      "belongs in the shows library")
                                    .format(id=sh["id"], name=sh.get("name"))))
                defer(_("TV series #{id}, not a film").format(id=sh["id"]))
                return None
        if ranked:
            best = fmt_result(ranked[0]).split("\n")[0]
            info("   " + yellow(_("uncertain → deferred: ")) + dim(best))
            defer(_("uncertain"))
        else:
            info("   " + yellow(_("no TMDB match → deferred")))
            defer(_("no match"))
        return None

    if not sys.stdin.isatty():
        warn(_("   no confident match and no TTY to ask → skipped"))
        defer(_("no TTY"))
        return None

    if not ranked:
        info("   " + yellow(_("no TMDB match")))
        sh = tv_hint()
        if sh:
            info("   " + yellow(_("no film, but TV series #{id} \"{name}\" — "
                                  "belongs in the shows library")
                                .format(id=sh["id"], name=sh.get("name"))))
    else:
        info("   " + bold(_("Matches:")))
        for i, r in enumerate(ranked[:6], 1):
            info(f"   {cyan(str(i))}) {fmt_result(r)}")

    return ask_user(ranked, tmdb, defer)


# --------------------------------------------------------------------------- #
# Umbenennen
# --------------------------------------------------------------------------- #

def sanitize(name: str) -> str:
    # NFC vereinheitlicht neu erzeugte Namen; sonst erbt der Zieldateiname die
    # Zerlegung der Quelle und die Bibliothek bleibt gemischt.
    name = nfc(name)
    name = name.replace("/", "-").replace(":", " -")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Die meisten Dateisysteme brechen bei 255 Bytes ab; Platz für Endung lassen.
    while len(name.encode("utf-8")) > 200:
        name = name[:-1].rstrip(" .")
    return name or "unbenannt"


def build_tag(movie_id: int, imdb_id: str | None) -> str:
    tag = f"[tmdbid-{movie_id}]"
    if imdb_id:
        tag += f" [imdbid-{imdb_id}]"
    return tag


def new_stem(stem: str, movie: dict, tag: str, style: str) -> str:
    if style == "clean":
        t = movie.get("title") or movie.get("original_title") or stem
        y = year_of(movie)
        base = f"{t} ({y})" if y else t
        return sanitize(f"{base} {tag}")
    return sanitize(f"{stem} {tag}")


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def sidecars(video: Path) -> list[Path]:
    """Untertitel/NFO/Poster mit gleichem Stamm.

    Vergleich über NFC, weil macOS NFC- und NFD-Namen mischt: ein Untertitel
    von einem anderen System hat sonst denselben sichtbaren Namen wie der
    Film, ist aber ein anderer String — und bliebe beim Umbenennen liegen.
    """
    out = []
    stem = nfc(video.stem)
    for f in video.parent.iterdir():
        if f == video or f.is_dir():
            continue
        if nfc(f.name).startswith(stem) and f.suffix.lower() not in VIDEO_EXT:
            out.append(f)
    return sorted(out)


def sidecar_rest(video: Path, sc: Path) -> str:
    """Der Namensteil hinter dem Filmstamm, z. B. '.ger.forced.srt'.

    Beide Seiten erst auf NFC bringen — sonst schneidet der Längen-Index bei
    gemischter Normalisierung mitten in den Namen (aus '.ger.srt' wird
    'n.ger.srt', jedes Umlaut-Zeichen verschiebt um eins).
    """
    return nfc(sc.name)[len(nfc(video.stem)):]


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


NFO_ID_RE = re.compile(
    r"<tmdbid>\s*(\d+)\s*</tmdbid>|<uniqueid[^>]*type=\"tmdb\"[^>]*>\s*(\d+)", re.I)


def nfo_tmdb_id(video: Path) -> tuple[Path, int] | None:
    """Die TMDB-ID aus einer danebenliegenden NFO — Jellyfins Vorrangquelle.

    Jellyfin liest eine vorhandene NFO *vor* dem Dateinamen. Eine NFO mit
    falscher ID macht das Umbenennen wirkungslos, ohne dass man es sieht.
    """
    nfo = video.with_suffix(".nfo")
    if not nfo.exists():
        return None
    try:
        text = nfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = NFO_ID_RE.search(text)
    return (nfo, int(m.group(1) or m.group(2))) if m else None


RUNTIME_RE = re.compile(r"<runtime>\s*(\d+)\s*</runtime>", re.I)


def _ebml_num(fh, keep_marker: bool):
    """Ein EBML-Variable-Length-Integer lesen (ID behält den Markerbit, Größe nicht)."""
    b = fh.read(1)
    if not b or b[0] == 0:
        return None, 0
    first, mask, length = b[0], 0x80, 1
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 8:
            return None, 0
    val = first if keep_marker else first & (mask - 1)
    for c in fh.read(length - 1):
        val = (val << 8) | c
    return val, length


def _mkv_duration(fh) -> float | None:
    """Laufzeit in Sekunden aus dem Matroska-Header (Segment > Info)."""

    def walk(end: int, wanted: set, depth: int = 0):
        """Liefert {ID: (offset, size)} für gesuchte Elemente auf dieser Ebene."""
        found = {}
        while fh.tell() < end and depth < 4:
            eid, n1 = _ebml_num(fh, True)
            size, n2 = _ebml_num(fh, False)
            if eid is None or size is None:
                break
            start = fh.tell()
            if eid in wanted:
                found[eid] = (start, size)
            if size == 0x00FFFFFFFFFFFFFF:      # unbekannte Größe
                break
            fh.seek(start + size)
            if len(found) == len(wanted):
                break
        return found

    fh.seek(0, 2)
    filesize = fh.tell()
    fh.seek(0)
    seg = walk(min(filesize, 1 << 20), {0x18538067})       # Segment
    if 0x18538067 not in seg:
        return None
    off, size = seg[0x18538067]
    fh.seek(off)
    info = walk(min(off + size, off + (1 << 22)), {0x1549A966})   # Info
    if 0x1549A966 not in info:
        return None
    off, size = info[0x1549A966]
    fh.seek(off)
    parts = walk(off + size, {0x2AD7B1, 0x4489})           # TimecodeScale, Duration
    if 0x4489 not in parts:
        return None

    scale = 1_000_000
    if 0x2AD7B1 in parts:
        o, s = parts[0x2AD7B1]
        fh.seek(o)
        scale = int.from_bytes(fh.read(s), "big") or 1_000_000
    o, s = parts[0x4489]
    fh.seek(o)
    raw = fh.read(s)
    if s == 4:
        ticks = struct.unpack(">f", raw)[0]
    elif s == 8:
        ticks = struct.unpack(">d", raw)[0]
    else:
        return None
    return ticks * scale / 1e9


def _mp4_duration(fh) -> float | None:
    """Laufzeit in Sekunden aus dem MP4-mvhd-Atom."""

    def atoms(end: int):
        while fh.tell() < end - 8:
            here = fh.tell()
            head = fh.read(8)
            if len(head) < 8:
                return
            size = int.from_bytes(head[:4], "big")
            kind = head[4:8]
            if size == 1:                       # 64-Bit-Größe
                size = int.from_bytes(fh.read(8), "big")
            if size < 8:
                return
            yield kind, fh.tell(), here + size
            fh.seek(here + size)

    fh.seek(0, 2)
    filesize = fh.tell()
    fh.seek(0)
    for kind, body, end in atoms(filesize):
        if kind != b"moov":
            continue
        fh.seek(body)
        for k2, body2, _ in atoms(end):
            if k2 != b"mvhd":
                continue
            fh.seek(body2)
            ver = fh.read(4)[0]
            if ver == 1:
                fh.read(16)
                scale, dur = struct.unpack(">IQ", fh.read(12))
            else:
                fh.read(8)
                scale, dur = struct.unpack(">II", fh.read(8))
            return dur / scale if scale else None
    return None


def media_duration(video: Path) -> int | None:
    """Laufzeit der Datei in Minuten — der einzige objektive Unterscheider,
    wenn zwei Filme Titel und Jahr teilen.

    Zuerst eine vorhandene NFO (Jellyfin schreibt die echte Länge hinein,
    kostet keinen Dateizugriff), dann der Container-Header, zuletzt ffprobe.
    """
    nfo = video.with_suffix(".nfo")
    if nfo.exists():
        try:
            m = RUNTIME_RE.search(nfo.read_text(encoding="utf-8", errors="replace"))
            if m and int(m.group(1)) > 0:
                return int(m.group(1))
        except OSError:
            pass

    ext = video.suffix.lower()
    try:
        with video.open("rb") as fh:
            secs = (_mkv_duration(fh) if ext in {".mkv", ".webm"}
                    else _mp4_duration(fh) if ext in {".mp4", ".m4v", ".mov"}
                    else None)
        if secs and secs > 0:
            return round(secs / 60)
    except (OSError, ValueError, struct.error, IndexError):
        pass

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(video)],
                capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                return round(float(out) / 60)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def is_scene_nfo(p: Path) -> bool:
    """Scene-Releases legen .nfo-Dateien mit ASCII-Art ab — die sind kein XML."""
    try:
        head = p.read_bytes()[:512].lstrip()
    except OSError:
        return True
    return not head.startswith((b"<?xml", b"<movie"))


def write_nfo(video: Path, movie: dict, imdb: str | None,
              dry: bool, force: bool) -> Path | None:
    """Schreibt eine Jellyfin-lesbare <film>.nfo neben die Videodatei."""
    target = video.with_suffix(".nfo")
    if target.exists() and is_scene_nfo(target):
        target = video.parent / "movie.nfo"          # Scene-NFO nicht anfassen
    if target.exists() and not force:
        if is_scene_nfo(target):
            warn(_("   NFO exists and is not a metadata file: {name} → skipped "
                   "(--force overwrites)").format(name=target.name))
            return None
        warn(_("   NFO already exists: {name} → skipped (--force overwrites)")
             .format(name=target.name))
        return None

    title = movie.get("title") or movie.get("original_title") or video.stem
    orig = movie.get("original_title") or ""
    y = year_of(movie)
    lines = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<movie>",
        f"  <title>{xml_escape(title)}</title>",
    ]
    if orig and orig != title:
        lines.append(f"  <originaltitle>{xml_escape(orig)}</originaltitle>")
    if y:
        lines.append(f"  <year>{y}</year>")
    if movie.get("release_date"):
        lines.append(f"  <premiered>{movie['release_date']}</premiered>")
    if movie.get("overview"):
        lines.append(f"  <plot>{xml_escape(movie['overview'])}</plot>")
    lines.append(f'  <uniqueid type="tmdb" default="true">{movie["id"]}</uniqueid>')
    if imdb:
        lines.append(f'  <uniqueid type="imdb">{xml_escape(imdb)}</uniqueid>')
    lines += [f"  <tmdbid>{movie['id']}</tmdbid>", "</movie>", ""]

    info(f"   {green('NFO')} {target.name}")
    if not dry:
        target.write_text("\n".join(lines), encoding="utf-8")
    return target


def log_rename(entries: list[tuple[str, str]]):
    if not entries:
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        for src, dst in entries:
            fh.write(json.dumps({"t": datetime.now().strftime(ISO),
                                 "from": src, "to": dst}, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Sammeln
# --------------------------------------------------------------------------- #

def resolve_video(p: Path) -> Path | None:
    """Zur Videodatei auflösen, wenn ein Beifänger übergeben wurde.

    Im Finder liegen .nfo, .srt und .mkv nebeneinander; wer eine davon ins
    Terminal zieht, meint immer den Film. Ohne diese Umleitung würde der
    Beifänger selbst umbenannt und der Film bliebe unangetastet.
    """
    if p.suffix.lower() in VIDEO_EXT:
        return p
    name = nfc(p.name)
    best = None
    try:
        for v in p.parent.iterdir():
            if (v.suffix.lower() in VIDEO_EXT and not v.name.startswith("._")
                    and name.startswith(nfc(v.stem))):
                # längster passender Stamm gewinnt, falls mehrere greifen
                if best is None or len(v.stem) > len(best.stem):
                    best = v
    except OSError:
        return None
    return best


def collect(paths: list[str], min_size: int, recursive: bool) -> list[Path]:
    out: list[Path] = []
    seen = 0
    show = sys.stdout.isatty()
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.exists():
            warn(_("not found: {p}").format(p=p))
            continue
        if p.is_file():
            v = resolve_video(p)
            if v is None:
                warn(_("not a video file, and no matching one alongside: {p}")
                     .format(p=p.name))
                continue
            if v != p:
                info(dim(_("   using {name} instead").format(name=v.name)))
            out.append(v.resolve())
            continue
        it = p.rglob("*") if recursive else p.glob("*")
        for f in it:
            if f.suffix.lower() not in VIDEO_EXT:
                continue
            # macOS legt auf SMB/exFAT ._Name-Beifänger an; jedes stat() darauf
            # ist ein Netzwerk-Roundtrip für nichts.
            if f.name.startswith("._"):
                continue
            if "sample" in f.name.lower() or "sample" in f.parent.name.lower():
                continue
            if not f.is_file():
                continue
            try:
                if f.stat().st_size < min_size:
                    continue
            except OSError:
                continue
            out.append(f.resolve())
            seen += 1
            if show and seen % 50 == 0:
                print(dim(_("\r   scanning … {n} video files").format(n=seen)), end="", flush=True)
    if show and seen >= 50:
        print("\r" + " " * 40 + "\r", end="", flush=True)
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# Key-Handling
# --------------------------------------------------------------------------- #

def load_key(cli_key: str | None) -> str:
    if cli_key:
        return cli_key
    env = os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_TOKEN")
    if env:
        return env
    if CONFIG_FILE.exists():
        try:
            k = json.loads(CONFIG_FILE.read_text()).get("api_key")
            if k:
                return k
        except (OSError, ValueError):
            pass
    raise SystemExit(red(_("No TMDB API key.\n"))
        + _("  Get one for free at https://www.themoviedb.org/settings/api\n"
            "  then store it:      tmdbtag --set-key YOUR_KEY\n"
            "  or:                 export TMDB_API_KEY=YOUR_KEY"))


def save_key(key: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"api_key": key.strip()}, indent=2))
    CONFIG_FILE.chmod(0o600)
    info(green("✓ ") + _("key stored in {path}").format(path=CONFIG_FILE))


def write_report(path: Path, rows: list[dict]) -> Path | None:
    """Report schreiben — oder einen alten löschen, wenn nichts mehr offen ist.

    Ein stehengebliebener Report von gestern sieht aus wie der von heute.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        if path.exists():
            path.unlink()
        return None
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def read_report(path: Path) -> list[Path]:
    if not path.exists():
        raise SystemExit(_("No report at {path}. Run `tmdbtag --batch <dir>` first.").format(path=path))
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        f = Path(json.loads(line)["file"])
        if f.exists():
            out.append(f)
        else:
            warn(_("gone since the report was written: {name}").format(name=f.name))
    return out


PART_RE = re.compile(
    r"[ ._-]*\b(?:cd|disc|disk|part|pt|teil)[ ._-]?(\d{1,2})\b", re.I)


def split_part(stem: str) -> tuple[str, int | None]:
    """Zerlegt einen Namen in (Basis, Teilnummer).

    Ein Film auf zwei Dateien trägt in beiden denselben Titel; ohne diese
    Trennung bekämen beide einen identischen Zielnamen, und die
    Duplikat-Prüfung würde sie als Doppelbestand melden.
    """
    m = PART_RE.search(stem)
    if not m:
        return stem, None
    base = (stem[:m.start()] + stem[m.end():])
    return re.sub(r"\s{2,}", " ", base).strip(" ._-"), int(m.group(1))


def strip_tag(text: str) -> str:
    """Vorhandene [tmdbid-…]/[imdbid-…] entfernen und Lücken schließen."""
    out = re.sub(r"\[imdbid-tt\d+\]", "", TAG_RE.sub("", text), flags=re.I)
    return re.sub(r"\s{2,}", " ", out).strip()


def apply_tag(f: Path, movie: dict, imdb: str | None, args, roots: set,
              retag: bool = False) -> Path:
    """Datei, Beifänger und ggf. Ordner auf den Tag umbenennen.

    Gemeinsamer Weg für den normalen Lauf und für --id, damit eine direkt
    gesetzte ID exakt dieselbe Benennung, Protokollierung und Trockenlauf-
    Behandlung bekommt wie eine gesuchte.
    """
    tag = build_tag(movie["id"], imdb)
    video_after = f

    if args.rename:
        renames: list[tuple[Path, Path]] = []
        # Beim Neu-Taggen den alten Tag gleich beim Namensbau entfernen, statt
        # erst umzubenennen und dann nochmal — sonst tragen die Beifänger den
        # alten Tag doppelt, und das Ziel kollidiert mit sich selbst.
        base = strip_tag(f.stem) if retag else f.stem
        # Jellyfin erwartet den Teil-Hinweis am Ende, hinter dem Tag
        base, part = split_part(base)
        target_stem = new_stem(base, movie, tag, args.style)
        if part:
            target_stem = f"{target_stem} - part{part}"
        dst = f.with_name(target_stem + f.suffix)
        renames.append((f, dst if dst == f else unique_path(dst)))

        if args.sidecars:
            for sc in sidecars(f):
                rest = sidecar_rest(f, sc)              # ".ger.forced.srt"
                if retag:
                    rest = strip_tag(rest)
                sc_dst = sc.with_name(target_stem + rest)
                renames.append((sc, sc_dst if sc_dst == sc else unique_path(sc_dst)))

        folder_src = None
        if args.folder:
            d = f.parent
            # nur echte Einzelfilm-Ordner anfassen, keine Sammelordner
            solo = sum(1 for x in d.iterdir()
                       if x.is_file() and x.suffix.lower() in VIDEO_EXT
                       and "sample" not in x.name.lower()) == 1
            if solo and d not in roots and d != d.parent and not TAG_RE.search(d.name):
                if args.style == "clean":
                    t = movie.get("title") or movie.get("original_title") or d.name
                    y = year_of(movie)
                    dn = sanitize(f"{t} ({y}) {tag}" if y else f"{t} {tag}")
                else:
                    dn = sanitize(f"{d.name} {tag}")
                folder_src = d
                renames.append((d, unique_path(d.with_name(dn))))

        # Dateien zuerst, Ordner zuletzt — sonst zeigen die Dateipfade ins Leere
        for src, dst in renames:
            if src == dst:
                continue
            info(f"   {green('→')} {dst.name}")
            if args.dry_run:
                if src == f:
                    video_after = dst
                continue
            try:
                src.rename(dst)
                # Sofort protokollieren, nicht erst am Ende: sonst ist nach
                # einem Ctrl-C mitten im Lauf nichts mehr rückgängig zu machen.
                log_rename([(str(src), str(dst))])
                if src == f:
                    video_after = dst
                elif src == folder_src:
                    video_after = dst / video_after.name
            except OSError as e:
                err(_("   rename failed: {e}").format(e=e))

    if args.nfo:
        write_nfo(video_after, movie, imdb, args.dry_run, args.force)
    return video_after


def unquote_path(text: str) -> Path | None:
    """Was das Terminal beim Hineinziehen einer Datei einfügt, zu einem Pfad machen.

    macOS setzt Backslashes vor Leerzeichen und Sonderzeichen, andere
    Terminals setzen Anführungszeichen — beides muss weg.
    """
    text = text.strip()
    if not text:
        return None
    # Zuerst wörtlich nehmen: aus dem Finder kopierte Pfade tragen keine
    # Escapes, und shlex würde sie am Leerzeichen zerschneiden.
    literal = Path(text.strip("'\"")).expanduser()
    if literal.exists():
        return literal
    try:
        parts = shlex.split(text)
        if parts:
            return Path(parts[0]).expanduser()
    except ValueError:
        pass                                   # unpaarige Anführungszeichen
    return Path(text.replace("\\ ", " ").strip("'\"")).expanduser()


def inspect(tmdb: Tmdb, f: Path, args, roots: set) -> bool:
    """Alles zeigen, was über eine Datei bekannt ist, und das Taggen anbieten.

    Gedacht für den Einzelfall: Datei ins Terminal ziehen und sehen, woran
    die Zuordnung hängt, statt einen Ordnerlauf zu starten.
    """
    info("")
    info(bold(f.name))
    info(dim(f"   {f.parent}"))

    try:
        size = f.stat().st_size / 1024 ** 3
    except OSError as e:
        err(_("not found: {p}").format(p=e))
        return False
    mins = media_duration(f)
    info("   " + dim(_("{size:.2f} GB, {mins} min")
                     .format(size=size, mins=mins if mins else "?")))

    # Was steht schon im Namen und in der NFO?
    tagged = TAG_RE.search(f.name)
    current = None
    if tagged:
        mid = int(re.search(r"\d+", tagged.group()).group())
        current = tmdb.details(mid) or None
        if current:
            info("   " + green(_("tag in name: #{id} {title}")
                               .format(id=mid, title=f"{current.get('title')} "
                                       f"({year_of(current) or '?'}, "
                                       f"{current.get('runtime') or '?'} min)")))
        else:
            info("   " + red(_("id {mid} does not exist on TMDB").format(mid=mid)))
    nfo = nfo_tmdb_id(f)
    if nfo:
        if tagged and nfo[1] != int(re.search(r"\d+", tagged.group()).group()):
            info("   " + red(_("NFO overrides the filename: {nfo_id} vs {file_id}")
                             .format(nfo_id=nfo[1],
                                     file_id=re.search(r"\d+", tagged.group()).group())))
        else:
            info("   " + dim(_("NFO agrees (#{id})").format(id=nfo[1])))

    title, year = parse_release(strip_tag(source_name(f)))
    info("   " + dim(_('detected: "{title}"').format(title=title)
                     + (f" ({year})" if year else _(" (no year)"))))

    cands = find_candidates(tmdb, title, year)
    if not cands:
        info("   " + yellow(_("no TMDB match")))
        try:
            for sh in tmdb.search_tv(title, year)[:2]:
                if title_agrees(title, sh.get("name") or ""):
                    info("   " + yellow(_('no film, but TV series #{id} "{name}" — '
                                          "belongs in the shows library")
                                        .format(id=sh["id"], name=sh.get("name"))))
                    break
        except TmdbUnavailable:
            pass
        return False

    ranked = sorted(cands, key=lambda r: score(r, title, year), reverse=True)[:6]
    info("   " + bold(_("Matches:")))
    for i, r in enumerate(ranked, 1):
        rt = (tmdb.details(r["id"]) or {}).get("runtime") or 0
        fit = ""
        if mins and rt:
            d = abs(rt - mins)
            fit = (green(f"  {rt} min ✓") if d <= 6
                   else dim(f"  {rt} min ({d:+} min)") if d <= 20
                   else yellow(f"  {rt} min ({rt - mins:+} min)"))
        mark = " " if not current or r["id"] != current.get("id") else dim(" ←")
        info(f"   {cyan(str(i))}) {fmt_result(r).splitlines()[0]}{fit}{mark}")

    if not sys.stdin.isatty():
        return False
    try:
        ans = input(dim(_("   tag it? number / [s]kip / [q]uit: "))).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if ans in ("q", "quit"):
        raise SystemExit(0)
    if not ans.isdigit() or not 1 <= int(ans) <= len(ranked):
        return False

    movie = ranked[int(ans) - 1]
    imdb = (tmdb.details(movie["id"]) or {}).get("imdb_id") if args.imdb else None
    apply_tag(f, movie, imdb, args, roots, retag=True)
    if nfo and args.fix_nfo:
        disable_nfo(nfo[0], args.dry_run)
    return True


def drag_loop(tmdb: Tmdb, args) -> int:
    """Dateien nacheinander entgegennehmen, bis der Benutzer aufhört."""
    info(bold(_("Drag a file into the terminal and press Enter. [q] quits.")))
    done = 0
    while True:
        try:
            raw = input(cyan("\n› "))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if raw.strip().lower() in ("q", "quit", "exit"):
            break
        f = unquote_path(raw)
        if f is None:
            continue
        if not f.exists():
            warn(_("not found: {p}").format(p=f))
            continue
        if f.is_dir():
            warn(_("that is a directory — pass it as an argument instead"))
            continue
        v = resolve_video(f)
        if v is None:
            warn(_("not a video file, and no matching one alongside: {p}")
                 .format(p=f.name))
            continue
        if v != f:
            info(dim(_("   using {name} instead").format(name=v.name)))
        f = v
        try:
            if inspect(tmdb, f, args, {f.parent}):
                done += 1
        except TmdbUnavailable as e:
            err(_("   TMDB unreachable: {e} → skipped").format(e=e))
    info(bold(_("Done: ") + _("{n} tagged").format(n=done)))
    return 0


def set_id(tmdb: Tmdb, files: list[Path], movie_id: int, args, roots: set) -> int:
    """Eine bekannte TMDB-ID direkt setzen, ohne Suche.

    Für Fälle, die keine Heuristik lösen kann: Film unter anderem Titel
    veröffentlicht, TMDB-Eintrag erst nachträglich angelegt, oder eine
    Zuordnung, die man von Hand recherchiert hat.
    """
    try:
        movie = tmdb.details(movie_id)
    except TmdbUnavailable as e:
        raise SystemExit(_("   TMDB unreachable: {e} → skipped").format(e=e))
    if not movie:
        raise SystemExit(_("id {mid} does not exist on TMDB").format(mid=movie_id))

    imdb = movie.get("imdb_id") if args.imdb else None
    info(bold(f"#{movie_id}  {movie.get('title')} "
              f"({year_of(movie) or '?'}, {movie.get('runtime') or '?'} min)") + "\n")

    for f in files:
        info(bold(f.name))
        apply_tag(f, movie, imdb, args, roots, retag=True)
        info("")

    info(bold(_("Done: ") + _("{n} tagged").format(n=len(files)))
         + (dim(_("  (dry run)")) if args.dry_run else ""))
    return 0



def disable_nfo(nfo: Path, dry: bool) -> Path | None:
    """Eine widersprechende NFO beiseiteschieben statt löschen.

    Jellyfin liest die NFO vor dem Dateinamen; solange eine mit falscher ID
    daneben liegt, bleibt der Film falsch zugeordnet. `.nfo.bak` wird von
    Jellyfin ignoriert und lässt sich jederzeit zurückholen.
    """
    target = unique_path(nfo.with_suffix(nfo.suffix + ".bak"))
    info("   " + green(_("NFO set aside: {name}").format(name=target.name)))
    if dry:
        return target
    try:
        nfo.rename(target)
        log_rename([(str(nfo), str(target))])
        return target
    except OSError as e:
        err(_("   could not move the NFO: {e}").format(e=e))
        return None


def verify(tmdb: Tmdb, files: list[Path], report: Path | None,
           fix_nfo: bool = False, dry: bool = False, workers: int = 6) -> int:
    """Bestehende [tmdbid-N]-Tags gegen den Dateinamen gegenprüfen.

    Findet Tags, die auf einen ganz anderen Film zeigen — etwa aus einem
    früheren Lauf mit -y oder aus Handarbeit.
    """
    tagged = [(f, int(m.group(1)))
              for f in files if (m := re.search(r"\[tmdbid-(\d+)\]", f.name, re.I))]
    if not tagged:
        raise SystemExit(_("No tagged files found."))

    info(bold(_("checking {n} tagged files").format(n=len(tagged))))
    prefetch_details(tmdb, [mid for _f, mid in tagged], workers)
    info("")
    suspect: list[dict] = []
    fixed: list[str] = []
    by_id: dict[int, list[str]] = {}
    for idx, (f, mid) in enumerate(tagged, 1):
        by_id.setdefault(mid, []).append(f.name)
        # Zuerst die NFO: sie schlägt den Dateinamen, also nützt ein korrekter
        # Tag nichts, solange daneben eine NFO mit anderer ID liegt.
        nfo = nfo_tmdb_id(f)
        if nfo and nfo[1] != mid:
            info(f"{dim(f'[{idx}/{len(tagged)}]')} {bold(f.name[:70])}")
            info("   " + red(_("NFO overrides the filename: {nfo_id} vs {file_id}")
                             .format(nfo_id=nfo[1], file_id=mid)))
            if fix_nfo:
                disable_nfo(nfo[0], dry)
                fixed.append(str(nfo[0]))
            else:
                info("   " + dim(_("   Jellyfin reads {name} first — rename or delete it")
                                 .format(name=nfo[0].name)))
                suspect.append({"file": str(f), "nfo": str(nfo[0]),
                                "reason": _("NFO id {nfo_id} contradicts filename {file_id}")
                                .format(nfo_id=nfo[1], file_id=mid)})
            continue

        stem = TAG_RE.sub("", f.stem)
        title, year = parse_release(stem)
        try:
            movie = tmdb.details(mid)
        except TmdbUnavailable as e:
            err(f"[{idx}/{len(tagged)}] {f.name[:60]}: {e}")
            continue
        if not movie:
            info(f"{dim(f'[{idx}/{len(tagged)}]')} {bold(f.name[:70])}")
            info("   " + red(_("id {mid} does not exist on TMDB").format(mid=mid)))
            suspect.append({"file": str(f), "reason": _("id {mid} unknown").format(mid=mid), "title": title})
            continue

        sim = max(similarity(title, movie.get("title", "")),
                  similarity(title, movie.get("original_title", "")))
        agrees = (title_agrees(title, movie.get("title", ""))
                  or title_agrees(title, movie.get("original_title", "")))
        ry = year_of(movie)
        year_off = bool(year and ry and abs(ry - year) > 1)

        # Laufzeit nur bei grober Abweichung melden: Extended Cuts, Remaster
        # und PAL-Fassungen weichen regelmäßig um 10-20 Minuten ab, ein
        # anderer Film fast immer um ein Vielfaches davon.
        mins, rt = media_duration(f), movie.get("runtime") or 0
        runtime_off = bool(mins and rt and abs(rt - mins) > max(30, rt * 0.4))
        if not agrees or year_off or runtime_off:
            info(f"{dim(f'[{idx}/{len(tagged)}]')} {bold(f.name[:70])}")
            reason = []
            if not agrees:
                reason.append(_("title mismatch (similarity {sim})").format(sim=f"{sim:.2f}"))
            if year_off:
                reason.append(_("year {year} vs TMDB {ry}").format(year=year, ry=ry))
            if runtime_off:
                reason.append(_("runtime {mins} min vs TMDB {rt} min")
                              .format(mins=mins, rt=rt))
            info("   " + yellow(" / ".join(reason)))
            info("   " + dim(_('file: "{title}" ({year})   TMDB #{mid}: "{mt}" ({ry})')
                         .format(title=title, year=year, mid=mid,
                                 mt=movie.get("title"), ry=ry)))
            suspect.append({"file": str(f), "reason": " / ".join(reason),
                            "title": title, "year": year,
                            "candidates": [{"id": mid, "title": movie.get("title"),
                                            "year": ry}]})
        elif idx % 25 == 0:
            info(dim(_("   … {idx}/{total} checked, {n} suspect")
                    .format(idx=idx, total=len(tagged), n=len(suspect))))

    # Mehrteiler tragen zu Recht dieselbe ID — nur echte Doppel melden
    dupes = {i: names for i, names in by_id.items()
             if len(names) > 1
             and len({split_part(Path(n).stem)[1] for n in names}) < len(names)}
    if dupes:
        info("")
        info(bold(yellow(_("{n} duplicate id(s)").format(n=len(dupes)))))
        for i, names in list(dupes.items())[:10]:
            info("   " + yellow(_("   id {mid} on {n} files")
                                .format(mid=i, n=len(names))))
            for nm in names:
                info("      " + dim(nm[:76]))
            suspect.append({"file": names[0],
                            "reason": _("   id {mid} on {n} files")
                            .format(mid=i, n=len(names))})

    info("")
    if fixed:
        info(bold(green(_("{n} NFO(s) set aside").format(n=len(fixed))))
             + (dim(_("  (dry run)")) if dry else ""))
        if not dry:
            info(dim(_("   Undo: tmdbtag --undo {n}").format(n=len(fixed))))
        info(dim(_("   Now in Jellyfin: Library -> Scan with 'Replace metadata'")))
    if not suspect:
        if not fixed:
            info(bold(green(_("All {n} tags look plausible.").format(n=len(tagged)))))
        return 0
    info(bold(yellow(_("{n} of {total} suspect").format(n=len(suspect), total=len(tagged)))))
    if report:
        written = write_report(report, suspect)
        if written:
            info(f"   → {written}")
            info(dim(_("   Fix them: tmdbtag --from-report --force")))
    return 0


def undo_last(count: int, dry: bool):
    if not LOG_FILE.exists():
        raise SystemExit(_("No rename log found."))
    lines = [json.loads(l) for l in LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = lines[-count:]
    for e in reversed(todo):
        dst, src = Path(e["to"]), Path(e["from"])
        if not dst.exists():
            warn(_("missing, skipped: {name}").format(name=dst.name))
            continue
        info(f"{dst.name}\n  → {src.name}")
        if not dry:
            dst.rename(src)
    if not dry:
        LOG_FILE.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                                    for e in lines[:-count]), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tmdbtag",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=_("Appends Jellyfin-readable [tmdbid-…] tags to movie files."),
        epilog="""examples:
  tmdbtag --set-key abc123…
  tmdbtag ~/Movies                  # interactive, recurses into directories
  tmdbtag -n ~/Movies               # dry run, changes nothing
  tmdbtag --batch ~/Movies          # unattended, defers uncertain cases
  tmdbtag --from-report             # work through the deferred ones
  tmdbtag --verify ~/Movies         # check existing tags
  tmdbtag --undo 5                  # roll back the last 5 renames

Set TMDBTAG_LANG=de for German output.
""")
    ap.add_argument("paths", nargs="*", help=_("files and/or directories"))
    ap.add_argument("-n", "--dry-run", action="store_true", help=_("show what would happen, change nothing"))
    ap.add_argument("--auto", action="store_true",
                    help=_("accept confident matches without asking"))
    ap.add_argument("-y", "--yes", action="store_true",
                    help=_("always take the best match (never ask)"))
    ap.add_argument("--batch", action="store_true",
                    help=_("run unattended: tag confident matches, write the rest to a "
                           "report instead of asking"))
    ap.add_argument("--report", metavar="FILE",
                    help=_("report of deferred cases "
                           "(default: ~/.config/tmdbtag/offen.jsonl)"))
    ap.add_argument("--from-report", action="store_true",
                    help=_("work through the files listed in the report instead of rescanning"))
    ap.add_argument("--id", type=int, metavar="N",
                    help=_("set this TMDB id on the given files directly, without "
                           "searching (replaces an existing tag)"))
    ap.add_argument("--inspect", action="store_true",
                    help=_("analyse the given files in detail and offer to tag them; without any path, take files dragged into the terminal"))
    ap.add_argument("--verify", action="store_true",
                    help=_("check existing [tmdbid-…] tags against the filename"))
    ap.add_argument("--fix-nfo", action="store_true",
                    help=_("with --verify: move NFOs that contradict the filename "
                           "aside to .nfo.bak so Jellyfin falls back to the name"))
    ap.add_argument("--timeout", type=int, default=15,
                    help=_("timeout per TMDB request in seconds (default 15)"))
    ap.add_argument("--workers", type=int, default=6, metavar="N",
                    help=_("parallel TMDB lookups up front (default 6, 1 disables)"))
    ap.add_argument("--style", choices=["suffix", "clean"], default="suffix",
                    help=_("suffix (default): original name + tag; "
                           "clean: 'Title (Year) [tmdbid-…]'"))
    ap.add_argument("--folder", action="store_true",
                    help=_("also tag the containing movie folder"))
    ap.add_argument("--imdb", action="store_true", help=_("also append [imdbid-…]"))
    ap.add_argument("--nfo", action="store_true",
                    help=_("also write a <movie>.nfo carrying the TMDB id"))
    ap.add_argument("--no-rename", dest="rename", action="store_false", default=True,
                    help=_("leave filenames untouched (pair with --nfo)"))
    ap.add_argument("--sidecars", action="store_true", default=True,
                    help=_("rename subtitles/NFO along (default on)"))
    ap.add_argument("--no-sidecars", dest="sidecars", action="store_false")
    ap.add_argument("--force", action="store_true",
                    help=_("also process files that already carry a tag"))
    ap.add_argument("--lang", default="de-DE", help=_("TMDB metadata language (default de-DE)"))
    ap.add_argument("--min-size", type=int, default=50,
                    help=_("minimum size in MB when scanning directories (default 50)"))
    ap.add_argument("--no-recursive", dest="recursive", action="store_false", default=True)
    ap.add_argument("--api-key", help=_("TMDB API key (v3) or read access token (v4)"))
    ap.add_argument("--set-key", nargs="?", const="", metavar="KEY",
                    help=_("store the API key permanently (omit the value to be "
                           "prompted, keeping it out of the shell history)"))
    ap.add_argument("--undo", type=int, metavar="N", help=_("undo the last N renames"))
    return ap


def gather_files(args, report_path: Path) -> list[Path]:
    """Die zu bearbeitenden Dateien bestimmen — aus dem Report oder vom Scan."""
    if args.from_report:
        files = read_report(report_path)
        if not files:
            raise SystemExit(_("Report lists no existing files any more."))
        info(dim(_("{n} file(s) from {path}").format(n=len(files), path=report_path)))
        return files
    return collect(args.paths, args.min_size * 1024 * 1024, args.recursive)


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    if args.set_key is not None:
        key = args.set_key
        if not key:
            import getpass
            key = getpass.getpass(_("TMDB API key (not echoed): ")).strip()
        if not key:
            raise SystemExit(_("No key entered."))
        save_key(key)
        return 0
    if args.undo:
        undo_last(args.undo, args.dry_run)
        return 0
    # --from-report bezieht die Dateien aus dem Report, braucht also keinen Pfad
    if not args.paths and not args.from_report:
        if args.inspect:
            return drag_loop(Tmdb(load_key(args.api_key), args.lang, args.timeout), args)
        ap.print_help()
        return 1

    tmdb = Tmdb(load_key(args.api_key), args.lang, args.timeout)
    report_path = (Path(args.report).expanduser() if args.report
                   else CONFIG_DIR / "offen.jsonl")

    files = gather_files(args, report_path)
    if not files:
        raise SystemExit(_("No video files found."))

    if args.verify:
        return verify(tmdb, files, report_path, args.fix_nfo, args.dry_run,
                      args.workers)

    if args.inspect:
        roots = {Path(p).expanduser().resolve() for p in args.paths}
        n = sum(1 for f in files if inspect(tmdb, f, args, roots))
        info("")
        info(bold(_("Done: ") + _("{n} tagged").format(n=n)))
        return 0

    if args.id:
        return set_id(tmdb, files, args.id,
                      args, {Path(p).expanduser().resolve() for p in args.paths})

    return run_tagging(tmdb, files, args, report_path)


def run_tagging(tmdb: Tmdb, files: list[Path], args, report_path: Path) -> int:
    """Der eigentliche Durchlauf: auflösen, taggen, offene Fälle sammeln."""
    mode = ("yes" if args.yes else "batch" if args.batch
            else "auto" if args.auto else "ask")
    roots = {Path(p).expanduser().resolve() for p in args.paths}
    done = skipped = failed = 0
    unresolved: list[dict] = []
    net_errors = 0

    # Bereits getaggte Dateien vorab aussortieren, statt 600 Zeilen
    # "übersprungen" auszugeben, in denen die eigentliche Arbeit untergeht.
    if args.rename and not args.force:
        already = [f for f in files if TAG_RE.search(f.name)]
        files = [f for f in files if not TAG_RE.search(f.name)]
        skipped += len(already)
        if already:
            info(dim(_("{n} already tagged → skipped").format(n=len(already))))

    total = len(files)
    if not total:
        info(bold(_("Nothing to do — everything is already tagged.")))
        return 0
    info(bold(_("{n} to process").format(n=total))
         + dim(_("  (mode: {mode})").format(mode=mode)))
    prefetch(tmdb, files, args.workers)
    info("")

    started = time.time()
    for idx, f in enumerate(files, 1):
        eta = ""
        if idx > 3:
            per = (time.time() - started) / (idx - 1)
            left = int(per * (total - idx + 1))
            eta = dim(_(" ~{m}:{s:02d} left").format(m=left // 60, s=left % 60)) if left > 45 else ""
        counter = dim(f"[{idx:>{len(str(total))}}/{total}]") + eta + " "
        header = f"{counter}{bold(f.name)}"
        tagged = TAG_RE.search(f.name)
        movie = None

        try:
            if tagged and not args.force:
                # --no-rename: ID steht schon im Namen, nur Details für die NFO holen
                info(header)
                movie = tmdb.details(int(re.search(r"\d+", tagged.group()).group()))
                if movie:
                    info("   " + dim(_("tag in name: #{id} {title}")
                                 .format(id=movie["id"], title=movie.get("title", ""))))

            if movie is None:
                title, year = parse_release(source_name(f))
                info(header)
                info("   " + dim(_('detected: "{title}"').format(title=title)
                             + (f" ({year})" if year else _(" (no year)"))))

                cands = find_candidates(tmdb, title, year)
                movie = choose(cands, title, year, mode, tmdb, unresolved, f)

            imdb = None
            if movie and args.imdb:
                imdb = (tmdb.details(movie["id"]) or {}).get("imdb_id")
        except TmdbUnavailable as e:
            err(_("   TMDB unreachable: {e} → skipped").format(e=e))
            unresolved.append({"file": str(f), "reason": _("network error: {e}").format(e=e)})
            failed += 1
            net_errors += 1
            if net_errors >= 5:
                err(_("Five consecutive errors — aborting. Everything tagged so far is "
                      "kept; another run continues from there."))
                info("")
                break
            info("")
            continue

        net_errors = 0
        if not movie:
            skipped += 1
            info("")
            continue

        apply_tag(f, movie, imdb, args, roots)
        done += 1
        info("")

    parts = [_("{n} tagged").format(n=done), _("{n} skipped").format(n=skipped)]
    if failed:
        parts.append(_("{n} errors").format(n=failed))
    info(bold(_("Done: ") + ", ".join(parts))
         + (dim(_("  (dry run)")) if args.dry_run else ""))

    if args.dry_run:
        # Ein Trockenlauf darf den Report nicht anfassen: er gehört zum letzten
        # echten Lauf, unter Umständen über ganz andere Ordner.
        if unresolved:
            info(yellow(_("{n} cases would be deferred")
                        .format(n=len(unresolved))))
        return 0

    had_report = report_path.exists()
    written = write_report(report_path, unresolved)
    if written:
        info(yellow(_("{n} cases deferred").format(n=len(unresolved))) + f" → {written}")
        info(dim(_("   Follow up: tmdbtag --from-report")))
    elif had_report:
        info(dim(_("Nothing left open — removed stale report {name}.").format(name=report_path.name)))

    if done and not args.dry_run:
        info(dim(_("Now in Jellyfin: Library → Scan (tick 'Replace metadata' if needed).")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)

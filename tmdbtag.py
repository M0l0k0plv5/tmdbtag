#!/usr/bin/env python3
"""tmdbtag — append Jellyfin-readable TMDB ids to movie files.

Parses scene release names (Some.Film.German.2019.1080p.BluRay.x264-GRP),
looks the movie up on TMDB and renames the file:

    Some.Film.German.2019.1080p.BluRay.x264-GRP.mkv
 -> Some.Film.German.2019.1080p.BluRay.x264-GRP [tmdbid-12345].mkv

Jellyfin reads the `[tmdbid-...]` straight from the file name and matches
the right movie by id — the original release name is preserved.

    tmdbtag PATH...          tag movies (confident matches automatically)
    tmdbtag check PATH...    check existing tags
    tmdbtag undo             undo the last run
    -n                       dry run, for all of the above

Standard library only, no dependencies. TMDBTAG_LANG=de for German output.
"""

from __future__ import annotations

import difflib
import json
import os
import re
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

# Was früher Flags waren, sind jetzt feste Werte.
TMDB_LANG = "de-DE"         # deutsche Titel finden deutsche Scene-Releases am besten
TIMEOUT = 15                # Sekunden je TMDB-Anfrage
WORKERS = 6                 # parallele Abfragen vorab
MIN_SIZE = 50 * 1024 ** 2   # kleinere Videos beim Ordner-Scan ignorieren (Samples, Trailer)

# Kennung des Laufs im Rename-Log, damit `undo` genau ihn zurücknimmt.
# main() setzt sie bei jedem Aufruf neu.
RUN_ID = ""

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

# Tokens, die genauso gut echte Titelwörter sein können ("Iron Web",
# "Open Fields", "Cam"). Sie beenden den Titel nur, wenn kein Jahr im Namen
# steht — mit Jahr ist dieses die verlässlichere Grenze.
# "dc"/"se" stehen bewusst nicht drin: sie sind immer Director's Cut bzw.
# Special Edition und würden sonst an den Titel geraten ("Lino Der Bote DC").
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
    "TMDB: API key rejected (401). ":
        "TMDB: API-Key ungültig (401). ",
    "Delete {path} and run again to enter a new one.":
        "{path} löschen und erneut starten, um einen neuen einzugeben.",
    "repeatedly no answer (rate limit?)":
        "wiederholt keine Antwort (evtl. Rate-Limit)",
    "\r   querying TMDB … {done}/{total}":
        "\r   frage TMDB ab … {done}/{total}",
    "   number / [s]kip / [i]d / [n]ew search / [q]uit: ":
        "   Nummer / [s]kip / [i]d eingeben / [n]eu suchen / [q]uit: ",
    "   TMDB id: ":
        "   TMDB-ID: ",
    "   id not found":
        "   ID nicht gefunden",
    "   search term: ":
        "   Suchbegriff: ",
    "runtime {mins} min → ":
        "Laufzeit {mins} min → ",
    "no film, but TV series #{id} \"{name}\" — belongs in the shows library":
        "kein Film, aber TV-Serie #{id} \"{name}\" — gehört in die Serien-Bibliothek",
    "uncertain → skipped: ":
        "unsicher → übersprungen: ",
    "no TMDB match":
        "keine TMDB-Treffer",
    "Matches:":
        "Treffer:",
    "not found: {p}":
        "nicht gefunden: {p}",
    "not a video file, and no matching one alongside: {p}":
        "keine Videodatei, und daneben liegt keine passende: {p}",
    "   using {name} instead":
        "   nehme stattdessen {name}",
    "\r   scanning … {n} video files":
        "\r   scanne … {n} Videodateien",
    "No TMDB API key.\n":
        "Kein TMDB-API-Key.\n",
    "  Get one for free at https://www.themoviedb.org/settings/api\n  then run tmdbtag once in a terminal (it asks for the key)\n  or:  export TMDB_API_KEY=YOUR_KEY":
        "  Hol dir einen (gratis) unter https://www.themoviedb.org/settings/api\n  und starte tmdbtag einmal im Terminal (fragt nach dem Key)\n  oder:  export TMDB_API_KEY=DEIN_KEY",
    "No TMDB API key yet — get one for free at https://www.themoviedb.org/settings/api":
        "Noch kein TMDB-API-Key — gratis unter https://www.themoviedb.org/settings/api",
    "TMDB API key (not echoed): ":
        "TMDB-API-Key (wird nicht angezeigt): ",
    "No key entered.":
        "Kein Key eingegeben.",
    "key stored in {path}":
        "Key gespeichert in {path}",
    "   rename failed: {e}":
        "   Umbenennen fehlgeschlagen: {e}",
    "NFO set aside: {name}":
        "NFO beiseitegelegt: {name}",
    "   could not move the NFO: {e}":
        "   NFO konnte nicht verschoben werden: {e}",
    "NFO overrides the filename: {nfo_id} vs {file_id}":
        "NFO schlägt den Dateinamen: {nfo_id} statt {file_id}",
    "id {mid} does not exist on TMDB":
        "ID {mid} existiert auf TMDB nicht",
    "id {mid} unknown":
        "ID {mid} unbekannt",
    "title mismatch (similarity {sim})":
        "Titel passt nicht (Ähnlichkeit {sim})",
    "year {year} vs TMDB {ry}":
        "Jahr {year} vs. TMDB {ry}",
    "runtime {mins} min vs TMDB {rt} min":
        "Laufzeit {mins} min vs. TMDB {rt} min",
    "{n} duplicate id(s)":
        "{n} doppelt vergebene ID(s)",
    "   id {mid} on {n} files":
        "   ID {mid} auf {n} Dateien",
    "{n} NFO(s) set aside":
        "{n} NFO(s) beiseitegelegt",
    "  (dry run)":
        "  (Trockenlauf)",
    "   Undo: tmdbtag undo":
        "   Rückgängig: tmdbtag undo",
    "   Now in Jellyfin: Library -> Scan with 'Replace metadata'":
        "   Jetzt in Jellyfin: Bibliothek -> Scannen mit 'Metadaten ersetzen'",
    "All {n} tags look plausible.":
        "Alle {n} Tags plausibel.",
    "{n} of {total} suspect":
        "{n} von {total} verdächtig",
    "   Re-identify one: tmdbtag FILE":
        "   Einzeln neu zuordnen: tmdbtag DATEI",
    "No tagged files found.":
        "Keine getaggten Dateien gefunden.",
    "checking {n} tagged files":
        "{n} getaggte Dateien werden geprüft",
    "   … {idx}/{total} checked, {n} suspect":
        "   … {idx}/{total} geprüft, {n} verdächtig",
    "No rename log found.":
        "Kein Rename-Log vorhanden.",
    "missing, skipped: {name}":
        "fehlt, übersprungen: {name}",
    "{n} rename(s) undone":
        "{n} Umbenennung(en) rückgängig gemacht",
    "unknown option: {opt}":
        "unbekannte Option: {opt}",
    "undo takes no path.":
        "undo nimmt keinen Pfad.",
    "No video files found.":
        "Keine Videodateien gefunden.",
    "{n} already tagged → skipped":
        "{n} bereits getaggt → übersprungen",
    " ~{m}:{s:02d} left":
        " ~{m}:{s:02d} verbleibend",
    "tag in name: #{id} {title}":
        "Tag im Namen: #{id} {title}",
    " (no year)":
        " (kein Jahr)",
    "{n} tagged":
        "{n} getaggt",
    "{n} skipped":
        "{n} übersprungen",
    "{n} errors":
        "{n} Fehler",
    "Done: ":
        "Fertig: ",
    "Now in Jellyfin: Library → Scan (tick 'Replace metadata' if needed).":
        "Jetzt in Jellyfin: Bibliothek → Scannen (ggf. 'Metadaten ersetzen').",
    "Nothing to do — everything is already tagged.":
        "Nichts zu tun — alles bereits getaggt.",
    "{n} to process":
        "{n} zu bearbeiten",
    "   TMDB unreachable: {e} → skipped":
        "   TMDB nicht erreichbar: {e} → übersprungen",
    "Five consecutive errors — aborting. Everything tagged so far is kept; another run continues from there.":
        "Fünf Fehler in Folge — Abbruch. Bereits Getaggtes bleibt erhalten, ein erneuter Lauf macht dort weiter.",
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
    def __init__(self, key: str, lang: str = TMDB_LANG, timeout: int = TIMEOUT):
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
                    raise SystemExit(red(_("TMDB: API key rejected (401). "))
                                     + _("Delete {path} and run again to enter a new one.")
                                     .format(path=CONFIG_FILE))
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
            continue  # ein Jahr an Position 0 ist Teil des Titels (z. B. "1934")
        m = YEAR_RE.match(tok)
        if m and int(m.group(1)) <= max_year:
            year, year_idx = int(m.group(1)), i  # letztes plausibles Jahr gewinnt

    # Steht ein Jahr im Namen, ist es die verlässlichere Grenze: mehrdeutige
    # Tokens davor gehören dann meist zum Titel ("Iron.Web.…2023.…WEB.H265").
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
    # "Der Hafen 02" vs. "Der Hafen - Teil II". i/v/x bleiben außen vor,
    # die sind als eigenständige Wörter zu mehrdeutig ("Agent X").
    s = re.sub(r"\b(ii|iii|iv|vi|vii|viii|ix)\b",
               lambda m: str(ROMAN[m.group()]), s)
    s = re.sub(r"\b0+(\d)", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def fold_umlauts(s: str) -> str:
    """Scene-Releases schreiben 'Fähre' als 'Faehre'. Beide Seiten auf die
    kürzere Form falten, damit sie vergleichbar werden. Bewusst grob — auf
    beide Seiten angewandt schadet ein Fehltreffer wie 'queen'→'qun' nicht,
    weil similarity() zusätzlich die ungefaltete Variante prüft."""
    return re.sub(r"(ae|oe|ue)", lambda m: m.group()[0], norm(s))


def unfold_umlauts(s: str) -> str:
    """Gegenrichtung für die TMDB-Suche: 'Faehre' → 'Fähre'."""
    return (s.replace("ae", "ä").replace("oe", "ö").replace("ue", "ü")
             .replace("Ae", "Ä").replace("Oe", "Ö").replace("Ue", "Ü"))


def contains_title(needle: str, haystack: str) -> bool:
    """Steckt der eine Titel als zusammenhängende Wortfolge im anderen?

    Dateinamen tragen oft einen Zusatz, den TMDB nicht führt
    ("47 Alles ist anders als gedacht" vs. "47", "Hanna Bergs Der Morast"
    vs. "Der Morast"). Reine Zeichenähnlichkeit hält das für einen Fehler.
    """
    a, b = norm(needle).split(), norm(haystack).split()
    if not a or len(a) > len(b):
        return False
    if len("".join(a)) < 3:
        # sehr kurze Titel ("47", "M") nur am Anfang gelten lassen, sonst
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
        for _res in pool.map(warm, files):
            done += 1
            if show and done % 5 == 0:
                print(dim(_("\r   querying TMDB … {done}/{total}").format(
                    done=done, total=len(files))), end="", flush=True)
    if show:
        print("\r" + " " * 46 + "\r", end="", flush=True)


def prefetch_details(tmdb: Tmdb, ids: list[int], workers: int) -> None:
    """Filmdetails für bekannte IDs vorab parallel in den Cache holen.

    check braucht je Datei genau eine Detailabfrage; nacheinander
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

    # TMDB findet "Fahre" → "Fähre" (Diakritika werden gefaltet), aber
    # nicht die Scene-Schreibweise "Faehre". Also zusätzlich rückübersetzen.
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

    Laufzeit allein reicht nicht: neben "Ufer" (130 min) liegt "Am Ufer"
    (129 min), neben "Odila" (112 min) liegt "Salve Odila" (111 min). Passt
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


def ask_user(ranked: list[dict], tmdb: Tmdb) -> dict | None:
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
           mode: str, tmdb: Tmdb, video: Path | None = None) -> dict | None:
    """mode: auto (sichere Treffer ohne Rückfrage) | ask (immer fragen)

    Ohne Terminal wird nie gefragt: Unsicheres bleibt dann einfach ungetaggt
    und kommt beim nächsten Lauf wieder dran.
    """
    ranked = sorted(cands, key=lambda r: score(r, title, year), reverse=True)

    if ranked:
        top = ranked[0]
        top_score = score(top, title, year)
        runner = score(ranked[1], title, year) if len(ranked) > 1 else 0.0
        confident = top_score >= 0.90 and (top_score - runner) >= 0.12

        # Titel und Jahr geben nichts mehr her: dann entscheidet die tatsächliche
        # Laufzeit der Datei. Zwei gleichnamige Filme desselben Jahres sind
        # anders nicht zu trennen — genau daran ist "Odila" (2024) gescheitert.
        if not confident and video is not None and len(ranked) > 1:
            picked = by_runtime(tmdb, ranked[:4], video, title, year)
            if picked:
                mins, cand = picked
                info("   " + green(_("runtime {mins} min → ").format(mins=mins))
                     + fmt_result(cand).split("\n")[0])
                if mode == "auto":
                    return cand
                ranked = [cand] + [r for r in ranked if r["id"] != cand["id"]]

        if mode == "auto" and confident:
            info("   " + green("auto → ") + fmt_result(top).split("\n")[0])
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

    if not ranked or score(ranked[0], title, year) < 0.60:
        sh = tv_hint()
        if sh:
            info("   " + yellow(_("no film, but TV series #{id} \"{name}\" — "
                                  "belongs in the shows library")
                                .format(id=sh["id"], name=sh.get("name"))))
            return None

    if not sys.stdin.isatty():
        if ranked:
            info("   " + yellow(_("uncertain → skipped: "))
                 + dim(fmt_result(ranked[0]).split("\n")[0]))
        else:
            info("   " + yellow(_("no TMDB match")))
        return None

    if not ranked:
        info("   " + yellow(_("no TMDB match")))
    else:
        info("   " + bold(_("Matches:")))
        for i, r in enumerate(ranked[:6], 1):
            info(f"   {cyan(str(i))}) {fmt_result(r)}")

    return ask_user(ranked, tmdb)


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


def build_tag(movie_id: int) -> str:
    return f"[tmdbid-{movie_id}]"


def new_stem(stem: str, tag: str) -> str:
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


def log_rename(entries: list[tuple[str, str]]):
    if not entries:
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        for src, dst in entries:
            fh.write(json.dumps({"t": datetime.now().strftime(ISO), "run": RUN_ID,
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


def collect(paths: list[str], min_size: int | None = None) -> list[Path]:
    if min_size is None:
        min_size = MIN_SIZE
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
        for f in p.rglob("*"):
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

def load_key() -> str:
    """Key aus Umgebung oder Config — bewusst nicht von der Kommandozeile.

    argv steht in der Shell-History und ist per `ps` für jeden Benutzer der
    Maschine sichtbar, also gibt es keinen Weg, den Key als Argument zu setzen.
    """
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
    return ask_key()


def ask_key() -> str:
    """Beim ersten Start nach dem Key fragen — ohne Echo, nie über argv."""
    if not sys.stdin.isatty():
        raise SystemExit(red(_("No TMDB API key.\n"))
            + _("  Get one for free at https://www.themoviedb.org/settings/api\n"
                "  then run tmdbtag once in a terminal (it asks for the key)\n"
                "  or:  export TMDB_API_KEY=YOUR_KEY"))
    import getpass
    info(_("No TMDB API key yet — get one for free at "
           "https://www.themoviedb.org/settings/api"))
    key = getpass.getpass(_("TMDB API key (not echoed): ")).strip()
    if not key:
        raise SystemExit(_("No key entered."))
    save_key(key)
    return key


def save_key(key: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"api_key": key.strip()}, indent=2))
    CONFIG_FILE.chmod(0o600)
    info(green("✓ ") + _("key stored in {path}").format(path=CONFIG_FILE))


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


def apply_tag(f: Path, movie: dict, dry: bool, retag: bool = False) -> Path:
    """Datei und Beifänger (Untertitel, NFO, Poster) auf den Tag umbenennen."""
    tag = build_tag(movie["id"])
    video_after = f
    renames: list[tuple[Path, Path]] = []
    # Beim Neu-Taggen den alten Tag gleich beim Namensbau entfernen, statt
    # erst umzubenennen und dann nochmal — sonst tragen die Beifänger den
    # alten Tag doppelt, und das Ziel kollidiert mit sich selbst.
    base = strip_tag(f.stem) if retag else f.stem
    # Jellyfin erwartet den Teil-Hinweis am Ende, hinter dem Tag
    base, part = split_part(base)
    target_stem = new_stem(base, tag)
    if part:
        target_stem = f"{target_stem} - part{part}"
    dst = f.with_name(target_stem + f.suffix)
    renames.append((f, dst if dst == f else unique_path(dst)))

    for sc in sidecars(f):
        rest = sidecar_rest(f, sc)              # ".ger.forced.srt"
        if retag:
            rest = strip_tag(rest)
        sc_dst = sc.with_name(target_stem + rest)
        renames.append((sc, sc_dst if sc_dst == sc else unique_path(sc_dst)))

    for src, dst in renames:
        if src == dst:
            continue
        info(f"   {green('→')} {dst.name}")
        if dry:
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
        except OSError as e:
            err(_("   rename failed: {e}").format(e=e))
    return video_after


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


def _nfo_conflict(nfo: tuple[Path, int], mid: int, head: str, dry: bool) -> bool:
    """Eine NFO, die dem Dateinamen widerspricht, melden und beiseitelegen."""
    info(head)
    info("   " + red(_("NFO overrides the filename: {nfo_id} vs {file_id}")
                     .format(nfo_id=nfo[1], file_id=mid)))
    return disable_nfo(nfo[0], dry) is not None


def _check_tag(tmdb: Tmdb, f: Path, mid: int, head: str) -> dict | None:
    """Einen [tmdbid-N]-Tag gegen TMDB prüfen — Verdachtsfall oder None.

    TmdbUnavailable wird durchgereicht, damit der Aufrufer Netzfehler von
    inhaltlichen Abweichungen unterscheiden kann.
    """
    stem = TAG_RE.sub("", f.stem)
    title, year = parse_release(stem)
    movie = tmdb.details(mid)
    if not movie:
        info(head)
        info("   " + red(_("id {mid} does not exist on TMDB").format(mid=mid)))
        return {"file": str(f), "reason": _("id {mid} unknown").format(mid=mid),
                "title": title}

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
    if not (not agrees or year_off or runtime_off):
        return None

    info(head)
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
    return {"file": str(f), "reason": " / ".join(reason),
            "title": title, "year": year,
            "candidates": [{"id": mid, "title": movie.get("title"), "year": ry}]}


def _duplicate_ids(by_id: dict[int, list[str]]) -> list[dict]:
    """Mehrteiler tragen zu Recht dieselbe ID — nur echte Doppel melden."""
    dupes = {i: names for i, names in by_id.items()
             if len(names) > 1
             and len({split_part(Path(n).stem)[1] for n in names}) < len(names)}
    if not dupes:
        return []
    info("")
    info(bold(yellow(_("{n} duplicate id(s)").format(n=len(dupes)))))
    found = []
    for i, names in list(dupes.items())[:10]:
        info("   " + yellow(_("   id {mid} on {n} files")
                            .format(mid=i, n=len(names))))
        for nm in names:
            info("      " + dim(nm[:76]))
        found.append({"file": names[0],
                      "reason": _("   id {mid} on {n} files")
                      .format(mid=i, n=len(names))})
    return found


def _verify_summary(total: int, suspect: list[dict], fixed: int, dry: bool) -> int:
    info("")
    if fixed:
        info(bold(green(_("{n} NFO(s) set aside").format(n=fixed)))
             + (dim(_("  (dry run)")) if dry else ""))
        if not dry:
            info(dim(_("   Undo: tmdbtag undo")))
        info(dim(_("   Now in Jellyfin: Library -> Scan with 'Replace metadata'")))
    if not suspect:
        if not fixed:
            info(bold(green(_("All {n} tags look plausible.").format(n=total))))
        return 0
    info(bold(yellow(_("{n} of {total} suspect")
                     .format(n=len(suspect), total=total))))
    for row in suspect[:20]:
        info("   " + dim(Path(row["file"]).name[:76]))
    info(dim(_("   Re-identify one: tmdbtag FILE")))
    return 0


def verify(tmdb: Tmdb, files: list[Path], dry: bool = False) -> int:
    """Bestehende [tmdbid-N]-Tags gegen den Dateinamen gegenprüfen.

    Findet Tags, die auf einen ganz anderen Film zeigen — etwa aus einem
    früheren Lauf oder aus Handarbeit. NFOs mit anderer ID werden
    beiseitegelegt, weil Jellyfin sie vor dem Dateinamen liest.
    """
    tagged = [(f, int(m.group(1)))
              for f in files if (m := re.search(r"\[tmdbid-(\d+)\]", f.name, re.I))]
    if not tagged:
        raise SystemExit(_("No tagged files found."))

    info(bold(_("checking {n} tagged files").format(n=len(tagged))))
    prefetch_details(tmdb, [mid for _f, mid in tagged], WORKERS)
    info("")
    suspect: list[dict] = []
    fixed = 0
    by_id: dict[int, list[str]] = {}
    for idx, (f, mid) in enumerate(tagged, 1):
        by_id.setdefault(mid, []).append(str(f))
        head = f"{dim(f'[{idx}/{len(tagged)}]')} {bold(f.name[:70])}"

        # Zuerst die NFO: sie schlägt den Dateinamen, also nützt ein korrekter
        # Tag nichts, solange daneben eine NFO mit anderer ID liegt.
        nfo = nfo_tmdb_id(f)
        if nfo and nfo[1] != mid:
            fixed += _nfo_conflict(nfo, mid, head, dry)
            continue

        try:
            entry = _check_tag(tmdb, f, mid, head)
        except TmdbUnavailable as e:
            err(f"[{idx}/{len(tagged)}] {f.name[:60]}: {e}")
            continue
        if entry:
            suspect.append(entry)
        elif idx % 25 == 0:
            info(dim(_("   … {idx}/{total} checked, {n} suspect")
                    .format(idx=idx, total=len(tagged), n=len(suspect))))

    suspect += _duplicate_ids(by_id)
    return _verify_summary(len(tagged), suspect, fixed, dry)


def undo_last(dry: bool) -> int:
    """Den letzten Lauf zurücknehmen — alle Umbenennungen mit derselben Lauf-ID."""
    if not LOG_FILE.exists():
        raise SystemExit(_("No rename log found."))
    lines = [json.loads(l) for l in LOG_FILE.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    if not lines:
        raise SystemExit(_("No rename log found."))
    last = lines[-1].get("run")
    keep = len(lines) - 1
    if last is not None:
        while keep > 0 and lines[keep - 1].get("run") == last:
            keep -= 1
    for e in reversed(lines[keep:]):
        dst, src = Path(e["to"]), Path(e["from"])
        if not dst.exists():
            warn(_("missing, skipped: {name}").format(name=dst.name))
            continue
        info(f"{dst.name}\n  → {src.name}")
        if not dry:
            dst.rename(src)
    info(bold(_("{n} rename(s) undone").format(n=len(lines) - keep))
         + (dim(_("  (dry run)")) if dry else ""))
    if not dry:
        LOG_FILE.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                                    for e in lines[:keep]), encoding="utf-8")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

USAGE = """usage:
  tmdbtag [-n] PATH...          tag movies: confident matches automatically,
                                the rest is offered to pick from
  tmdbtag [-n] check PATH...    check existing tags, set contradicting NFOs aside
  tmdbtag [-n] undo             undo the last run

  -n    dry run: show what would happen, change nothing

Already tagged files are skipped when scanning a folder. Pass a single
tagged file directly to re-identify it. The TMDB API key is asked for on
the first run. Set TMDBTAG_LANG=de for German output.
"""


def parse_args(argv: list[str]) -> tuple[str, list[str], bool]:
    """(Modus, Pfade, Trockenlauf) — bewusst ohne argparse: drei Modi, ein Flag."""
    dry = False
    rest = []
    for a in argv:
        if a in ("-n", "--dry-run"):
            dry = True
        elif a in ("-h", "--help"):
            info(USAGE.rstrip())
            raise SystemExit(0)
        elif a.startswith("-") and len(a) > 1:
            raise SystemExit(_("unknown option: {opt}").format(opt=a) + "\n\n" + USAGE.rstrip())
        else:
            rest.append(a)
    mode = "tag"
    if rest and rest[0] in ("check", "undo"):
        mode = rest.pop(0)
    return mode, rest, dry


def main(argv: list[str] | None = None) -> int:
    global RUN_ID
    RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + str(os.getpid())
    mode, paths, dry = parse_args(sys.argv[1:] if argv is None else argv)

    if mode == "undo":
        if paths:
            raise SystemExit(_("undo takes no path."))
        return undo_last(dry)
    if not paths:
        info(USAGE.rstrip())
        return 1

    tmdb = Tmdb(load_key())
    files = collect(paths)
    if not files:
        raise SystemExit(_("No video files found."))

    if mode == "check":
        return verify(tmdb, files, dry)
    explicit = {Path(p).expanduser().resolve() for p in paths if Path(p).expanduser().is_file()}
    return run_tagging(tmdb, files, explicit, dry)


def _split_tagged(files: list[Path], explicit: set) -> tuple[list[Path], int]:
    """Bereits getaggte Dateien vorab aussortieren, statt 600 Zeilen
    "übersprungen" auszugeben, in denen die eigentliche Arbeit untergeht.

    Eine direkt übergebene getaggte Datei bleibt drin: die soll neu
    zugeordnet werden.
    """
    already = [f for f in files if TAG_RE.search(f.name) and f not in explicit]
    if already:
        info(dim(_("{n} already tagged → skipped").format(n=len(already))))
    return [f for f in files if f not in already], len(already)


def _progress_header(f: Path, idx: int, total: int, started: float) -> str:
    """Zähler, ETA und Dateiname — die Kopfzeile pro Datei."""
    eta = ""
    if idx > 3:
        per = (time.time() - started) / (idx - 1)
        left = int(per * (total - idx + 1))
        eta = dim(_(" ~{m}:{s:02d} left").format(m=left // 60, s=left % 60)) if left > 45 else ""
    counter = dim(f"[{idx:>{len(str(total))}}/{total}]") + eta + " "
    return f"{counter}{bold(f.name)}"


def _resolve_movie(tmdb: Tmdb, f: Path, mode: str, header: str) -> dict | None:
    """Den Film zu einer Datei per Suche bestimmen.

    TmdbUnavailable reicht sie an den Aufrufer durch, der die Fehlerserie zählt.
    """
    title, year = parse_release(strip_tag(source_name(f)))
    info(header)
    tagged = TAG_RE.search(f.name)
    if tagged:
        mid = int(re.search(r"\d+", tagged.group()).group())
        current = tmdb.details(mid)
        info("   " + dim(_("tag in name: #{id} {title}")
                         .format(id=mid, title=(current or {}).get("title", "?"))))
    info("   " + dim(_('detected: "{title}"').format(title=title)
                     + (f" ({year})" if year else _(" (no year)"))))
    cands = find_candidates(tmdb, title, year)
    return choose(cands, title, year, mode, tmdb, f)


def _tagging_summary(done: int, skipped: int, failed: int, dry: bool) -> int:
    parts = [_("{n} tagged").format(n=done), _("{n} skipped").format(n=skipped)]
    if failed:
        parts.append(_("{n} errors").format(n=failed))
    info(bold(_("Done: ") + ", ".join(parts))
         + (dim(_("  (dry run)")) if dry else ""))
    if done and not dry:
        info(dim(_("   Undo: tmdbtag undo")))
        info(dim(_("Now in Jellyfin: Library → Scan (tick 'Replace metadata' if needed).")))
    return 0


def run_tagging(tmdb: Tmdb, files: list[Path], explicit: set, dry: bool) -> int:
    """Der eigentliche Durchlauf: auflösen und taggen.

    Sichere Treffer gehen automatisch durch, unsichere werden zur Auswahl
    angeboten. Ohne Terminal bleiben sie ungetaggt — ein späterer Lauf im
    Terminal erwischt dann genau diese, weil alles andere schon getaggt ist.
    """
    done = failed = net_errors = 0
    files, skipped = _split_tagged(files, explicit)
    total = len(files)
    if not total:
        info(bold(_("Nothing to do — everything is already tagged.")))
        return 0
    info(bold(_("{n} to process").format(n=total)))
    prefetch(tmdb, files, WORKERS)
    info("")

    started = time.time()
    for idx, f in enumerate(files, 1):
        header = _progress_header(f, idx, total, started)
        retag = bool(TAG_RE.search(f.name))
        # Neu zuordnen heißt: der Benutzer zweifelt am Tag — also immer fragen.
        mode = "ask" if retag and sys.stdin.isatty() else "auto"
        try:
            movie = _resolve_movie(tmdb, f, mode, header)
        except TmdbUnavailable as e:
            err(_("   TMDB unreachable: {e} → skipped").format(e=e))
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

        apply_tag(f, movie, dry, retag=retag)
        done += 1
        info("")

    return _tagging_summary(done, skipped, failed, dry)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)

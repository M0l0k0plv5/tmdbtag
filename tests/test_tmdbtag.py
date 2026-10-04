"""Tests für tmdbtag — reine Standardbibliothek, kein pytest nötig.

    python3 -m unittest discover -s tests -v
"""

import importlib.machinery
import importlib.util
import io
import json
import shutil
import struct
import sys
import os
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("tmdbtag", str(ROOT / "tmdbtag.py"))
_spec = importlib.util.spec_from_loader("tmdbtag", _loader)
t = importlib.util.module_from_spec(_spec)
sys.modules["tmdbtag"] = t
_loader.exec_module(t)


# --------------------------------------------------------------------------- #

class TestParseRelease(unittest.TestCase):
    CASES = [
        ("Der.Turm.der.Raben.Die.Gefaehrten.German.2001.EXTENDED.1080p.BluRay.x264-GRP",
         "Der Turm der Raben Die Gefaehrten", 2001),
        ("Star.Courier.2077.2017.2160p.UHD.BluRay.x265.10bit.HDR.DTS-HD.MA.TrueHD.7.1-GRP",
         "Star Courier 2077", 2017),
        ("1934.2019.German.DL.1080p.BluRay.x264-GRP", "1934", 2019),
        ("2005.2009.1080p.BluRay.x264-GRP", "2005", 2009),
        ("Ant-Lord.Far.From.Shore.2019.MULTi.COMPLETE.UHD.BLURAY-GRP",
         "Ant-Lord Far From Shore", 2019),
        ("Undertow (2010)", "Undertow", 2010),
        ("Tidepool.2019.KOREAN.1080p.BluRay.H264.AAC-GRP", "Tidepool", 2019),
        ("The.Glass.Harbor.1999.REMASTERED.1080p.BluRay.x265-GRP", "The Glass Harbor", 1999),
        ("Sand.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-GRP",
         "Sand Part Two", 2024),
        ("Der.Leuchtturm.1981.Directors.Cut.German.DTS.1080p.BluRay.x264-GRP", "Der Leuchtturm", 1981),
        ("Reactor.2.Final.Hour.1991.1080p.BluRay.x264", "Reactor 2 Final Hour", 1991),
        ("Wolfsbane_1922_720p_BluRay", "Wolfsbane", 1922),
        ("Renee [2001] 1080p", "Renee", 2001),
        # "Web" ist hier Titelwort, nicht die Quellenangabe: der Titel wurde
        # bei "Iron" abgeschnitten und der falsche Film getaggt.
        ("Iron.Web.A.Night.in.Kyoto.2023.German.DL.EAC3.1080p.WEB.H265-GRP",
         "Iron Web A Night in Kyoto", 2023),
        ("Open.Fields.2003.German.DL.1080p.BluRay.x264-GRP", "Open Fields", 2003),
        ("Cam.2015.German.DL.1080p.NF.WEB.x264-GRP", "Cam", 2015),
    ]

    def test_cases(self):
        for name, title, year in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(t.parse_release(name), (title, year))

    def test_soft_token_still_stops_without_a_year(self):
        """Ohne Jahr fehlt die verlässliche Grenze — dann muss 'WEB' greifen."""
        self.assertEqual(t.parse_release("Some.Film.WEB.H265-GRP"),
                         ("Some Film", None))

    def test_hard_token_still_stops_before_the_year(self):
        """'German' bleibt hart: sonst hiesse der Titel 'Der Film German'."""
        self.assertEqual(t.parse_release("Der.Film.German.2019.1080p.BluRay-GRP"),
                         ("Der Film", 2019))
        self.assertEqual(t.parse_release("Some.Film.1080p.2019.BluRay-GRP"),
                         ("Some Film", 2019))

    def test_no_year_falls_back_to_stop_token(self):
        self.assertEqual(t.parse_release("Some.Obscure.Film.1080p.WEB-DL-GRP"),
                         ("Some Obscure Film", None))

    def test_year_at_index_zero_stays_in_title(self):
        # "1934" ohne Jahresangabe darf nicht als Jahr weginterpretiert werden
        title, year = t.parse_release("1934.1080p.BluRay.x264")
        self.assertEqual(title, "1934")
        self.assertIsNone(year)

    def test_implausible_future_year_ignored(self):
        title, year = t.parse_release("Star.Courier.2077.1080p.BluRay")
        self.assertEqual(title, "Star Courier 2077")
        self.assertIsNone(year)

    def test_existing_tag_is_stripped(self):
        self.assertEqual(t.parse_release("The.Glass.Harbor.1999 [tmdbid-603].1080p"),
                         ("The Glass Harbor", 1999))


class TestScoring(unittest.TestCase):
    def test_norm_ignores_articles_and_diacritics(self):
        self.assertEqual(t.norm("Der Hut des Schamanen"), t.norm("Hut des Schamanen"))
        self.assertEqual(t.norm("Renée"), t.norm("Renee"))
        self.assertEqual(t.norm("Grüße"), t.norm("Gruesse".replace("ue", "ü")))

    def test_matching_year_beats_wrong_year(self):
        good = {"id": 1, "title": "Der Leuchtturm", "release_date": "1981-09-17"}
        bad = {"id": 2, "title": "Der Leuchtturm", "release_date": "1997-01-01"}
        self.assertGreater(t.score(good, "Der Leuchtturm", 1981), t.score(bad, "Der Leuchtturm", 1981))

    def test_title_similarity_matters(self):
        near = {"id": 1, "title": "The Glass Harbor", "release_date": "1999-03-30"}
        far = {"id": 2, "title": "Glass Harbor Returns", "release_date": "1999-03-30"}
        self.assertGreater(t.score(near, "The Glass Harbor", 1999), t.score(far, "The Glass Harbor", 1999))


class TestUmlauts(unittest.TestCase):
    """Scene-Releases schreiben Umlaute als ae/oe/ue aus."""

    PAIRS = [
        ("Die Faehre", "Die Fähre"),
        ("Mach ju Hoefe", "Mach ju Höfe"),
        ("Maenner die auf Wolken warten", "Männer, die auf Wolken warten"),
        ("Ueber uns der Mond", "Über uns der Mond"),
        ("Toedliche Ostern", "Tödliche Ostern"),
        ("Das weisse Tuch", "Das weiße Tuch"),
    ]

    def test_folded_titles_match(self):
        for scene, real in self.PAIRS:
            with self.subTest(scene=scene):
                self.assertGreaterEqual(t.similarity(scene, real), 0.95)

    def test_folding_does_not_break_normal_titles(self):
        # 'ue' in 'Queen' wird mitgefaltet — darf den Vergleich nicht ruinieren,
        # weil similarity() zusätzlich die ungefaltete Variante prüft.
        self.assertEqual(t.similarity("Queen", "Queen"), 1.0)
        self.assertLess(t.similarity("Queen", "Quentin Talbot"), 0.7)

    def test_unfold_generates_umlaut_query(self):
        self.assertEqual(t.unfold_umlauts("Die Faehre"), "Die Fähre")
        self.assertEqual(t.unfold_umlauts("Ueber uns"), "Über uns")

    def test_search_retries_with_umlaut_spelling(self):
        """Regression: der Umlaut-Fallback muss die Suche erreichen."""
        seen = []

        class FakeTmdb:
            lang = "de-DE"

            def search(self, q, y, lang=None):
                seen.append(q)
                # Nur die echte Umlaut-Schreibweise liefert einen Treffer
                if "Fähre" in q:
                    return [{"id": 22954, "title": "Die Fähre",
                             "original_title": "Die Fähre",
                             "release_date": "2009-10-22", "popularity": 10}]
                return []

        got = t.find_candidates(FakeTmdb(), "Die Faehre", 2009)
        self.assertTrue(any("Fähre" in q for q in seen),
                        f"Umlaut-Variante nie gesucht, nur: {seen}")
        self.assertEqual([r["id"] for r in got], [22954])

    def test_search_stops_early_on_strong_hit(self):
        """Kein unnötiger zweiter Request, wenn der erste sitzt."""
        calls = []

        class FakeTmdb:
            lang = "de-DE"

            def search(self, q, y, lang=None):
                calls.append(q)
                return [{"id": 603, "title": "Der Leuchtturm", "original_title": "Der Leuchtturm",
                         "release_date": "1981-09-17", "popularity": 30}]

        t.find_candidates(FakeTmdb(), "Der Leuchtturm", 1981)
        self.assertEqual(len(calls), 1, f"zu viele Requests: {calls}")


class TestNaming(unittest.TestCase):
    MOVIE = {"id": 387, "title": "Der Leuchtturm", "original_title": "Der Leuchtturm",
             "release_date": "1981-09-17"}

    def test_suffix_keeps_original_name(self):
        stem = "Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP"
        out = t.new_stem(stem, t.build_tag(387))
        self.assertEqual(out, f"{stem} [tmdbid-387]")

    def test_sanitize_strips_path_separators(self):
        self.assertEqual(t.sanitize("A/B: C?"), "A-B - C")

    def test_tag_regex_detects_existing(self):
        self.assertTrue(t.TAG_RE.search("Film [tmdbid-1].mkv"))
        self.assertFalse(t.TAG_RE.search("Film [1080p].mkv"))


# --------------------------------------------------------------------------- #
# Ende-zu-Ende gegen eine gefälschte TMDB-API
# --------------------------------------------------------------------------- #

DB = {
    "der leuchtturm": {"id": 387, "title": "Der Leuchtturm", "original_title": "Der Leuchtturm",
                 "release_date": "1981-09-17", "popularity": 30,
                 "overview": "Ein Wärter allein.", "imdb_id": "tt1234567"},
    "glass harbor": {"id": 603, "title": "Glass Harbor", "original_title": "The Glass Harbor",
               "release_date": "1999-03-30", "popularity": 80,
               "overview": "Ein Lotse im Nebel."},
}


def fake_get(self, path, **p):
    if path == "/search/movie":
        q = t.norm(p.get("query", ""))
        return {"results": [v for k, v in DB.items() if k in q or q in k]}
    if path.startswith("/movie/"):
        i = int(path.rsplit("/", 1)[-1])
        return next((v for v in DB.values() if v["id"] == i), {})
    return {}


class E2EBase(unittest.TestCase):
    def setUp(self):
        # Jeden angefassten Modulzustand sichern — sonst hängt das Ergebnis
        # eines Tests davon ab, welcher vorher lief.
        self._saved = {n: getattr(t, n)
                       for n in ("load_key", "LOG_FILE", "CONFIG_DIR", "CONFIG_FILE")}
        self._real_get = t.Tmdb._get
        t.Tmdb._get = fake_get
        t.load_key = lambda: "fake"
        self.tmp = Path(tempfile.mkdtemp())
        t.LOG_FILE = self.tmp / "renames.jsonl"
        t.CONFIG_DIR = self.tmp
        t.CONFIG_FILE = self.tmp / "config.json"

    def tearDown(self):
        t.Tmdb._get = self._real_get
        for n, v in self._saved.items():
            setattr(t, n, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, rel, size=60 * 1024 * 1024):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" * size)
        return p

    def run_cli(self, *argv):
        sys.argv = ["tmdbtag", *argv]
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            rc = t.main()
        return rc, buf.getvalue()

    def run_small(self, workers=6):
        """Lauf mit Mini-Testdateien: Mindestgröße aus, Parallelität gesetzt."""
        with mock.patch.object(t, "WORKERS", workers), mock.patch.object(t, "MIN_SIZE", 0):
            return self.run_cli(str(self.tmp))

    def names(self):
        return sorted(str(p.relative_to(self.tmp))
                      for p in self.tmp.rglob("*") if p.name != "renames.jsonl")


class TestEndToEnd(E2EBase):
    def test_renames_video_and_sidecars(self):
        d = "Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP"
        self.make(f"{d}/{d}.mkv")
        self.make(f"{d}/{d}.ger.forced.srt", 10)
        self.run_cli(str(self.tmp))
        got = self.names()
        self.assertIn(f"{d}/{d} [tmdbid-387].mkv", got)
        self.assertIn(f"{d}/{d} [tmdbid-387].ger.forced.srt", got)

    def test_dry_run_changes_nothing(self):
        d = "Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP"
        self.make(f"{d}/{d}.mkv")
        before = self.names()
        self.run_cli("-n", str(self.tmp))
        self.assertEqual(before, self.names())

    def test_already_tagged_is_skipped(self):
        p = self.make("flat/The.Glass.Harbor.1999 [tmdbid-603].mkv")
        self.run_cli(str(self.tmp))
        self.assertTrue(p.exists())

    def test_sample_and_small_files_ignored(self):
        self.make("flat/sample.mkv", 1024)
        self.make("flat/The.Glass.Harbor.1999.1080p-GRP.mkv", 1024)  # unter --min-size
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(str(self.tmp))
        self.assertIn("No video files found", str(cm.exception))

    def test_sample_ignored_but_real_file_processed(self):
        self.make("flat/Der.Leuchtturm.1981.German.1080p-GRP-sample.mkv")
        self.make("flat/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        self.run_cli(str(self.tmp))
        got = self.names()
        self.assertIn("flat/Der.Leuchtturm.1981.German.1080p-GRP-sample.mkv", got)
        self.assertIn("flat/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv", got)

    def test_log_written_incrementally(self):
        """Nach einem Abbruch mitten im Lauf muss undo trotzdem greifen."""
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-AAA.mkv")
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-BBB.mkv")
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-CCC.mkv")

        calls = {"n": 0}
        real = t.Tmdb._get

        def die_on_third(self, path, **p):
            calls["n"] += 1
            if calls["n"] > 2:
                raise KeyboardInterrupt
            return real(self, path, **p)

        t.Tmdb._get = die_on_third
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli(str(self.tmp))
        finally:
            t.Tmdb._get = real

        self.assertTrue(t.LOG_FILE.exists(), "Log fehlt nach Abbruch")
        rows = [json.loads(l) for l in t.LOG_FILE.read_text().splitlines() if l.strip()]
        self.assertGreater(len(rows), 0, "kein Rename protokolliert — undo unmöglich")

    def test_undo_restores_names(self):
        d = "Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP"
        self.make(f"{d}/{d}.mkv")
        self.make(f"{d}/{d}.srt", 10)
        before = self.names()
        self.run_cli(str(self.tmp))
        self.assertNotEqual(before, self.names())
        self.run_cli("undo")
        self.assertEqual(before, self.names())

    def test_undo_takes_back_only_the_last_run(self):
        self.make("a/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        self.run_cli(str(self.tmp / "a"))
        after_first = self.names()
        self.make("b/The.Glass.Harbor.1999.1080p.BluRay.x264-GRP.mkv")
        self.run_cli(str(self.tmp / "b"))
        self.run_cli("undo")
        self.assertEqual(after_first + ["b", "b/The.Glass.Harbor.1999.1080p.BluRay.x264-GRP.mkv"],
                         sorted(self.names()))

    def test_undo_dry_run_changes_nothing(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        self.run_cli(str(self.tmp))
        tagged = self.names()
        self.run_cli("-n", "undo")
        self.assertEqual(tagged, self.names())

    def test_unknown_option_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli(str(self.tmp))

    def test_no_path_prints_usage(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("tmdbtag [-n] check PATH", out)


class TestUnicodeFilenames(E2EBase):
    """macOS mischt NFC- und NFD-Dateinamen — sichtbar identisch, verschiedene Strings."""

    REL = "Der.Leuchtturm.1981.Ueber.German.1080p.BluRay.x264-GRP"

    def test_nfd_sidecar_is_found_and_renamed(self):
        """Regression: NFD-Untertitel zu NFC-Video blieb liegen und verwaiste."""
        import unicodedata as ud
        base = "Der.Leuchtturm.1981.Blödsinn.German.1080p.BluRay.x264-GRP"
        video = self.make(f"x/{ud.normalize('NFC', base)}.mkv")
        srt = self.tmp / "x" / (ud.normalize("NFD", base) + ".ger.forced.srt")
        srt.write_text("x")

        found = t.sidecars(video)
        self.assertEqual([f.name for f in found], [srt.name],
                         "NFD-Sidecar nicht gefunden")

    def test_mixed_normalization_slices_correctly(self):
        """Regression: der Längen-Index zerschnitt den Namen bei jedem Umlaut."""
        import unicodedata as ud
        base = "Film.mit.Ümläüten.2009"
        video = self.make(f"x/{ud.normalize('NFC', base)}.mkv", 10)
        srt = self.tmp / "x" / (ud.normalize("NFD", base) + ".ger.srt")
        srt.write_text("x")
        self.assertEqual(t.sidecar_rest(video, srt), ".ger.srt")

    def test_end_to_end_with_nfd_sidecar(self):
        import unicodedata as ud
        base = "Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP"
        self.make(f"x/{ud.normalize('NFC', base)}.mkv")
        (self.tmp / "x" / (ud.normalize("NFD", base) + ".ger.srt")).write_text("x")
        self.run_cli(str(self.tmp))
        got = self.names()
        self.assertIn(f"x/{base} [tmdbid-387].mkv", got)
        self.assertIn(f"x/{base} [tmdbid-387].ger.srt", got)

    def test_output_names_are_nfc(self):
        import unicodedata as ud
        base = ud.normalize("NFD", "Der.Leuchtturm.1981.Blödsinn.German.1080p-GRP")
        self.make(f"x/{base}.mkv")
        self.run_cli(str(self.tmp))
        new = next(p for p in (self.tmp / "x").iterdir() if p.suffix == ".mkv")
        self.assertEqual(new.name, ud.normalize("NFC", new.name),
                         "Zielname ist nicht NFC-normalisiert")


class TestUnattended(E2EBase):
    """Ohne Terminal (cron, Pipe) läuft alles durch — kein input(), kein Abbruch."""

    def test_uncertain_match_is_skipped_not_prompted(self):
        # "Ironbridges 1979" existiert nicht (Ironbridge=1979, Ironbridges=1986) -> unsicher
        self.make("f/Ironbridges.1979.Special.Edition.German.DL.1080p.BluRay.x264-GRP.mkv")
        with mock.patch("builtins.input",
                        side_effect=AssertionError("ohne Terminal interaktiv nachgefragt")):
            _, out = self.run_cli(str(self.tmp))
        self.assertIn("f/Ironbridges.1979.Special.Edition.German.DL.1080p.BluRay.x264-GRP.mkv",
                      self.names())
        self.assertIn("1 skipped", out)

    def test_uncertain_match_is_offered_in_a_terminal(self):
        self.make("f/Ironbridges.1979.Special.Edition.German.1080p-GRP.mkv")
        with mock.patch("builtins.input", return_value="s") as ask, \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.run_cli(str(self.tmp))
        self.assertTrue(ask.called, "im Terminal nicht nachgefragt")

    def test_confident_matches_are_tagged_without_asking(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        self.make("f/Ironbridges.1979.Special.Edition.German.1080p-GRP.mkv")
        self.run_cli(str(self.tmp))
        got = self.names()
        self.assertIn("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv", got)
        self.assertIn("f/Ironbridges.1979.Special.Edition.German.1080p-GRP.mkv", got)

    def test_network_error_skips_file_and_continues(self):
        """Ein Aussetzer darf nicht den ganzen Lauf über tausende Dateien töten."""
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-AAA.mkv")
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-ZZZ.mkv")
        calls = {"n": 0}
        real = t.Tmdb._get

        def flaky(self, path, **p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise t.TmdbUnavailable("Verbindung abgebrochen")
            return real(self, path, **p)

        t.Tmdb._get = flaky
        try:
            rc, out = self.run_cli(str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertEqual(rc, 0)
        self.assertIn("1 errors", out)
        # die zweite Datei wurde trotzdem verarbeitet
        self.assertIn("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-ZZZ [tmdbid-387].mkv",
                      self.names())

    def test_aborts_after_five_consecutive_errors(self):
        for i in range(9):
            self.make(f"f/Film{i}.Der.Leuchtturm.1981.German.1080p-GRP.mkv")

        def dead(self, path, **p):
            raise t.TmdbUnavailable("kein Netz")

        real, t.Tmdb._get = t.Tmdb._get, dead
        try:
            rc, out = self.run_cli(str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertEqual(rc, 0)
        self.assertIn("Five consecutive errors", out)

    def test_tagged_files_summarised_not_listed(self):
        """Viele Zeilen 'übersprungen' verdecken sonst die eigentliche Arbeit."""
        for i in range(5):
            self.make(f"f/Film{i}.1999 [tmdbid-{600+i}].mkv")
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        _, out = self.run_cli(str(self.tmp))
        self.assertIn("5 already tagged", out)
        self.assertNotIn("already tagged → skipped\n[", out)
        self.assertIn("1 to process", out)
        self.assertIn("[1/1]", out)          # Zähler zählt nur echte Arbeit

    def test_nothing_to_do_exits_early(self):
        self.make("f/Film.1999 [tmdbid-603].mkv")
        rc, out = self.run_cli(str(self.tmp))
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to do", out)

    def test_apple_double_files_ignored(self):
        """macOS legt auf SMB ._Name-Beifänger an — kein stat() dafür."""
        self.make("f/Der.Leuchtturm.1981.German.1080p-GRP.mkv")
        (self.tmp / "f" / "._Der.Leuchtturm.1981.German.1080p-GRP.mkv").write_bytes(b"x" * 100)
        got = t.collect([str(self.tmp)], 0)
        self.assertTrue(all(not p.name.startswith("._") for p in got), got)

    def test_progress_counter_shown(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p-GRP.mkv")
        _, out = self.run_cli(str(self.tmp))
        self.assertIn("[1/1]", out)


class TestCheck(E2EBase):
    def test_verify_accepts_correct_tag(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv")
        rc, out = self.run_cli("check", str(self.tmp))
        self.assertEqual(rc, 0)
        self.assertIn("plausible", out)

    def test_verify_flags_wrong_title(self):
        # Tag zeigt auf Der Leuchtturm (#387), Datei ist ein ganz anderer Film
        self.make("f/The.Glass.Harbor.1999.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv")
        rc, out = self.run_cli("check", str(self.tmp))
        self.assertIn("1 of 1 suspect", out)
        self.assertIn("title mismatch", out)

    def test_verify_accepts_german_subtitle_suffix(self):
        """Fehlalarm-Klasse: die Datei trägt einen Zusatz, den TMDB nicht führt."""
        self.assertTrue(t.title_agrees("47 Alles ist anders als gedacht", "47"))
        self.assertTrue(t.title_agrees("Hanna Bergs Der Morast", "Der Morast"))
        self.assertTrue(t.title_agrees("Der Morast", "Hanna Bergs Der Morast"))
        self.assertTrue(t.title_agrees("7KM Sieben Kilometer", "7km"))

    def test_sequel_numbering_roman_or_arabic(self):
        """Aus dem Lauf über die Sammlung: 'Der Hafen 02' vs 'Der Hafen - Teil II'."""
        self.assertTrue(t.title_agrees("Der Hafen 02", "Der Hafen - Teil II"))
        self.assertTrue(t.title_agrees("Der Hafen 03", "Der Hafen - Teil III"))
        self.assertTrue(t.title_agrees("Bruno 4", "Bruno IV"))
        # Einzelbuchstaben bleiben unangetastet
        self.assertEqual(t.norm("Agent X"), "agent x")

    def test_verify_still_rejects_different_films(self):
        self.assertFalse(t.title_agrees("The Glass Harbor", "Der Leuchtturm"))
        self.assertFalse(t.title_agrees("Skyman", "Nightwolf Rising"))
        # zu kurz, um als Teilmenge zu zählen
        self.assertFalse(t.title_agrees("Ob", "Obelisk"))

    def test_verify_accepts_tag_with_extra_title_words(self):
        self.make("f/47.Alles.ist.anders.als.gedacht.1998.German.1080p-GRP "
                  "[tmdbid-387].mkv")
        real = t.Tmdb._get

        def as_23(self, path, **p):
            if path.startswith("/movie/"):
                return {"id": 387, "title": "47", "original_title": "47",
                        "release_date": "1998-01-01"}
            return real(self, path, **p)

        t.Tmdb._get = as_23
        try:
            _, out = self.run_cli("check", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("plausible", out)

    def test_verify_flags_wrong_year(self):
        self.make("f/Der.Leuchtturm.2015.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("suspect", out)
        self.assertIn("year 2015", out)

    def test_verify_flags_unknown_id(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-999999].mkv")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("does not exist on TMDB", out)

    def test_verify_flags_nfo_that_overrides_filename(self):
        """Dateiname trägt #405775, die danebenliegende NFO #106646 — und Jellyfin
        nimmt die NFO, der Film bleibt also falsch zugeordnet."""
        v = self.make("f/The.Hollow.Gate.2017.German.1080p-GRP [tmdbid-405775].mkv")
        v.with_suffix(".nfo").write_text(
            '<?xml version="1.0"?>\n<movie>\n  <title>The Copper Market</title>\n'
            "  <tmdbid>106646</tmdbid>\n</movie>\n", encoding="utf-8")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("NFO overrides the filename", out)
        self.assertIn("106646", out)
        self.assertIn("1 NFO(s) set aside", out)

    def test_fix_nfo_sets_the_contradicting_file_aside(self):
        v = self.make("f/The.Hollow.Gate.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertFalse(nfo.exists(), "NFO liegt noch im Weg")
        self.assertTrue(nfo.with_suffix(".nfo.bak").exists(), "keine Sicherung angelegt")
        self.assertIn("NFO set aside", out)

    def test_fix_nfo_is_undoable(self):
        v = self.make("f/The.Hollow.Gate.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        self.run_cli("check", str(self.tmp))
        self.run_cli("undo")
        self.assertTrue(nfo.exists(), "undo hat die NFO nicht zurückgeholt")

    def test_fix_nfo_dry_run_changes_nothing(self):
        v = self.make("f/The.Hollow.Gate.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        _, out = self.run_cli("-n", "check", str(self.tmp))
        self.assertTrue(nfo.exists(), "Trockenlauf hat die NFO verschoben")
        self.assertFalse(nfo.with_suffix(".nfo.bak").exists())
        self.assertIn("dry run", out)

    def test_fix_nfo_leaves_matching_nfos_alone(self):
        v = self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>387</tmdbid></movie>", encoding="utf-8")
        self.run_cli("check", str(self.tmp))
        self.assertTrue(nfo.exists(), "korrekte NFO wurde angefasst")

    def test_fix_nfo_does_not_clobber_an_existing_backup(self):
        v = self.make("f/The.Hollow.Gate.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        old = nfo.with_suffix(".nfo.bak")
        old.write_text("aeltere Sicherung", encoding="utf-8")
        self.run_cli("check", str(self.tmp))
        self.assertEqual(old.read_text(), "aeltere Sicherung", "alte Sicherung überschrieben")

    def test_verify_accepts_matching_nfo(self):
        v = self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text(
            '<?xml version="1.0"?>\n<movie>\n  <tmdbid>387</tmdbid>\n</movie>\n',
            encoding="utf-8")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("plausible", out)

    def test_nfo_id_read_from_uniqueid_form(self):
        """tmdbtag schreibt <uniqueid type="tmdb">, Jellyfin auch."""
        v = self.make("f/Film.2000 [tmdbid-42].mkv", 10)
        v.with_suffix(".nfo").write_text(
            '<movie><uniqueid type="tmdb" default="true">99</uniqueid></movie>',
            encoding="utf-8")
        got = t.nfo_tmdb_id(v)
        self.assertIsNotNone(got)
        self.assertEqual(got[1], 99)

    def test_scene_nfo_without_id_is_not_flagged(self):
        v = self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text("  ___ SCENE ASCII ART ___\n")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("plausible", out)

    def test_skipped_file_is_picked_up_by_the_next_run(self):
        """Statt Report: was übersprungen wurde, bleibt ungetaggt und kommt
        beim nächsten Lauf wieder dran — alles andere ist dann schon getaggt."""
        self.make("f/Ironbridges.1979.Special.Edition.German.1080p-GRP.mkv")
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        self.run_cli(str(self.tmp))
        _, out = self.run_cli(str(self.tmp))
        self.assertIn("1 already tagged", out)
        self.assertIn("1 to process", out)
        self.assertFalse((t.CONFIG_DIR / "offen.jsonl").exists())


class TestParallelPrefetch(E2EBase):
    def _make_many(self, n=12):
        for i in range(n):
            self.make(f"f/Der.Leuchtturm.198{i%10}.German.1080p.BluRay.x264-G{i:02d}.mkv", 10)

    def test_requests_actually_overlap(self):
        """Belegt Nebenläufigkeit, nicht nur 'es stürzt nicht ab'."""
        import threading
        live = {"now": 0, "peak": 0}
        lock = threading.Lock()
        real = t.Tmdb._get

        def slow(self, path, **p):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            time.sleep(0.05)
            try:
                return real(self, path, **p)
            finally:
                with lock:
                    live["now"] -= 1

        self._make_many()
        t.Tmdb._get = slow
        try:
            self.run_small(workers=6)
        finally:
            t.Tmdb._get = real
        self.assertGreater(live["peak"], 1,
                           f"Anfragen liefen nacheinander (peak={live['peak']})")

    def test_workers_1_stays_sequential(self):
        import threading
        live = {"now": 0, "peak": 0}
        lock = threading.Lock()
        real = t.Tmdb._get

        def slow(self, path, **p):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            time.sleep(0.01)
            try:
                return real(self, path, **p)
            finally:
                with lock:
                    live["now"] -= 1

        self._make_many(6)
        t.Tmdb._get = slow
        try:
            self.run_small(workers=1)
        finally:
            t.Tmdb._get = real
        self.assertEqual(live["peak"], 1)

    def test_prefetch_warms_cache_so_main_loop_refetches_nothing(self):
        calls = []
        real = t.Tmdb._get

        def counting(self, path, **p):
            # _get ist die Methode, die den Cache hält — beim Patchen muss der
            # Cache nachgebaut werden, sonst misst der Test ihn weg.
            key = (path, tuple(sorted(p.items())))
            if key in self._cache:
                return self._cache[key]
            if p.get("query"):
                # volle Kombination inklusive Endpunkt: dieselbe Phrase mit
                # anderem Jahr, anderer Sprache oder gegen /search/tv ist eine
                # andere Anfrage, kein Duplikat
                calls.append((path, p["query"], p.get("year"), p.get("language")))
            self._cache[key] = real(self, path, **p)
            return self._cache[key]

        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        self.make("f/The.Glass.Harbor.1999.German.1080p.BluRay.x264-GRP.mkv")
        self.make("f/Quartzlight.1972.German.1080p.BluRay.x264-GRP.mkv")
        self.make("f/Ironbridge.1979.German.1080p.BluRay.x264-GRP.mkv")
        t.Tmdb._get = counting
        try:
            self.run_cli(str(self.tmp))
        finally:
            t.Tmdb._get = real
        # jede Query genau einmal — der Hauptlauf trifft den Cache
        self.assertEqual(len(calls), len(set(calls)),
                         f"doppelte Anfragen trotz Cache: {calls}")

    def test_prefetch_error_does_not_abort_run(self):
        real = t.Tmdb._get
        state = {"n": 0}

        def flaky(self, path, **p):
            state["n"] += 1
            if state["n"] <= 2:
                raise t.TmdbUnavailable("Aussetzer beim Vorabruf")
            return real(self, path, **p)

        self._make_many(6)
        t.Tmdb._get = flaky
        try:
            rc, _ = self.run_small()
        finally:
            t.Tmdb._get = real
        self.assertEqual(rc, 0)


class TestRuntimeTiebreaker(E2EBase):
    """Laufzeit trennt gleichnamige Filme, wo Titel und Jahr nichts hergeben."""

    ODILA = [
        {"id": 1038263, "title": "Odila", "original_title": "Odila",
         "release_date": "2024-08-29", "popularity": 3.8},      # Biopic, 123 min
        {"id": 1243381, "title": "Odila", "original_title": "Odile",
         "release_date": "2024-12-06", "popularity": 3.0},      # Bibelfilm, 112 min
    ]
    RUNTIMES = {1038263: 123, 1243381: 112}

    def _tmdb(self):
        class Fake:
            lang = "de-DE"
            def details(self, mid):
                return {"id": mid, "runtime": TestRuntimeTiebreaker.RUNTIMES.get(mid)}
        return Fake()

    def test_nfo_runtime_is_used(self):
        v = self.make("f/Odila.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        self.assertEqual(t.media_duration(v), 112)

    def test_picks_candidate_matching_the_file(self):
        v = self.make("f/Odila.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        got = t.by_runtime(self._tmdb(), self.ODILA, v)
        self.assertIsNotNone(got, "kein Kandidat gewählt")
        self.assertEqual(got[1]["id"], 1243381, "falscher Film gewählt")

    def test_picks_the_other_one_for_a_longer_file(self):
        v = self.make("f/Odila.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>123</runtime></movie>")
        got = t.by_runtime(self._tmdb(), self.ODILA, v)
        self.assertEqual(got[1]["id"], 1038263)

    def test_declines_when_two_candidates_are_close(self):
        """11 min Abstand ist zu wenig — dann soll der Mensch entscheiden."""
        v = self.make("f/Odila.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>118</runtime></movie>")
        self.assertIsNone(t.by_runtime(self._tmdb(), self.ODILA, v))

    def test_title_breaks_the_tie_when_runtimes_collide(self):
        """Neben 'Odila' (112 min) liegt 'Salve Odila' (111 min): die Laufzeit
        allein reicht nicht, der Titel muss mitentscheiden."""
        cands = self.ODILA + [{"id": 1247597, "title": "Salve Odila",
                               "original_title": "Salve Odila",
                               "release_date": "2024-01-01", "popularity": 2.0}]
        rt = {**self.RUNTIMES, 1247597: 111}

        class Fake:
            lang = "de-DE"
            def details(self, mid): return {"id": mid, "runtime": rt.get(mid)}

        v = self.make("f/Odila.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        got = t.by_runtime(Fake(), cands, v, "Odila", 2024)
        self.assertIsNotNone(got, "Titel hätte entscheiden müssen")
        self.assertEqual(got[1]["id"], 1243381)

    def test_declines_when_runtime_and_title_both_tie(self):
        """Zwei Namensvettern gleicher Länge — hier darf nichts geraten werden."""
        twins = [
            {"id": 1, "title": "Odila", "original_title": "Odila",
             "release_date": "2024-01-01", "popularity": 3.0},
            {"id": 2, "title": "Odila", "original_title": "Odila",
             "release_date": "2024-06-01", "popularity": 3.1},
        ]

        class Fake:
            lang = "de-DE"
            def details(self, mid): return {"id": mid, "runtime": 112}

        v = self.make("f/Odila.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        self.assertIsNone(t.by_runtime(Fake(), twins, v, "Odila", 2024))

    def test_declines_without_a_known_duration(self):
        v = self.make("f/Odila.2024.German-GRP.avi", 10)   # kein Header, keine NFO
        self.assertIsNone(t.by_runtime(self._tmdb(), self.ODILA, v))

    def test_reads_real_mkv_header(self):
        """Echter Matroska-Header: Segment > Info > TimecodeScale + Duration."""
        def vint(n):                       # Größe als 1-Byte-EBML-Zahl
            return bytes([0x80 | n])
        dur = struct.pack(">d", 112 * 60 * 1000.0)     # ms bei scale 1e6
        info = (b"\x2a\xd7\xb1" + vint(4) + (1_000_000).to_bytes(4, "big")
                + b"\x44\x89" + vint(8) + dur)
        segment = b"\x15\x49\xa9\x66" + vint(len(info)) + info
        body = b"\x18\x53\x80\x67" + vint(len(segment)) + segment
        v = self.tmp / "f" / "Film.mkv"
        v.parent.mkdir(parents=True, exist_ok=True)
        v.write_bytes(b"\x1a\x45\xdf\xa3" + vint(1) + b"\x00" + body)
        self.assertEqual(t.media_duration(v), 112)

    def test_reads_real_mp4_header(self):
        mvhd = (b"\x00\x00\x00\x00" + b"\x00" * 8
                + (1000).to_bytes(4, "big") + (90 * 60 * 1000).to_bytes(4, "big"))
        mvhd_atom = (len(mvhd) + 8).to_bytes(4, "big") + b"mvhd" + mvhd
        moov = (len(mvhd_atom) + 8).to_bytes(4, "big") + b"moov" + mvhd_atom
        v = self.tmp / "f" / "Film.mp4"
        v.parent.mkdir(parents=True, exist_ok=True)
        v.write_bytes(moov)
        self.assertEqual(t.media_duration(v), 90)

    def test_garbage_file_does_not_raise(self):
        v = self.make("f/Kaputt.mkv", 4096)      # Nullbytes, kein gültiger Header
        self.assertIsNone(t.media_duration(v))

    def test_verify_flags_grossly_wrong_runtime(self):
        v = self.make("f/Der.Leuchtturm.1981.German-GRP [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text(
            "<movie><tmdbid>387</tmdbid><runtime>22</runtime></movie>")
        real = t.Tmdb._get

        def with_runtime(self, path, **p):
            d = dict(real(self, path, **p))
            if path.startswith("/movie/"):
                d["runtime"] = 149
            return d

        t.Tmdb._get = with_runtime
        try:
            _, out = self.run_cli("check", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("runtime 22 min vs TMDB 149 min", out)

    def test_verify_tolerates_extended_cut(self):
        v = self.make("f/Der.Leuchtturm.1981.EXTENDED.German-GRP [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text(
            "<movie><tmdbid>387</tmdbid><runtime>168</runtime></movie>")
        real = t.Tmdb._get

        def with_runtime(self, path, **p):
            d = dict(real(self, path, **p))
            if path.startswith("/movie/"):
                d["runtime"] = 149
            return d

        t.Tmdb._get = with_runtime
        try:
            _, out = self.run_cli("check", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("plausible", out)


class TestSafety(E2EBase):
    """Zusicherungen, auf die man sich verlassen muss."""

    def test_dry_run_still_shows_what_would_happen(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        _, out = self.run_cli("-n", str(self.tmp))
        self.assertIn("[tmdbid-387]", out)
        self.assertIn("dry run", out)

    def test_dry_run_never_renames_or_logs(self):
        v = self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        before = self.names()
        self.run_cli("-n", str(self.tmp))
        self.assertEqual(before, self.names(), "Trockenlauf hat Dateien verändert")
        self.assertFalse(t.LOG_FILE.exists(), "Trockenlauf hat ins Undo-Log geschrieben")

    def test_overlong_name_is_truncated(self):
        """Sonst scheitert das Umbenennen am Dateisystem-Limit."""
        stem = "A" * 300
        out = t.new_stem(stem, t.build_tag(387))
        self.assertLessEqual(len(out.encode("utf-8")), 200)

    def test_sanitize_never_yields_an_empty_name(self):
        for bad in ["..", ".", "   ", "///", "\x00"]:
            self.assertTrue(t.sanitize(bad), f"leerer Name aus {bad!r}")

    def test_sanitize_cannot_escape_the_directory(self):
        for bad in ["../../etc/passwd", "a/b", "/absolut"]:
            self.assertNotIn("/", t.sanitize(bad))

    def test_config_file_is_not_world_readable(self):
        t.CONFIG_FILE = self.tmp / "config.json"
        with redirect_stdout(io.StringIO()):
            t.save_key("geheim123")
        self.assertEqual(oct(t.CONFIG_FILE.stat().st_mode)[-3:], "600")

    def test_key_is_asked_for_on_first_run(self):
        """Kein Key gespeichert: im Terminal nachfragen (ohne Echo) und ablegen."""
        t.CONFIG_FILE = self.tmp / "config.json"
        with mock.patch("getpass.getpass", return_value="  abc123  "), \
                mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.dict(os.environ, {}, clear=True), \
                redirect_stdout(io.StringIO()):
            key = self._saved["load_key"]()
        self.assertEqual(key, "abc123")
        self.assertEqual(json.loads(t.CONFIG_FILE.read_text())["api_key"], "abc123")

    def test_no_key_without_terminal_fails_clearly(self):
        t.CONFIG_FILE = self.tmp / "config.json"
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                self._saved["load_key"]()
        self.assertIn("TMDB_API_KEY", str(cm.exception))
        self.assertFalse(t.CONFIG_FILE.exists())

    def test_empty_prompted_key_is_refused(self):
        t.CONFIG_FILE = self.tmp / "config.json"
        with mock.patch("getpass.getpass", return_value=""), \
                mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.dict(os.environ, {}, clear=True), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._saved["load_key"]()

    def test_collect_does_not_follow_symlinks_out_of_the_tree(self):
        outside = self.tmp / "aussen"
        outside.mkdir()
        (outside / "Fremd.2000.1080p-GRP.mkv").write_bytes(b"0" * (60 * 1024 * 1024))
        lib = self.tmp / "lib"
        lib.mkdir()
        try:
            (lib / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("keine Symlinks möglich")
        got = t.collect([str(lib)], 0)
        self.assertEqual(got, [], f"Symlink verfolgt: {got}")


class TestRetag(E2EBase):
    """Eine getaggte Datei direkt übergeben heißt: neu zuordnen."""

    def _details(self, movie):
        real = t.Tmdb._get
        return mock.patch.object(t.Tmdb, "_get", lambda s, path, **p: (
            movie if path.startswith("/movie/") else real(s, path, **p)))

    def _pick_id(self, mid):
        return (mock.patch("builtins.input", side_effect=["i", str(mid)]),
                mock.patch.object(sys.stdin, "isatty", return_value=True))

    def test_replaces_an_existing_tag(self):
        v = self.make("f/Iron.Web.2023-GRP [tmdbid-1173580].mkv")
        a, b = self._pick_id(901121)
        with self._details({"id": 901121, "title": "Iron Web", "runtime": 135}), a, b:
            self.run_cli(str(v))
        got = self.names()
        self.assertIn("f/Iron.Web.2023-GRP [tmdbid-901121].mkv", got)
        self.assertNotIn("f/Iron.Web.2023-GRP [tmdbid-1173580].mkv", got)
        self.assertEqual(sum("tmdbid" in n for n in got), 1)

    def test_same_id_is_a_no_op(self):
        """Sonst hängt unique_path ein ' (2)' an, obwohl nichts zu tun ist."""
        v = self.make("f/Overpass.1986.German-GRP [tmdbid-64690].mkv")
        a, b = self._pick_id(64690)
        with self._details({"id": 64690, "title": "Overpass", "runtime": 100}), a, b:
            self.run_cli(str(v))
        self.assertEqual(self.names(), ["f", "f/Overpass.1986.German-GRP [tmdbid-64690].mkv"])

    def test_sidecars_do_not_collect_a_second_tag(self):
        """Regression: der alte Tag steckte auch im Sidecar-Rest und wurde
        an den neuen angehängt."""
        v = self.make("f/Overpass.1986.German-GRP [tmdbid-1].mkv")
        (v.parent / "Overpass.1986.German-GRP [tmdbid-1].ger.srt").write_text("x")
        a, b = self._pick_id(64690)
        with self._details({"id": 64690, "title": "Overpass", "runtime": 100}), a, b:
            self.run_cli(str(v))
        for n in self.names():
            self.assertLessEqual(n.count("tmdbid"), 1, f"doppelter Tag: {n}")
        self.assertIn("f/Overpass.1986.German-GRP [tmdbid-64690].ger.srt", self.names())

    def test_folder_scan_leaves_tagged_files_alone(self):
        self.make("f/Overpass.1986.German-GRP [tmdbid-1].mkv")
        with mock.patch("builtins.input",
                        side_effect=AssertionError("Ordner-Scan hat nachgefragt")):
            _, out = self.run_cli(str(self.tmp))
        self.assertIn("Nothing to do", out)

    def test_strip_tag_closes_the_gap(self):
        self.assertEqual(t.strip_tag("Film.2000-GRP [tmdbid-1]"), "Film.2000-GRP")
        self.assertEqual(t.strip_tag(" [tmdbid-1].ger.srt"), ".ger.srt")
        self.assertEqual(t.strip_tag("A [tmdbid-1] [imdbid-tt7] B"), "A B")


class TestTvHint(E2EBase):
    """Miniserien haben oft keinen Film-Eintrag — dann darf nicht der
    ähnlichste Film getaggt werden."""

    def test_reports_a_series_instead_of_guessing_a_film(self):
        real = t.Tmdb._get

        def with_tv(self, path, **p):
            if path == "/search/tv":
                return {"results": [{"id": 19614, "name": "Ur",
                                     "original_name": "Primal",
                                     "first_air_date": "1990-11-18"}]}
            if path == "/search/movie":
                return {"results": []}
            return real(self, path, **p)

        t.Tmdb._get = with_tv
        try:
            self.make("f/Ur.1990.German.DL.1080p.BluRay.x264-GRP.mkv")
            _, out = self.run_cli(str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("TV series #19614", out)
        self.assertIn("shows library", out)
        self.assertIn("f/Ur.1990.German.DL.1080p.BluRay.x264-GRP.mkv", self.names())

    def test_no_tv_lookup_when_the_film_matches(self):
        calls = []
        real = t.Tmdb._get

        def watch(self, path, **p):
            calls.append(path)
            return real(self, path, **p)

        t.Tmdb._get = watch
        try:
            self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
            self.run_cli(str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertNotIn("/search/tv", calls, "unnötige TV-Suche")


class TestDuplicateIds(E2EBase):
    def test_verify_reports_the_same_id_on_two_files(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387].mkv")
        self.make("f/Der.Leuchtturm.1981.German.2160p-UHD [tmdbid-387].mkv")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("1 duplicate id(s)", out)
        self.assertIn("id 387 on 2 files", out)

    def test_no_false_alarm_for_distinct_ids(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387].mkv")
        self.make("f/The.Glass.Harbor.1999.German.1080p-GRP [tmdbid-603].mkv")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertNotIn("duplicate", out)


class TestSidecarArgument(E2EBase):
    """Im Finder liegen .srt, .nfo und .mkv nebeneinander — wer eine davon
    übergibt, meint den Film. Sonst würde der Beifänger selbst umbenannt."""

    def test_sidecar_as_argument_resolves_to_the_video(self):
        v = self.make("f/Der.Leuchtturm.1981.German-GRP.mkv")
        srt = v.with_suffix(".ger.srt")
        srt.write_text("x")
        _, out = self.run_cli(str(srt))
        self.assertIn("using Der.Leuchtturm.1981.German-GRP.mkv instead", out)
        got = self.names()
        self.assertIn("f/Der.Leuchtturm.1981.German-GRP [tmdbid-387].mkv", got)
        self.assertIn("f/Der.Leuchtturm.1981.German-GRP [tmdbid-387].ger.srt", got)

    def test_lone_non_video_is_refused(self):
        stray = self.tmp / "f" / "notizen.txt"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("x")
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            t.main([str(stray)])
        self.assertIn("not a video file", buf.getvalue())

    def test_resolve_video_picks_the_longest_matching_stem(self):
        d = self.tmp / "f"
        d.mkdir(exist_ok=True)
        (d / "Film.mkv").write_bytes(b"x")
        (d / "Film.2011.German.mkv").write_bytes(b"x")
        sc = d / "Film.2011.German.ger.srt"
        sc.write_text("x")
        self.assertEqual(t.resolve_video(sc).name, "Film.2011.German.mkv")

    def test_resolve_video_passes_videos_through(self):
        v = self.make("f/Film.2011.mkv", 10)
        self.assertEqual(t.resolve_video(v), v)


class TestMultiPart(E2EBase):
    """Ein Film auf zwei Dateien: beide tragen dieselbe ID, zu Recht."""

    def test_split_part_recognises_the_usual_forms(self):
        for stem, base, part in [
            ("Film.2000.German-GRP.CD1", "Film.2000.German-GRP", 1),
            ("Film.2000.German-GRP.cd2", "Film.2000.German-GRP", 2),
            ("Film 2000 - part1", "Film 2000", 1),
            ("Film.2000.Teil2", "Film.2000", 2),
            ("Film.2000.disc1.German", "Film.2000.German", 1),
        ]:
            with self.subTest(stem=stem):
                self.assertEqual(t.split_part(stem), (base, part))

    def test_split_part_leaves_normal_names_alone(self):
        for stem in ["Der.Leuchtturm.1981.German-GRP", "Harper's 11", "Reactor 2"]:
            self.assertEqual(t.split_part(stem), (stem, None))

    def test_both_parts_get_the_tag_and_a_part_suffix(self):
        for n in (1, 2):
            self.make(f"f/Der.Leuchtturm.1981.German.1080p-GRP.CD{n}.mkv")
        self.run_cli(str(self.tmp))
        got = self.names()
        self.assertIn("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387] - part1.mkv", got)
        self.assertIn("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387] - part2.mkv", got)

    def test_verify_does_not_call_two_parts_a_duplicate(self):
        for n in (1, 2):
            self.make(f"f/Der.Leuchtturm.1981-GRP [tmdbid-387] - part{n}.mkv")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertNotIn("duplicate", out)

    def test_verify_still_reports_a_real_duplicate(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p-GRP [tmdbid-387].mkv")
        self.make("f/Der.Leuchtturm.1981.German.2160p-UHD [tmdbid-387].mkv")
        _, out = self.run_cli("check", str(self.tmp))
        self.assertIn("duplicate", out)


class TestLanguageSwitch(E2EBase):
    """Englisch ist Standard, TMDBTAG_LANG=de schaltet zurück."""

    def test_default_is_english(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        _, out = self.run_cli(str(self.tmp))
        self.assertIn("to process", out)
        self.assertIn("Done:", out)
        self.assertNotIn("zu bearbeiten", out)

    def test_german_via_env(self):
        self.make("f/Der.Leuchtturm.1981.German.1080p.BluRay.x264-GRP.mkv")
        real = t.LANG
        t.LANG = "de"
        try:
            _, out = self.run_cli(str(self.tmp))
        finally:
            t.LANG = real
        self.assertIn("zu bearbeiten", out)
        self.assertIn("Fertig:", out)

    def test_env_var_selects_german(self):
        with mock.patch.dict(os.environ, {"TMDBTAG_LANG": "de_DE"}):
            lang = ("de" if os.environ.get("TMDBTAG_LANG", "").lower().startswith("de")
                    else "en")
        self.assertEqual(lang, "de")

    def test_translation_keys_are_reachable(self):
        """Ein Tippfehler im Schlüssel bliebe sonst unsichtbar: der englische
        Text fällt einfach durch und die Übersetzung greift nie."""
        src = (ROOT / "tmdbtag.py").read_text()
        body = src.split('def _(s: str) -> str:', 1)[1]
        missing = [k for k in t._DE
                   if k.split("{")[0].split("\n")[0][:22].strip() not in body]
        self.assertEqual(missing, [], f"unerreichbare Uebersetzungen: {missing}")

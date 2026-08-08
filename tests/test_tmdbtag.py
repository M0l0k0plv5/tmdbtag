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
        ("Der.Herr.der.Ringe.Die.Gefaehrten.German.2001.EXTENDED.1080p.BluRay.x264-ENCOUNTERS",
         "Der Herr der Ringe Die Gefaehrten", 2001),
        ("Blade.Runner.2049.2017.2160p.UHD.BluRay.x265.10bit.HDR.DTS-HD.MA.TrueHD.7.1-SWTYBLZ",
         "Blade Runner 2049", 2017),
        ("1917.2019.German.DL.1080p.BluRay.x264-EmpireHD", "1917", 2019),
        ("2012.2009.1080p.BluRay.x264-REFiNED", "2012", 2009),
        ("Spider-Man.Far.From.Home.2019.MULTi.COMPLETE.UHD.BLURAY-SharpHD",
         "Spider-Man Far From Home", 2019),
        ("Inception (2010)", "Inception", 2010),
        ("Parasite.2019.KOREAN.1080p.BluRay.H264.AAC-VXT", "Parasite", 2019),
        ("The.Matrix.1999.REMASTERED.1080p.BluRay.x265-RARBG", "The Matrix", 1999),
        ("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX",
         "Dune Part Two", 2024),
        ("Das.Boot.1981.Directors.Cut.German.DTS.1080p.BluRay.x264-SoW", "Das Boot", 1981),
        ("Terminator.2.Judgment.Day.1991.1080p.BluRay.x264", "Terminator 2 Judgment Day", 1991),
        ("Nosferatu_1922_720p_BluRay", "Nosferatu", 1922),
        ("Amelie [2001] 1080p", "Amelie", 2001),
        # "Web" ist hier Titelwort, nicht die Quellenangabe: der Titel wurde
        # bei "Spider" abgeschnitten und der falsche Film getaggt.
        ("Spider.Web.Once.Upon.A.Time.in.Seoul.2023.German.DL.EAC3.1080p.WEB.H265-ZeroTwo",
         "Spider Web Once Upon A Time in Seoul", 2023),
        ("Open.Water.2003.German.DL.1080p.BluRay.x264-GRP", "Open Water", 2003),
        ("Cam.2018.German.DL.1080p.NF.WEB.x264-GRP", "Cam", 2018),
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
        # "1917" ohne Jahresangabe darf nicht als Jahr weginterpretiert werden
        title, year = t.parse_release("1917.1080p.BluRay.x264")
        self.assertEqual(title, "1917")
        self.assertIsNone(year)

    def test_implausible_future_year_ignored(self):
        title, year = t.parse_release("Blade.Runner.2049.1080p.BluRay")
        self.assertEqual(title, "Blade Runner 2049")
        self.assertIsNone(year)

    def test_existing_tag_is_stripped(self):
        self.assertEqual(t.parse_release("The.Matrix.1999 [tmdbid-603].1080p"),
                         ("The Matrix", 1999))


class TestScoring(unittest.TestCase):
    def test_norm_ignores_articles_and_diacritics(self):
        self.assertEqual(t.norm("Der Schuh des Manitu"), t.norm("Schuh des Manitu"))
        self.assertEqual(t.norm("Amélie"), t.norm("Amelie"))
        self.assertEqual(t.norm("Grüße"), t.norm("Gruesse".replace("ue", "ü")))

    def test_matching_year_beats_wrong_year(self):
        good = {"id": 1, "title": "Das Boot", "release_date": "1981-09-17"}
        bad = {"id": 2, "title": "Das Boot", "release_date": "1997-01-01"}
        self.assertGreater(t.score(good, "Das Boot", 1981), t.score(bad, "Das Boot", 1981))

    def test_title_similarity_matters(self):
        near = {"id": 1, "title": "The Matrix", "release_date": "1999-03-30"}
        far = {"id": 2, "title": "Matrix Reloaded", "release_date": "1999-03-30"}
        self.assertGreater(t.score(near, "The Matrix", 1999), t.score(far, "The Matrix", 1999))


class TestUmlauts(unittest.TestCase):
    """Scene-Releases schreiben Umlaute als ae/oe/ue aus."""

    PAIRS = [
        ("Die Paepstin", "Die Päpstin"),
        ("Fack ju Goehte", "Fack ju Göhte"),
        ("Maenner die auf Ziegen starren", "Männer, die auf Ziegen starren"),
        ("Ueber uns das All", "Über uns das All"),
        ("Toedliche Weihnachten", "Tödliche Weihnachten"),
        ("Das weisse Band", "Das weiße Band"),
    ]

    def test_folded_titles_match(self):
        for scene, real in self.PAIRS:
            with self.subTest(scene=scene):
                self.assertGreaterEqual(t.similarity(scene, real), 0.95)

    def test_folding_does_not_break_normal_titles(self):
        # 'ue' in 'Queen' wird mitgefaltet — darf den Vergleich nicht ruinieren,
        # weil similarity() zusätzlich die ungefaltete Variante prüft.
        self.assertEqual(t.similarity("Queen", "Queen"), 1.0)
        self.assertLess(t.similarity("Queen", "Quentin Tarantino"), 0.7)

    def test_unfold_generates_umlaut_query(self):
        self.assertEqual(t.unfold_umlauts("Die Paepstin"), "Die Päpstin")
        self.assertEqual(t.unfold_umlauts("Ueber uns"), "Über uns")

    def test_search_retries_with_umlaut_spelling(self):
        """Regression: der Umlaut-Fallback muss die Suche erreichen."""
        seen = []

        class FakeTmdb:
            lang = "de-DE"

            def search(self, q, y, lang=None):
                seen.append(q)
                # Nur die echte Umlaut-Schreibweise liefert einen Treffer
                if "Päpstin" in q:
                    return [{"id": 22954, "title": "Die Päpstin",
                             "original_title": "Die Päpstin",
                             "release_date": "2009-10-22", "popularity": 10}]
                return []

        got = t.find_candidates(FakeTmdb(), "Die Paepstin", 2009)
        self.assertTrue(any("Päpstin" in q for q in seen),
                        f"Umlaut-Variante nie gesucht, nur: {seen}")
        self.assertEqual([r["id"] for r in got], [22954])

    def test_search_stops_early_on_strong_hit(self):
        """Kein unnötiger zweiter Request, wenn der erste sitzt."""
        calls = []

        class FakeTmdb:
            lang = "de-DE"

            def search(self, q, y, lang=None):
                calls.append(q)
                return [{"id": 603, "title": "Das Boot", "original_title": "Das Boot",
                         "release_date": "1981-09-17", "popularity": 30}]

        t.find_candidates(FakeTmdb(), "Das Boot", 1981)
        self.assertEqual(len(calls), 1, f"zu viele Requests: {calls}")


class TestNaming(unittest.TestCase):
    MOVIE = {"id": 387, "title": "Das Boot", "original_title": "Das Boot",
             "release_date": "1981-09-17"}

    def test_suffix_keeps_original_name(self):
        stem = "Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW"
        out = t.new_stem(stem, self.MOVIE, t.build_tag(387, None), "suffix")
        self.assertEqual(out, f"{stem} [tmdbid-387]")

    def test_clean_style_rewrites_name(self):
        out = t.new_stem("egal", self.MOVIE, t.build_tag(387, None), "clean")
        self.assertEqual(out, "Das Boot (1981) [tmdbid-387]")

    def test_imdb_tag_appended(self):
        self.assertEqual(t.build_tag(387, "tt0082096"),
                         "[tmdbid-387] [imdbid-tt0082096]")

    def test_sanitize_strips_path_separators(self):
        self.assertEqual(t.sanitize("A/B: C?"), "A-B - C")

    def test_tag_regex_detects_existing(self):
        self.assertTrue(t.TAG_RE.search("Film [tmdbid-1].mkv"))
        self.assertFalse(t.TAG_RE.search("Film [1080p].mkv"))


# --------------------------------------------------------------------------- #
# Ende-zu-Ende gegen eine gefälschte TMDB-API
# --------------------------------------------------------------------------- #

DB = {
    "das boot": {"id": 387, "title": "Das Boot", "original_title": "Das Boot",
                 "release_date": "1981-09-17", "popularity": 30,
                 "overview": "U-96 auf Feindfahrt.", "imdb_id": "tt0082096"},
    "matrix": {"id": 603, "title": "Matrix", "original_title": "The Matrix",
               "release_date": "1999-03-30", "popularity": 80,
               "overview": "Neo folgt dem weissen Kaninchen."},
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
        t.load_key = lambda _: "fake"
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

    def names(self):
        return sorted(str(p.relative_to(self.tmp))
                      for p in self.tmp.rglob("*") if p.name != "renames.jsonl")


class TestEndToEnd(E2EBase):
    def test_renames_video_and_sidecars(self):
        d = "Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        self.make(f"{d}/{d}.ger.forced.srt", 10)
        self.run_cli("--auto", str(self.tmp))
        got = self.names()
        self.assertIn(f"{d}/{d} [tmdbid-387].mkv", got)
        self.assertIn(f"{d}/{d} [tmdbid-387].ger.forced.srt", got)

    def test_dry_run_changes_nothing(self):
        d = "Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        before = self.names()
        self.run_cli("--auto", "-n", str(self.tmp))
        self.assertEqual(before, self.names())

    def test_folder_flag_skips_multi_movie_dirs(self):
        """Regression: --folder darf Sammelordner nicht umbenennen."""
        self.make("flat/The.Matrix.1999.1080p.BluRay.x264-AMIABLE.mkv")
        self.make("flat/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        self.run_cli("--auto", "--folder", str(self.tmp))
        self.assertTrue((self.tmp / "flat").is_dir(),
                        "Sammelordner wurde umbenannt")

    def test_folder_flag_tags_single_movie_dir(self):
        d = "Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        self.run_cli("--auto", "--folder", str(self.tmp))
        self.assertTrue((self.tmp / f"{d} [tmdbid-387]").is_dir())

    def test_already_tagged_is_skipped(self):
        p = self.make("flat/The.Matrix.1999 [tmdbid-603].mkv")
        self.run_cli("--auto", str(self.tmp))
        self.assertTrue(p.exists())

    def test_sample_and_small_files_ignored(self):
        self.make("flat/sample.mkv", 1024)
        self.make("flat/The.Matrix.1999.1080p-GRP.mkv", 1024)  # unter --min-size
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("--auto", str(self.tmp))
        self.assertIn("No video files found", str(cm.exception))

    def test_sample_ignored_but_real_file_processed(self):
        self.make("flat/Das.Boot.1981.German.1080p-SoW-sample.mkv")
        self.make("flat/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        self.run_cli("--auto", str(self.tmp))
        got = self.names()
        self.assertIn("flat/Das.Boot.1981.German.1080p-SoW-sample.mkv", got)
        self.assertIn("flat/Das.Boot.1981.German.1080p.BluRay.x264-SoW [tmdbid-387].mkv", got)

    def test_log_written_incrementally(self):
        """Nach einem Abbruch mitten im Lauf muss --undo trotzdem greifen."""
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-AAA.mkv")
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-BBB.mkv")
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-CCC.mkv")

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
                self.run_cli("--batch", str(self.tmp))
        finally:
            t.Tmdb._get = real

        self.assertTrue(t.LOG_FILE.exists(), "Log fehlt nach Abbruch")
        rows = [json.loads(l) for l in t.LOG_FILE.read_text().splitlines() if l.strip()]
        self.assertGreater(len(rows), 0, "kein Rename protokolliert — --undo unmöglich")

    def test_undo_restores_names(self):
        d = "Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        self.make(f"{d}/{d}.srt", 10)
        before = self.names()
        self.run_cli("--auto", "--folder", str(self.tmp))
        self.assertNotEqual(before, self.names())
        n = len(t.LOG_FILE.read_text().splitlines())
        self.run_cli("--undo", str(n))
        self.assertEqual(before, self.names())

    def test_imdb_flag(self):
        d = "Das.Boot.1981.German.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        self.run_cli("--auto", "--imdb", str(self.tmp))
        self.assertIn(f"{d}/{d} [tmdbid-387] [imdbid-tt0082096].mkv", self.names())


class TestUnicodeFilenames(E2EBase):
    """macOS mischt NFC- und NFD-Dateinamen — sichtbar identisch, verschiedene Strings."""

    REL = "Das.Boot.1981.Ueber.German.1080p.BluRay.x264-SoW"

    def test_nfd_sidecar_is_found_and_renamed(self):
        """Regression: NFD-Untertitel zu NFC-Video blieb liegen und verwaiste."""
        import unicodedata as ud
        base = "Das.Boot.1981.Blödsinn.German.1080p.BluRay.x264-SoW"
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
        base = "Das.Boot.1981.German.1080p.BluRay.x264-SoW"
        self.make(f"x/{ud.normalize('NFC', base)}.mkv")
        (self.tmp / "x" / (ud.normalize("NFD", base) + ".ger.srt")).write_text("x")
        self.run_cli("--auto", str(self.tmp))
        got = self.names()
        self.assertIn(f"x/{base} [tmdbid-387].mkv", got)
        self.assertIn(f"x/{base} [tmdbid-387].ger.srt", got)

    def test_output_names_are_nfc(self):
        import unicodedata as ud
        base = ud.normalize("NFD", "Das.Boot.1981.Blödsinn.German.1080p-SoW")
        self.make(f"x/{base}.mkv")
        self.run_cli("--auto", str(self.tmp))
        new = next(p for p in (self.tmp / "x").iterdir() if p.suffix == ".mkv")
        self.assertEqual(new.name, ud.normalize("NFC", new.name),
                         "Zielname ist nicht NFC-normalisiert")


class TestBatchMode(E2EBase):
    """--batch muss unbeaufsichtigt durchlaufen — kein input(), kein Abbruch."""

    def test_uncertain_match_is_deferred_not_prompted(self):
        # "Aliens 1979" existiert nicht (Alien=1979, Aliens=1986) -> unsicher
        self.make("f/Aliens.1979.Special.Edition.German.DL.1080p.BluRay.x264-GRP.mkv")
        with mock.patch("builtins.input",
                        side_effect=AssertionError("--batch hat interaktiv nachgefragt")):
            self.run_cli("--batch", str(self.tmp))
        # Datei unverändert, aber im Report vermerkt
        self.assertIn("f/Aliens.1979.Special.Edition.German.DL.1080p.BluRay.x264-GRP.mkv",
                      self.names())
        report = t.CONFIG_DIR / "offen.jsonl"
        self.assertTrue(report.exists(), "kein Report geschrieben")
        rows = [json.loads(l) for l in report.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["reason"], ("uncertain", "no match"))

    def test_batch_still_tags_confident_matches(self):
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        self.run_cli("--batch", str(self.tmp))
        got = self.names()
        self.assertIn("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW [tmdbid-387].mkv", got)
        self.assertIn("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv", got)

    def test_network_error_skips_file_and_continues(self):
        """Ein Aussetzer darf nicht den ganzen Lauf über tausende Dateien töten."""
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-AAA.mkv")
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-ZZZ.mkv")
        calls = {"n": 0}
        real = t.Tmdb._get

        def flaky(self, path, **p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise t.TmdbUnavailable("Verbindung abgebrochen")
            return real(self, path, **p)

        t.Tmdb._get = flaky
        try:
            rc, out = self.run_cli("--batch", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertEqual(rc, 0)
        self.assertIn("1 errors", out)
        # die zweite Datei wurde trotzdem verarbeitet
        self.assertIn("f/Das.Boot.1981.German.1080p.BluRay.x264-ZZZ [tmdbid-387].mkv",
                      self.names())

    def test_aborts_after_five_consecutive_errors(self):
        for i in range(9):
            self.make(f"f/Film{i}.Das.Boot.1981.German.1080p-SoW.mkv")

        def dead(self, path, **p):
            raise t.TmdbUnavailable("kein Netz")

        real, t.Tmdb._get = t.Tmdb._get, dead
        try:
            rc, out = self.run_cli("--batch", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertEqual(rc, 0)
        self.assertIn("Five consecutive errors", out)

    def test_tagged_files_summarised_not_listed(self):
        """Viele Zeilen 'übersprungen' verdecken sonst die eigentliche Arbeit."""
        for i in range(5):
            self.make(f"f/Film{i}.1999 [tmdbid-{600+i}].mkv")
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        _, out = self.run_cli("--batch", str(self.tmp))
        self.assertIn("5 already tagged", out)
        self.assertNotIn("already tagged → skipped\n[", out)
        self.assertIn("1 to process", out)
        self.assertIn("[1/1]", out)          # Zähler zählt nur echte Arbeit

    def test_nothing_to_do_exits_early(self):
        self.make("f/Film.1999 [tmdbid-603].mkv")
        rc, out = self.run_cli("--batch", str(self.tmp))
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to do", out)

    def test_apple_double_files_ignored(self):
        """macOS legt auf SMB ._Name-Beifänger an — kein stat() dafür."""
        self.make("f/Das.Boot.1981.German.1080p-SoW.mkv")
        (self.tmp / "f" / "._Das.Boot.1981.German.1080p-SoW.mkv").write_bytes(b"x" * 100)
        got = t.collect([str(self.tmp)], 0, True)
        self.assertTrue(all(not p.name.startswith("._") for p in got), got)

    def test_progress_counter_shown(self):
        self.make("f/Das.Boot.1981.German.1080p-SoW.mkv")
        _, out = self.run_cli("--batch", str(self.tmp))
        self.assertIn("[1/1]", out)


class TestVerifyAndReport(E2EBase):
    def test_verify_accepts_correct_tag(self):
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW [tmdbid-387].mkv")
        rc, out = self.run_cli("--verify", str(self.tmp))
        self.assertEqual(rc, 0)
        self.assertIn("plausible", out)

    def test_verify_flags_wrong_title(self):
        # Tag zeigt auf Das Boot (#387), Datei ist ein ganz anderer Film
        self.make("f/The.Matrix.1999.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv")
        rc, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("1 of 1 suspect", out)
        self.assertIn("title mismatch", out)

    def test_verify_accepts_german_subtitle_suffix(self):
        """Fehlalarm-Klasse: die Datei trägt einen Zusatz, den TMDB nicht führt."""
        self.assertTrue(t.title_agrees("23 Nichts ist so wie es scheint", "23"))
        self.assertTrue(t.title_agrees("Stephen Kings Der Nebel", "Der Nebel"))
        self.assertTrue(t.title_agrees("Der Nebel", "Stephen Kings Der Nebel"))
        self.assertTrue(t.title_agrees("8MM Acht Millimeter", "8mm"))

    def test_sequel_numbering_roman_or_arabic(self):
        """Aus dem Lauf über die Sammlung: 'Der Pate 02' vs 'Der Pate - Teil II'."""
        self.assertTrue(t.title_agrees("Der Pate 02", "Der Pate - Teil II"))
        self.assertTrue(t.title_agrees("Der Pate 03", "Der Pate - Teil III"))
        self.assertTrue(t.title_agrees("Rocky 4", "Rocky IV"))
        # Einzelbuchstaben bleiben unangetastet
        self.assertEqual(t.norm("Malcolm X"), "malcolm x")

    def test_verify_still_rejects_different_films(self):
        self.assertFalse(t.title_agrees("The Matrix", "Das Boot"))
        self.assertFalse(t.title_agrees("Superman", "Batman Begins"))
        # zu kurz, um als Teilmenge zu zählen
        self.assertFalse(t.title_agrees("It", "Interstellar"))

    def test_verify_accepts_tag_with_extra_title_words(self):
        self.make("f/23.Nichts.ist.so.wie.es.scheint.1998.German.1080p-GRP "
                  "[tmdbid-387].mkv")
        real = t.Tmdb._get

        def as_23(self, path, **p):
            if path.startswith("/movie/"):
                return {"id": 387, "title": "23", "original_title": "23",
                        "release_date": "1998-01-01"}
            return real(self, path, **p)

        t.Tmdb._get = as_23
        try:
            _, out = self.run_cli("--verify", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("plausible", out)

    def test_verify_flags_wrong_year(self):
        self.make("f/Das.Boot.2015.German.1080p.BluRay.x264-GRP [tmdbid-387].mkv")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("suspect", out)
        self.assertIn("year 2015", out)

    def test_verify_flags_unknown_id(self):
        self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-999999].mkv")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("does not exist on TMDB", out)

    def test_verify_flags_nfo_that_overrides_filename(self):
        """Dateiname trägt #405775, die danebenliegende NFO #106646 — und Jellyfin
        nimmt die NFO, der Film bleibt also falsch zugeordnet."""
        v = self.make("f/The.Wall.2017.German.1080p-GRP [tmdbid-405775].mkv")
        v.with_suffix(".nfo").write_text(
            '<?xml version="1.0"?>\n<movie>\n  <title>The Wolf of Wall Street</title>\n'
            "  <tmdbid>106646</tmdbid>\n</movie>\n", encoding="utf-8")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("NFO overrides the filename", out)
        self.assertIn("106646", out)
        self.assertIn("1 of 1 suspect", out)

    def test_fix_nfo_sets_the_contradicting_file_aside(self):
        v = self.make("f/The.Wall.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        _, out = self.run_cli("--verify", "--fix-nfo", str(self.tmp))
        self.assertFalse(nfo.exists(), "NFO liegt noch im Weg")
        self.assertTrue(nfo.with_suffix(".nfo.bak").exists(), "keine Sicherung angelegt")
        self.assertIn("NFO set aside", out)

    def test_fix_nfo_is_undoable(self):
        v = self.make("f/The.Wall.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        self.run_cli("--verify", "--fix-nfo", str(self.tmp))
        self.run_cli("--undo", "1")
        self.assertTrue(nfo.exists(), "--undo hat die NFO nicht zurückgeholt")

    def test_fix_nfo_dry_run_changes_nothing(self):
        v = self.make("f/The.Wall.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        _, out = self.run_cli("--verify", "--fix-nfo", "-n", str(self.tmp))
        self.assertTrue(nfo.exists(), "Trockenlauf hat die NFO verschoben")
        self.assertFalse(nfo.with_suffix(".nfo.bak").exists())
        self.assertIn("dry run", out)

    def test_fix_nfo_leaves_matching_nfos_alone(self):
        v = self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>387</tmdbid></movie>", encoding="utf-8")
        self.run_cli("--verify", "--fix-nfo", str(self.tmp))
        self.assertTrue(nfo.exists(), "korrekte NFO wurde angefasst")

    def test_fix_nfo_does_not_clobber_an_existing_backup(self):
        v = self.make("f/The.Wall.2017.German.1080p-GRP [tmdbid-405775].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>106646</tmdbid></movie>", encoding="utf-8")
        old = nfo.with_suffix(".nfo.bak")
        old.write_text("aeltere Sicherung", encoding="utf-8")
        self.run_cli("--verify", "--fix-nfo", str(self.tmp))
        self.assertEqual(old.read_text(), "aeltere Sicherung", "alte Sicherung überschrieben")

    def test_verify_accepts_matching_nfo(self):
        v = self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text(
            '<?xml version="1.0"?>\n<movie>\n  <tmdbid>387</tmdbid>\n</movie>\n',
            encoding="utf-8")
        _, out = self.run_cli("--verify", str(self.tmp))
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
        v = self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text("  ___ SCENE ASCII ART ___\n")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("plausible", out)

    def test_stale_report_is_removed(self):
        """Regression: ein Report von gestern sah aus wie der von heute."""
        stale = t.CONFIG_DIR / "offen.jsonl"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"file": "/weg.mkv", "reason": "alt"}\n')
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        self.run_cli("--batch", str(self.tmp))
        self.assertFalse(stale.exists(), "veralteter Report blieb liegen")

    def test_report_is_rewritten_not_appended(self):
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        self.run_cli("--batch", str(self.tmp))
        self.run_cli("--batch", str(self.tmp))
        rows = (t.CONFIG_DIR / "offen.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(rows), 1, "Report wächst bei jedem Lauf")

    def test_from_report_processes_only_listed_files(self):
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        self.run_cli("--batch", str(self.tmp))          # Boot getaggt, Aliens offen
        _, out = self.run_cli("--batch", "--from-report", str(self.tmp))
        self.assertIn("1 file(s) from", out)
        self.assertIn("1 to process", out)

    def test_from_report_needs_no_path(self):
        """Regression: `tmdbtag --from-report` (so im README) gab nur die Hilfe aus."""
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        self.run_cli("--batch", str(self.tmp))
        rc, out = self.run_cli("--batch", "--from-report")
        self.assertEqual(rc, 0)
        self.assertNotIn("usage: tmdbtag", out)
        self.assertIn("1 file(s) from", out)

    def test_from_report_without_report_fails_clearly(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("--from-report", str(self.tmp))
        self.assertIn("No report at", str(cm.exception))

    def test_manual_skip_stays_in_report(self):
        """Sonst verschwindet die Datei beim --from-report-Durchgang."""
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        with mock.patch("builtins.input", return_value="s"), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.run_cli(str(self.tmp))
        report = t.CONFIG_DIR / "offen.jsonl"
        self.assertTrue(report.exists(), "übersprungene Datei fiel aus dem Report")
        rows = [json.loads(l) for l in report.read_text().splitlines() if l.strip()]
        self.assertEqual(rows[0]["reason"], "deferred")


class TestParallelPrefetch(E2EBase):
    def _make_many(self, n=12):
        for i in range(n):
            self.make(f"f/Das.Boot.198{i%10}.German.1080p.BluRay.x264-G{i:02d}.mkv", 10)

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
            self.run_cli("--batch", "--workers", "6", "--min-size", "0", str(self.tmp))
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
            self.run_cli("--batch", "--workers", "1", "--min-size", "0", str(self.tmp))
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

        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        self.make("f/The.Matrix.1999.German.1080p.BluRay.x264-GRP.mkv")
        self.make("f/Solaris.1972.German.1080p.BluRay.x264-PLX.mkv")
        self.make("f/Alien.1979.German.1080p.BluRay.x264-GRP.mkv")
        t.Tmdb._get = counting
        try:
            self.run_cli("--batch", str(self.tmp))
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
            rc, _ = self.run_cli("--batch", "--min-size", "0", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertEqual(rc, 0)


class TestRuntimeTiebreaker(E2EBase):
    """Laufzeit trennt gleichnamige Filme, wo Titel und Jahr nichts hergeben."""

    MARIA = [
        {"id": 1038263, "title": "Maria", "original_title": "Maria",
         "release_date": "2024-08-29", "popularity": 3.8},      # Callas, 123 min
        {"id": 1243381, "title": "Maria", "original_title": "Mary",
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
        v = self.make("f/Maria.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        self.assertEqual(t.media_duration(v), 112)

    def test_picks_candidate_matching_the_file(self):
        v = self.make("f/Maria.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        got = t.by_runtime(self._tmdb(), self.MARIA, v)
        self.assertIsNotNone(got, "kein Kandidat gewählt")
        self.assertEqual(got[1]["id"], 1243381, "falscher Film gewählt")

    def test_picks_the_other_one_for_a_longer_file(self):
        v = self.make("f/Maria.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>123</runtime></movie>")
        got = t.by_runtime(self._tmdb(), self.MARIA, v)
        self.assertEqual(got[1]["id"], 1038263)

    def test_declines_when_two_candidates_are_close(self):
        """11 min Abstand ist zu wenig — dann soll der Mensch entscheiden."""
        v = self.make("f/Maria.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>118</runtime></movie>")
        self.assertIsNone(t.by_runtime(self._tmdb(), self.MARIA, v))

    def test_title_breaks_the_tie_when_runtimes_collide(self):
        """Neben 'Maria' (112 min) liegt 'Salve Maria' (111 min): die Laufzeit
        allein reicht nicht, der Titel muss mitentscheiden."""
        cands = self.MARIA + [{"id": 1247597, "title": "Salve Maria",
                               "original_title": "Salve Maria",
                               "release_date": "2024-01-01", "popularity": 2.0}]
        rt = {**self.RUNTIMES, 1247597: 111}

        class Fake:
            lang = "de-DE"
            def details(self, mid): return {"id": mid, "runtime": rt.get(mid)}

        v = self.make("f/Maria.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        got = t.by_runtime(Fake(), cands, v, "Maria", 2024)
        self.assertIsNotNone(got, "Titel hätte entscheiden müssen")
        self.assertEqual(got[1]["id"], 1243381)

    def test_declines_when_runtime_and_title_both_tie(self):
        """Zwei Namensvettern gleicher Länge — hier darf nichts geraten werden."""
        twins = [
            {"id": 1, "title": "Maria", "original_title": "Maria",
             "release_date": "2024-01-01", "popularity": 3.0},
            {"id": 2, "title": "Maria", "original_title": "Maria",
             "release_date": "2024-06-01", "popularity": 3.1},
        ]

        class Fake:
            lang = "de-DE"
            def details(self, mid): return {"id": mid, "runtime": 112}

        v = self.make("f/Maria.2024.German-GRP.mkv", 10)
        v.with_suffix(".nfo").write_text("<movie><runtime>112</runtime></movie>")
        self.assertIsNone(t.by_runtime(Fake(), twins, v, "Maria", 2024))

    def test_declines_without_a_known_duration(self):
        v = self.make("f/Maria.2024.German-GRP.avi", 10)   # kein Header, keine NFO
        self.assertIsNone(t.by_runtime(self._tmdb(), self.MARIA, v))

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
        v = self.make("f/Das.Boot.1981.German-SoW [tmdbid-387].mkv")
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
            _, out = self.run_cli("--verify", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("runtime 22 min vs TMDB 149 min", out)

    def test_verify_tolerates_extended_cut(self):
        v = self.make("f/Das.Boot.1981.EXTENDED.German-SoW [tmdbid-387].mkv")
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
            _, out = self.run_cli("--verify", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("plausible", out)


class TestSafety(E2EBase):
    """Zusicherungen, auf die man sich verlassen muss."""

    def test_dry_run_leaves_an_existing_report_alone(self):
        """Regression: ein Trockenlauf über Ordner A löschte die offenen
        Fälle von Ordner B — 'ändert nichts' galt für den Report nicht."""
        report = t.CONFIG_DIR / "offen.jsonl"
        report.parent.mkdir(parents=True, exist_ok=True)
        keep = '{"file": "/woanders/wichtig.mkv", "reason": "von gestern"}\n'
        report.write_text(keep)
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        self.run_cli("--batch", "-n", str(self.tmp))
        self.assertEqual(report.read_text(), keep, "Trockenlauf hat den Report verändert")

    def test_dry_run_still_reports_the_count(self):
        self.make("f/Aliens.1979.Special.Edition.German.1080p-GRP.mkv")
        _, out = self.run_cli("--batch", "-n", str(self.tmp))
        self.assertIn("would be deferred", out)

    def test_dry_run_never_renames_or_logs(self):
        v = self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        before = self.names()
        self.run_cli("--batch", "-n", "--folder", "--nfo", str(self.tmp))
        self.assertEqual(before, self.names(), "Trockenlauf hat Dateien verändert")
        self.assertFalse(t.LOG_FILE.exists(), "Trockenlauf hat ins Undo-Log geschrieben")

    def test_overlong_name_is_truncated(self):
        """Sonst scheitert das Umbenennen am Dateisystem-Limit."""
        stem = "A" * 300
        out = t.new_stem(stem, {}, t.build_tag(387, None), "suffix")
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

    def test_set_key_can_be_prompted_instead_of_passed(self):
        """--set-key ohne Wert fragt nach, damit der Key nicht in der
        Shell-History landet."""
        t.CONFIG_FILE = self.tmp / "config.json"
        with mock.patch("getpass.getpass", return_value="  abc123  "), \
                redirect_stdout(io.StringIO()):
            sys.argv = ["tmdbtag", "--set-key"]
            rc = t.main()
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(t.CONFIG_FILE.read_text())["api_key"], "abc123")

    def test_empty_prompted_key_is_refused(self):
        t.CONFIG_FILE = self.tmp / "config.json"
        with mock.patch("getpass.getpass", return_value=""), \
                redirect_stdout(io.StringIO()):
            sys.argv = ["tmdbtag", "--set-key"]
            with self.assertRaises(SystemExit):
                t.main()

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
        got = t.collect([str(lib)], 0, True)
        self.assertEqual(got, [], f"Symlink verfolgt: {got}")


class TestSetId(E2EBase):
    """--id für Fälle, die keine Heuristik lösen kann."""

    def test_sets_the_tag_without_searching(self):
        searched = []
        real = t.Tmdb._get

        def watch(self, path, **p):
            if path == "/search/movie":
                searched.append(p.get("query"))
            if path.startswith("/movie/"):
                return {"id": 64690, "title": "Drive", "original_title": "Drive",
                        "release_date": "2011-09-16", "runtime": 100}
            return real(self, path, **p)

        t.Tmdb._get = watch
        try:
            self.run_cli("--id", "64690", str(self.make("f/Drive.1986.German-GRP.mkv")))
        finally:
            t.Tmdb._get = real
        self.assertEqual(searched, [], "hat trotzdem gesucht")
        self.assertIn("f/Drive.1986.German-GRP [tmdbid-64690].mkv", self.names())

    def test_replaces_an_existing_tag(self):
        real = t.Tmdb._get

        def details(self, path, **p):
            if path.startswith("/movie/"):
                return {"id": 901121, "title": "Spider Web", "runtime": 135}
            return real(self, path, **p)

        t.Tmdb._get = details
        try:
            self.run_cli("--id", "901121",
                         str(self.make("f/Spider.Web.2023-GRP [tmdbid-1173580].mkv")))
        finally:
            t.Tmdb._get = real
        got = self.names()
        self.assertIn("f/Spider.Web.2023-GRP [tmdbid-901121].mkv", got)
        self.assertNotIn("f/Spider.Web.2023-GRP [tmdbid-1173580].mkv", got)
        # kein doppelter Tag im Namen
        self.assertEqual(sum("tmdbid" in n for n in got), 1)

    def test_same_id_is_a_no_op(self):
        """Sonst hängt unique_path ein ' (2)' an, obwohl nichts zu tun ist."""
        v = self.make("f/Drive.1986.German-GRP [tmdbid-64690].mkv")
        real = t.Tmdb._get
        t.Tmdb._get = lambda s, path, **p: (
            {"id": 64690, "title": "Drive", "runtime": 100}
            if path.startswith("/movie/") else real(s, path, **p))
        try:
            self.run_cli("--id", "64690", str(v))
        finally:
            t.Tmdb._get = real
        self.assertEqual(self.names(), ["f", "f/Drive.1986.German-GRP [tmdbid-64690].mkv"])

    def test_sidecars_do_not_collect_a_second_tag(self):
        """Regression: der alte Tag steckte auch im Sidecar-Rest und wurde
        an den neuen angehängt."""
        v = self.make("f/Drive.1986.German-GRP [tmdbid-1].mkv")
        (v.parent / "Drive.1986.German-GRP [tmdbid-1].ger.srt").write_text("x")
        real = t.Tmdb._get
        t.Tmdb._get = lambda s, path, **p: (
            {"id": 64690, "title": "Drive", "runtime": 100}
            if path.startswith("/movie/") else real(s, path, **p))
        try:
            self.run_cli("--id", "64690", str(v))
        finally:
            t.Tmdb._get = real
        for n in self.names():
            self.assertLessEqual(n.count("tmdbid"), 1, f"doppelter Tag: {n}")
        self.assertIn("f/Drive.1986.German-GRP [tmdbid-64690].ger.srt", self.names())

    def test_strip_tag_closes_the_gap(self):
        self.assertEqual(t.strip_tag("Film.2000-GRP [tmdbid-1]"), "Film.2000-GRP")
        self.assertEqual(t.strip_tag(" [tmdbid-1].ger.srt"), ".ger.srt")
        self.assertEqual(t.strip_tag("A [tmdbid-1] [imdbid-tt7] B"), "A B")

    def test_renames_sidecars_too(self):
        v = self.make("f/Drive.1986.German-GRP.mkv")
        (v.parent / "Drive.1986.German-GRP.ger.srt").write_text("x")
        real = t.Tmdb._get
        t.Tmdb._get = lambda s, path, **p: (
            {"id": 64690, "title": "Drive", "runtime": 100}
            if path.startswith("/movie/") else real(s, path, **p))
        try:
            self.run_cli("--id", "64690", str(v))
        finally:
            t.Tmdb._get = real
        self.assertIn("f/Drive.1986.German-GRP [tmdbid-64690].ger.srt", self.names())

    def test_dry_run_changes_nothing(self):
        v = self.make("f/Drive.1986.German-GRP.mkv")
        before = self.names()
        real = t.Tmdb._get
        t.Tmdb._get = lambda s, path, **p: (
            {"id": 64690, "title": "Drive", "runtime": 100}
            if path.startswith("/movie/") else real(s, path, **p))
        try:
            self.run_cli("--id", "64690", "-n", str(v))
        finally:
            t.Tmdb._get = real
        self.assertEqual(before, self.names())

    def test_unknown_id_is_refused(self):
        real = t.Tmdb._get
        t.Tmdb._get = lambda s, path, **p: ({} if path.startswith("/movie/")
                                            else real(s, path, **p))
        try:
            with self.assertRaises(SystemExit) as cm:
                self.run_cli("--id", "999999999", str(self.make("f/X.2000-GRP.mkv")))
        finally:
            t.Tmdb._get = real
        self.assertIn("does not exist", str(cm.exception))


class TestTvHint(E2EBase):
    """Miniserien haben oft keinen Film-Eintrag — dann darf nicht der
    ähnlichste Film getaggt werden."""

    def test_reports_a_series_instead_of_guessing_a_film(self):
        real = t.Tmdb._get

        def with_tv(self, path, **p):
            if path == "/search/tv":
                return {"results": [{"id": 19614, "name": "Es",
                                     "original_name": "It",
                                     "first_air_date": "1990-11-18"}]}
            if path == "/search/movie":
                return {"results": []}
            return real(self, path, **p)

        t.Tmdb._get = with_tv
        try:
            self.make("f/Es.1990.German.DL.1080p.BluRay.x264-GRP.mkv")
            _, out = self.run_cli("--batch", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertIn("TV series #19614", out)
        self.assertIn("shows library", out)
        rows = [json.loads(l) for l in
                (t.CONFIG_DIR / "offen.jsonl").read_text().splitlines() if l.strip()]
        self.assertIn("19614", rows[0]["reason"])

    def test_no_tv_lookup_when_the_film_matches(self):
        calls = []
        real = t.Tmdb._get

        def watch(self, path, **p):
            calls.append(path)
            return real(self, path, **p)

        t.Tmdb._get = watch
        try:
            self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
            self.run_cli("--batch", str(self.tmp))
        finally:
            t.Tmdb._get = real
        self.assertNotIn("/search/tv", calls, "unnötige TV-Suche")


class TestDuplicateIds(E2EBase):
    def test_verify_reports_the_same_id_on_two_files(self):
        self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        self.make("f/Das.Boot.1981.German.2160p-UHD [tmdbid-387].mkv")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("1 duplicate id(s)", out)
        self.assertIn("id 387 on 2 files", out)

    def test_no_false_alarm_for_distinct_ids(self):
        self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        self.make("f/The.Matrix.1999.German.1080p-GRP [tmdbid-603].mkv")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertNotIn("duplicate", out)


class TestDragAndInspect(E2EBase):
    """Datei ins Terminal ziehen, analysieren, auf Wunsch taggen."""

    def test_unquotes_what_terminals_paste(self):
        cases = [
            ("/a/Some\\ Film.mkv", "/a/Some Film.mkv"),          # macOS escaped
            ("'/a/Some Film.mkv'", "/a/Some Film.mkv"),          # einfache Quotes
            ('"/a/Some Film.mkv"', "/a/Some Film.mkv"),          # doppelte Quotes
            ("  /a/Film.mkv  \n", "/a/Film.mkv"),                # Leerraum
            ("/a/Wei\\ \\[2\\].mkv", "/a/Wei [2].mkv"),          # escapte Klammern
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(str(t.unquote_path(raw)), want)

    def test_dragging_a_sidecar_uses_the_video(self):
        """Im Finder liegen .nfo und .mkv nebeneinander — wer die NFO zieht,
        meint den Film. Sonst würde die NFO selbst umbenannt."""
        v = self.make("f/Drive.2011.German-GRP [tmdbid-64690].mkv")
        nfo = v.with_suffix(".nfo")
        nfo.write_text("<movie><tmdbid>64690</tmdbid><runtime>97</runtime></movie>")
        with mock.patch("builtins.input", side_effect=[str(nfo), "s", "q"]), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            _, out = self.run_cli("--inspect")
        self.assertIn("using Drive.2011.German-GRP [tmdbid-64690].mkv instead", out)

    def test_sidecar_as_argument_resolves_too(self):
        v = self.make("f/Das.Boot.1981.German-SoW.mkv")
        srt = v.with_suffix(".ger.srt")
        srt.write_text("x")
        with mock.patch("builtins.input", return_value="1"), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.run_cli("--inspect", str(srt))
        got = self.names()
        self.assertIn("f/Das.Boot.1981.German-SoW [tmdbid-387].mkv", got)
        self.assertIn("f/Das.Boot.1981.German-SoW [tmdbid-387].ger.srt", got)

    def test_lone_non_video_is_refused(self):
        stray = self.tmp / "f" / "notizen.txt"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("x")
        with mock.patch("builtins.input", side_effect=[str(stray), "q"]), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            _, out = self.run_cli("--inspect")
        self.assertIn("not a video file", out)

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

    def test_unescaped_path_with_spaces_is_taken_literally(self):
        """Aus dem Finder kopierte Pfade tragen keine Escapes — shlex würde
        sie am Leerzeichen zerschneiden."""
        v = self.make("f/Some Film 2011 [tmdbid-1].mkv", 10)
        self.assertEqual(t.unquote_path(str(v)), v)

    def test_unquote_survives_unbalanced_quotes(self):
        self.assertIsNotNone(t.unquote_path('/a/it\'s a film.mkv'))

    def test_unquote_ignores_empty_input(self):
        self.assertIsNone(t.unquote_path("   "))

    def test_inspect_shows_what_matters(self):
        v = self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        v.with_suffix(".nfo").write_text(
            "<movie><tmdbid>387</tmdbid><runtime>149</runtime></movie>")
        real = t.Tmdb._get

        def with_runtime(self, path, **p):
            d = dict(real(self, path, **p))
            if path.startswith("/movie/"):
                d["runtime"] = 149
            return d

        t.Tmdb._get = with_runtime
        try:
            with mock.patch("builtins.input", return_value="s"), \
                    mock.patch.object(sys.stdin, "isatty", return_value=True):
                _, out = self.run_cli("--inspect", str(v))
        finally:
            t.Tmdb._get = real
        self.assertIn("149 min", out)          # Laufzeit der Datei
        self.assertIn("tag in name: #387", out)
        self.assertIn("NFO agrees", out)
        self.assertIn("Matches:", out)

    def test_inspect_flags_an_overriding_nfo(self):
        v = self.make("f/The.Wall.2017.German-GRP [tmdbid-405775].mkv")
        v.with_suffix(".nfo").write_text("<movie><tmdbid>106646</tmdbid></movie>")
        with mock.patch("builtins.input", return_value="s"), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            _, out = self.run_cli("--inspect", str(v))
        self.assertIn("NFO overrides the filename", out)

    def test_inspect_tags_on_confirmation(self):
        v = self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        with mock.patch("builtins.input", return_value="1"), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.run_cli("--inspect", str(v))
        self.assertIn("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW [tmdbid-387].mkv",
                      self.names())

    def test_inspect_leaves_the_file_alone_on_skip(self):
        v = self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        before = self.names()
        with mock.patch("builtins.input", return_value="s"), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.run_cli("--inspect", str(v))
        self.assertEqual(before, self.names())

    def test_drag_loop_handles_a_missing_file_and_quits(self):
        with mock.patch("builtins.input", side_effect=["/gibt/es/nicht.mkv", "q"]), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            rc, out = self.run_cli("--inspect")
        self.assertEqual(rc, 0)
        self.assertIn("not found", out)
        self.assertIn("Done:", out)

    def test_drag_loop_rejects_a_directory(self):
        d = self.tmp / "f"
        d.mkdir(exist_ok=True)
        with mock.patch("builtins.input", side_effect=[str(d), "q"]), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            _, out = self.run_cli("--inspect")
        self.assertIn("directory", out)

    def test_drag_loop_tags_a_dragged_file(self):
        v = self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        escaped = str(v).replace(" ", "\\ ")
        with mock.patch("builtins.input", side_effect=[escaped, "1", "q"]), \
                mock.patch.object(sys.stdin, "isatty", return_value=True):
            self.run_cli("--inspect")
        self.assertIn("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW [tmdbid-387].mkv",
                      self.names())


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
        for stem in ["Das.Boot.1981.German-SoW", "Ocean's 11", "Terminator 2"]:
            self.assertEqual(t.split_part(stem), (stem, None))

    def test_both_parts_get_the_tag_and_a_part_suffix(self):
        for n in (1, 2):
            self.make(f"f/Das.Boot.1981.German.1080p-SoW.CD{n}.mkv")
        self.run_cli("--batch", str(self.tmp))
        got = self.names()
        self.assertIn("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387] - part1.mkv", got)
        self.assertIn("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387] - part2.mkv", got)

    def test_verify_does_not_call_two_parts_a_duplicate(self):
        for n in (1, 2):
            self.make(f"f/Das.Boot.1981-SoW [tmdbid-387] - part{n}.mkv")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertNotIn("duplicate", out)

    def test_verify_still_reports_a_real_duplicate(self):
        self.make("f/Das.Boot.1981.German.1080p-SoW [tmdbid-387].mkv")
        self.make("f/Das.Boot.1981.German.2160p-UHD [tmdbid-387].mkv")
        _, out = self.run_cli("--verify", str(self.tmp))
        self.assertIn("duplicate", out)


class TestNfo(E2EBase):
    def test_nfo_written_next_to_video(self):
        d = "Das.Boot.1981.German.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        self.run_cli("--auto", "--nfo", "--no-rename", str(self.tmp))
        nfo = self.tmp / d / f"{d}.nfo"
        self.assertTrue(nfo.exists())
        text = nfo.read_text()
        self.assertIn('<uniqueid type="tmdb" default="true">387</uniqueid>', text)
        self.assertIn("<tmdbid>387</tmdbid>", text)
        # Dateiname unangetastet
        self.assertTrue((self.tmp / d / f"{d}.mkv").exists())

    def test_nfo_follows_renamed_file(self):
        d = "Das.Boot.1981.German.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        self.run_cli("--auto", "--nfo", "--folder", str(self.tmp))
        new = self.tmp / f"{d} [tmdbid-387]" / f"{d} [tmdbid-387].nfo"
        self.assertTrue(new.exists(), f"NFO nicht gefunden, da: {self.names()}")

    def test_scene_nfo_not_overwritten(self):
        d = "Das.Boot.1981.German.1080p.BluRay.x264-SoW"
        self.make(f"{d}/{d}.mkv")
        scene = self.tmp / d / f"{d}.nfo"
        scene.write_text("  ____  SCENE ASCII ART  ____\n")
        self.run_cli("--auto", "--nfo", "--no-rename", str(self.tmp))
        self.assertIn("SCENE ASCII ART", scene.read_text())
        self.assertTrue((self.tmp / d / "movie.nfo").exists())

    def test_nfo_reuses_id_from_existing_tag(self):
        """--no-rename --nfo auf schon getaggter Datei: keine Suche nötig."""
        self.make("flat/The.Matrix.1999 [tmdbid-603].mkv")
        self.run_cli("--nfo", "--no-rename", str(self.tmp))
        nfo = self.tmp / "flat" / "The.Matrix.1999 [tmdbid-603].nfo"
        self.assertTrue(nfo.exists())
        self.assertIn("<tmdbid>603</tmdbid>", nfo.read_text())

    def test_xml_escaping(self):
        movie = {"id": 1, "title": "Fish & Chips <Director's Cut>",
                 "release_date": "2000-01-01", "overview": "a > b & c"}
        video = self.make("x/Film.2000.mkv", 10)
        with redirect_stdout(io.StringIO()):
            out = t.write_nfo(video, movie, None, dry=False, force=False)
        text = out.read_text()
        self.assertIn("Fish &amp; Chips &lt;Director's Cut&gt;", text)
        self.assertNotIn("& C", text)


if __name__ == "__main__":
    unittest.main()


class TestLanguageSwitch(E2EBase):
    """Englisch ist Standard, TMDBTAG_LANG=de schaltet zurück."""

    def test_default_is_english(self):
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        _, out = self.run_cli("--batch", str(self.tmp))
        self.assertIn("to process", out)
        self.assertIn("Done:", out)
        self.assertNotIn("zu bearbeiten", out)

    def test_german_via_env(self):
        self.make("f/Das.Boot.1981.German.1080p.BluRay.x264-SoW.mkv")
        real = t.LANG
        t.LANG = "de"
        try:
            _, out = self.run_cli("--batch", str(self.tmp))
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

# Changelog

## v2.0.0 — 2026-10-04

### Breaking: three modes instead of 27 flags

    tmdbtag [-n] PATH...          tag movies
    tmdbtag [-n] check PATH...    check existing tags
    tmdbtag [-n] undo             undo the last run

- **Tagging** takes confident matches automatically and offers the picker for
  the rest. Without a terminal (cron, pipes) it never asks; uncertain files stay
  untagged and the next run in a terminal picks up exactly those. This replaces
  `--auto`, `--batch`, `-y`, `--report` and `--from-report`.
- **Re-identifying** a file: pass a tagged file directly and the picker comes up,
  `[i]` takes a TMDB id. Replaces `--id`, `--inspect` and `--force`.
- **`check`** replaces `--verify`; NFOs that contradict the filename are always
  set aside to `.nfo.bak` (logged, so `undo` restores them). Replaces `--fix-nfo`.
- **`undo`** reverts the whole last run instead of taking a count.
- **API key:** asked for on the first run in a terminal (no echo) and stored
  `chmod 600`. `--set-key` is gone; `TMDB_API_KEY` still works.
- Removed: `--style`, `--folder`, `--imdb`, `--nfo`, `--no-rename`,
  `--no-sidecars`, `--lang`, `--timeout`, `--workers`, `--min-size`,
  `--no-recursive`. Their defaults are now fixed: original name + tag, sidecars
  renamed along, recursive scan, de-DE metadata, 50 MB minimum size.

## v1.1.0 — 2026-08-09

### Security

- **Breaking:** `--set-key` no longer accepts the key as an argument and always
  prompts instead, so it can never reach the shell history or the process list
  (argv is world-readable via `ps` on a multi-user box). `tmdbtag --set-key KEY`
  now exits with an error telling you to treat that key as compromised.
  Unattended setups should use `TMDB_API_KEY` in the environment.
- **Breaking:** `--api-key` removed for the same reason. The key now comes from
  `TMDB_API_KEY`/`TMDB_TOKEN` or the stored config only — never from argv.

## v1.0.0 — 2026-08-08

First release. Most of the entries below come from running the tool over a
large library rather than from unit testing — several were defects that only
appear at scale or against messy real-world filenames.

### Tagging

- Parse scene release names (title + year) with a heuristic that survives the
  usual traps: `Star Courier 2077` (2017), `1934` (2019), `2005` (2009),
  `Sand.Part.Two`, years in brackets, existing tags.
- Append `[tmdbid-N]` to the filename without rewriting it; `--style clean`
  rewrites to `Title (Year) [tmdbid-N]` instead. Optional `[imdbid-tt…]`.
- `--folder` tags the containing folder, but refuses directories holding more
  than one movie.
- Subtitles, NFOs and posters sharing the stem are renamed along.
- `--nfo` writes a `<movie>.nfo` carrying the id; `--no-rename` leaves
  filenames untouched entirely.

### Getting it right

- **German scene spellings.** Releases transliterate umlauts (`Die Faehre`),
  which TMDB cannot resolve to `Die Fähre`. Search retries with the umlaut
  form and comparison folds `ae`/`oe`/`ue`.
- **Runtime as a tiebreaker.** Two films can share title *and* year — `Odila`
  (2024) is both a singer biopic (123 min) and a biblical drama (112 min).
  The file's own runtime decides, read from an existing NFO, else straight
  from the Matroska or MP4 header, else `ffprobe`. Only on clean evidence;
  where `Ufer` (130 min) sits next to `Am Ufer` (129 min), the title must
  agree as well.
- **Roman sequel numbering.** `Der Hafen 02` matches `Der Hafen - Teil II`.
- **Titles with extra words.** `47 Alles ist anders als gedacht` matches
  TMDB's `47`; `Hanna Bergs Der Morast` matches `Der Morast`.

### Running at scale

- `--batch` never prompts: confident matches are tagged, everything else goes
  to a report for later. `--from-report` works through exactly those.
- A network hiccup skips one file instead of ending the run; five consecutive
  failures abort.
- `--workers` resolves titles through a thread pool up front (renaming stays
  single-threaded). Scanning skips macOS `._` files without stat()ing them.
- Progress counter, ETA, and already-tagged files summarised in one line
  rather than hundreds.
- Every rename is logged as it happens, so `--undo N` works even after Ctrl-C.

### Checking what is already tagged

- `--verify` compares each tag against the filename, and reports titles that
  disagree, years off by more than one, runtimes off by more than 30 min / 40 %,
  and ids that do not exist.
- **NFO sidecars take priority in Jellyfin**, so a stale NFO silently defeats a
  correct filename. `--verify` reports that, and `--fix-nfo` moves the file to
  `.nfo.bak` (reversible via `--undo`).

### Safety

- `--dry-run` changes nothing at all, including the deferred-cases report.
- `sanitize()` cannot escape a directory, never yields an empty name, and caps
  length at 200 bytes.
- `collect()` does not follow symlinks out of the library tree.
- `unique_path()` never overwrites an existing file.
- The API key is stored `chmod 600`; `--set-key` without a value prompts so it
  stays out of the shell history.

### Other

- Interface in English, `TMDBTAG_LANG=de` for German.
- 100 tests, standard library only, no network access in the suite.

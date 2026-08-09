# Changelog

## Unreleased

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
  usual traps: `Blade Runner 2049` (2017), `1917` (2019), `2012` (2009),
  `Dune.Part.Two`, years in brackets, existing tags.
- Append `[tmdbid-N]` to the filename without rewriting it; `--style clean`
  rewrites to `Title (Year) [tmdbid-N]` instead. Optional `[imdbid-tt…]`.
- `--folder` tags the containing folder, but refuses directories holding more
  than one movie.
- Subtitles, NFOs and posters sharing the stem are renamed along.
- `--nfo` writes a `<movie>.nfo` carrying the id; `--no-rename` leaves
  filenames untouched entirely.

### Getting it right

- **German scene spellings.** Releases transliterate umlauts (`Die Paepstin`),
  which TMDB cannot resolve to `Die Päpstin`. Search retries with the umlaut
  form and comparison folds `ae`/`oe`/`ue`.
- **Runtime as a tiebreaker.** Two films can share title *and* year — `Maria`
  (2024) is both the Callas biopic (123 min) and a biblical drama (112 min).
  The file's own runtime decides, read from an existing NFO, else straight
  from the Matroska or MP4 header, else `ffprobe`. Only on clean evidence;
  where `Eden` (130 min) sits next to `Martin Eden` (129 min), the title must
  agree as well.
- **Roman sequel numbering.** `Der Pate 02` matches `Der Pate - Teil II`.
- **Titles with extra words.** `23 Nichts ist so wie es scheint` matches
  TMDB's `23`; `Stephen Kings Der Nebel` matches `Der Nebel`.

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

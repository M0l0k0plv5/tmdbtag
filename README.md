# tmdbtag

Append Jellyfin-readable TMDB IDs to movie files — **without losing the original release name**.

```
Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW.mkv
  → Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW [tmdbid-387].mkv
```

Jellyfin strips the bracketed part when parsing and uses the ID as a hard match, so every
movie is identified correctly — no more "Das Boot" resolving to the 1985 TV series.

> **One caveat that costs people hours:** if an `.nfo` file sits next to the video,
> Jellyfin reads *that* first and ignores the filename entirely. A stale NFO written by
> an earlier bad match silently defeats the rename. `--verify` now reports this case.

Single Python file, standard library only, no dependencies, no database, no daemon.

## Why not FileBot / Radarr?

Be honest with yourself before installing this — you may not need it:

| Tool | Does this? | Trade-off |
|---|---|---|
| [FileBot](https://www.filebot.net/) | Yes, via custom format `{fn} [tmdbid-{id}]` | Commercial licence, large Java install |
| [Radarr](https://trash-guides.info/Radarr/Radarr-recommended-naming-scheme/) | Yes, via `{Original Title} [tmdbid-{TmdbId}]` | Only worth it if your whole library lives in Radarr |
| [tinyMediaManager](https://www.tinymediamanager.org/docs/movies/renamer) | Yes, via renamer tokens | GUI-centric, leans on NFO files |

`tmdbtag` exists for the narrow case in between: you have a pile of scene releases, you want
them tagged **once**, you don't want a licence, a GUI, or a media manager owning your library.
Point it at a directory, answer a few prompts, done. `--undo` if you regret it.

## Install

Requires Python 3.9+ (macOS ships this; Linux too).

```bash
git clone https://github.com/M0l0k0plv5/tmdbtag.git
install -m755 tmdbtag/tmdbtag.py ~/.local/bin/tmdbtag
```

Get a free API key at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
(v3 key or v4 read access token — both work), then:

```bash
tmdbtag --set-key              # prompts, stored in ~/.config/tmdbtag/config.json (chmod 600)
```

Pass the key as an argument (`--set-key abc123`) only if you don't mind it landing in your
shell history. `TMDB_API_KEY` in the environment works too.

## Usage

```bash
tmdbtag -n ~/Movies                  # dry run — always start here
tmdbtag --batch ~/Movies             # unattended: tag confident hits, defer the rest
tmdbtag --auto ~/Movies              # take confident matches, ask about the rest
tmdbtag --undo 10                    # roll back the last 10 renames
```

For a large library, run `--batch` first and clean up afterwards. It never prompts:
confident matches are tagged, everything else lands in a report
(`~/.config/tmdbtag/offen.jsonl`) so the run finishes unattended. Then go back with
plain `tmdbtag <dir>` — already-tagged files are skipped, so you only get asked about
what is actually left. A network hiccup skips that one file instead of killing the run.

### Runtime as a tiebreaker

Two films can share a title *and* a year — `Maria` (2024) is both the Callas biopic
(123 min) and a biblical drama (112 min). Title and year cannot separate those, so when a
match stays uncertain, tmdbtag reads the file's actual runtime and compares it against the
candidates. Duration comes from an existing `.nfo`, otherwise straight from the Matroska or
MP4 header (no dependencies), otherwise `ffprobe` if installed.

It only decides when the evidence is clean: exactly one candidate within 6 minutes and the
next one at least 8 minutes further off. If several candidates match the runtime — `Eden`
(130 min) sits right next to `Martin Eden` (129 min) — the title has to break the tie as
well, or the case stays open for you.

`--verify` also reports runtimes that are grossly off (more than 30 min or 40%), which
catches wrong ids that title and year alone never reveal. Extended cuts and remasters stay
below that threshold on purpose.

`--auto` only accepts a match when title *and* year line up and the runner-up is clearly worse.
Otherwise you get a picker:

```
Das.Boot.1981.German.DL.1080p.BluRay.x264-SoW.mkv  /Volumes/Media/Movies
   erkannt: "Das Boot" (1981)
   Treffer:
   1) Das Boot (1981)  #387
      U-96 auf Feindfahrt im Nordatlantik…
   2) Das Boot (2018)  #79008
      Fortsetzung als Serie…
   Nummer / [s]kip / [i]d eingeben / [n]eu suchen / [q]uit:
```

`[i]` lets you paste a TMDB ID directly when the search is hopeless.

### Inspecting a single file

Drag a file into the terminal and see everything that bears on its identification —
size, actual runtime, the tag already in the name, whether a sidecar NFO agrees, what the
parser made of the filename, and how each candidate's runtime compares:

```bash
tmdbtag --inspect            # then drag files in, one after another
tmdbtag --inspect FILE       # or pass them as arguments
```

```
The.Prestige.2006.German.AC3.1080p.BluRay.x265-GTF [tmdbid-1124].mkv
   1.49 GB, 131 min
   tag in name: #1124 Prestige - Die Meister der Magie (2006, 130 min)
   NFO agrees (#1124)
   detected: "The Prestige" (2006)
   Matches:
   1) Prestige - Die Meister der Magie / The Prestige (2006)  #1124  130 min ✓ ←
   tag it? number / [s]kip / [q]uit:
```

`✓` marks a runtime within 6 minutes, `←` the id the file already carries. Paths pasted by
the terminal are unescaped for you — backslashes, quotes, or none at all if you copied the
path from Finder. Drag the `.nfo` or a subtitle by mistake and it switches to the video file
they belong to, rather than renaming the sidecar on its own.

### When you already know the id

Some cases no heuristic can settle — a film released under a different title, or a TMDB entry
added after the fact. `--id` sets it straight, with the same renaming, sidecar handling and
undo logging as a normal run:

```bash
tmdbtag --id 64690 "Drive.1986.German-GRP.mkv"
```

An existing tag is replaced rather than appended to, and passing the id a file already carries
does nothing.

### Series mistaken for films

TV mini-series often have no movie entry at all, so a plain search settles for the closest
film and tags the wrong one. When no film matches convincingly, tmdbtag checks the TV index
and says so instead of guessing — those files belong in your shows library, not here.

### Options

| Flag | Effect |
|---|---|
| `-n`, `--dry-run` | Show what would happen, change nothing |
| `--auto` | Accept confident matches without asking |
| `--batch` | Never prompt: tag confident hits, write the rest to a report |
| `--report` | Where that report goes (default `~/.config/tmdbtag/offen.jsonl`) |
| `--timeout` | Seconds per TMDB request, default 15 |
| `--workers` | Parallel TMDB lookups up front, default 6 (`1` disables) |
| `--verify` | Check existing `[tmdbid-…]` tags against the filename |
| `--fix-nfo` | With `--verify`: move contradicting NFOs aside to `.nfo.bak` |
| `--from-report` | Work through the deferred cases instead of rescanning |
| `--id N` | Set this TMDB id directly, no search — replaces an existing tag |
| `--inspect` | Analyse files in detail and offer to tag; without a path, accept dragged files |
| `-y`, `--yes` | Never ask; always take the best match (risky on messy libraries) |
| `--style clean` | Rename to `Title (Year) [tmdbid-N]` instead of keeping the release name |
| `--folder` | Also tag the containing folder — skipped for folders holding several movies |
| `--imdb` | Append `[imdbid-tt…]` as well |
| `--nfo` | Also write a `<movie>.nfo` carrying the ID |
| `--no-rename` | Leave filenames alone — pair with `--nfo` |
| `--force` | Reprocess files that already carry a tag |
| `--lang` | TMDB metadata language, default `de-DE` |
| `--min-size` | Minimum size in MB when scanning directories, default 50 |
| `--undo N` | Undo the last N renames |

### The NFO alternative

If you'd rather not touch filenames at all, Jellyfin also reads provider IDs from NFO sidecars:

```bash
tmdbtag --auto --nfo --no-rename ~/Movies
```

This writes `<movie>.nfo` containing `<uniqueid type="tmdb" default="true">387</uniqueid>`.
Existing scene `.nfo` files (the ASCII-art kind) are detected and never overwritten — the
metadata goes to `movie.nfo` instead.

## Checking what you already tagged

Tags written by earlier runs were never validated. `--verify` fetches each tagged
id and compares it against the filename, flagging titles that do not match and years
that are off by more than one:

```bash
tmdbtag --verify ~/Movies              # report suspicious tags
tmdbtag --verify --fix-nfo ~/Movies    # …and set overriding NFOs aside
tmdbtag --from-report --force          # then re-do just those
```

It checks the NFO first, because Jellyfin does too: a `<tmdbid>` in the sidecar that
disagrees with the filename is reported as an override, since that is what actually
decides what Jellyfin shows. `--fix-nfo` renames those to `.nfo.bak`, which Jellyfin ignores, so it falls back to the
filename and refetches on the next scan with *Replace metadata*. The move is logged and
`--undo` reverses it. Note that a plain Jellyfin scan **rewrites the NFO from its own
database**, reintroducing the old wrong id — only *Replace metadata* refetches.

## Language

The interface is English. Set `TMDBTAG_LANG=de` for German output.

## Safety

- Dry run first; nothing is destructive by default — `-n` also leaves the deferred-cases
  report untouched, so a trial run over one directory cannot discard another's results.
- Every rename is logged to `~/.config/tmdbtag/renames.jsonl` and reversible with `--undo`.
- `sample` files and anything under `--min-size` are ignored.
- Files that already carry a tag are skipped unless `--force`.
- `--folder` refuses to rename directories containing more than one movie.
- `--verify` reports the same TMDB id on several files — duplicate copies or a misfiled one.
- Nothing is re-encoded or remuxed. Only names are touched.

## What it can't do

- **Movies only.** TV shows need episode-level matching; use Sonarr or FileBot.
- No embedded MKV tag writing — Jellyfin ignores those for provider IDs, which is the whole
  reason this tool renames instead.
- Parsing is heuristic. Weird releases will need the interactive picker, and that's fine.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

128 tests, no network access (the TMDB client is stubbed), no dependencies.

## Licence

MIT

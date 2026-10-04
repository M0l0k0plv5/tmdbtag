# tmdbtag

Append Jellyfin-readable TMDB IDs to movie files — **without losing the original release name**.

```
Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP.mkv
  → Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP [tmdbid-387].mkv
```

Jellyfin strips the bracketed part when parsing and uses the ID as a hard match, so every
movie is identified correctly — no more "Der Leuchtturm" resolving to the TV series.

> **One caveat that costs people hours:** if an `.nfo` file sits next to the video,
> Jellyfin reads *that* first and ignores the filename entirely. A stale NFO written by
> an earlier bad match silently defeats the rename. `tmdbtag check` finds and fixes this.

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
Point it at a directory, answer a few prompts, done. `tmdbtag undo` if you regret it.

## Install

Requires Python 3.9+ (macOS ships this; Linux too).

```bash
git clone https://github.com/M0l0k0plv5/tmdbtag.git
install -m755 tmdbtag/tmdbtag.py ~/.local/bin/tmdbtag
```

Get a free API key at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)
(v3 key or v4 read access token — both work). The first run asks for it (no echo) and stores
it in `~/.config/tmdbtag/config.json` (chmod 600). The key is never taken from the command
line, so it stays out of your shell history. For unattended setups put `TMDB_API_KEY` in the
environment instead.

## Usage

There are three modes, and `-n` (dry run) works with all of them:

```bash
tmdbtag -n ~/Movies          # dry run — always start here
tmdbtag ~/Movies             # tag
tmdbtag check ~/Movies       # check what is already tagged
tmdbtag undo                 # undo the last run
```

Tagging takes confident matches — title *and* year line up, runner-up clearly worse —
without asking. Everything else gets a picker:

```
Der.Leuchtturm.1981.German.DL.1080p.BluRay.x264-GRP.mkv
   detected: "Der Leuchtturm" (1981)
   Matches:
   1) Der Leuchtturm (1981)  #387
      Ein Wärter allein auf der Klippe…
   2) Der Leuchtturm (2018)  #79008
      Fortsetzung als Serie…
   number / [s]kip / [i]d / [n]ew search / [q]uit:
```

`[i]` takes a TMDB id directly when the search is hopeless.

Without a terminal (cron, a pipe) nothing is ever asked: uncertain files simply stay
untagged. Since already-tagged files are skipped, the next run in a terminal only asks
about exactly those. A network hiccup skips one file instead of killing the run.

To **re-identify** a single file, pass it directly — even if it already carries a tag.
The picker comes up and the old tag is replaced rather than appended to. Passing a
subtitle or `.nfo` by mistake switches to the video it belongs to.

### Release folders with obfuscated files

Many keep each release as a folder named after the scene release, with a shortened file
inside (`Some.Film.2019.German.DL.1080p.BluRay.x264-GRP/grp-sf-1080p.mkv`). When the
filename has no year but the folder looks like a release (year followed by tokens like
`1080p` or `BluRay`), the folder name is searched instead. Collection folders such as
`Western Collection` or `Movies 2024` are not mistaken for releases.

### Runtime as a tiebreaker

Two films can share a title *and* a year — `Odila` (2024) is both a singer biopic
(123 min) and a biblical drama (112 min). Title and year cannot separate those, so when a
match stays uncertain, tmdbtag reads the file's actual runtime and compares it against the
candidates. Duration comes from an existing `.nfo`, otherwise straight from the Matroska or
MP4 header (no dependencies), otherwise `ffprobe` if installed.

It only decides when the evidence is clean: exactly one candidate within 6 minutes and the
next one at least 8 minutes further off. If several candidates match the runtime — `Ufer`
(130 min) sits right next to `Am Ufer` (129 min) — the title has to break the tie as
well, or the case stays open for you.

`check` also reports runtimes that are grossly off (more than 30 min or 40%), which
catches wrong ids that title and year alone never reveal. Extended cuts and remasters stay
below that threshold on purpose.

### Series mistaken for films

TV mini-series often have no movie entry at all, so a plain search settles for the closest
film and tags the wrong one. When no film matches convincingly, tmdbtag checks the TV index
and says so instead of guessing — those files belong in your shows library, not here.

## Checking what you already tagged

`tmdbtag check` fetches each tagged id and compares it against the filename, flagging
titles that do not match, years off by more than one, ids that do not exist, and the same
id on several files (genuine two-parters excepted).

It looks at the NFO first, because Jellyfin does too: a `<tmdbid>` in the sidecar that
disagrees with the filename decides what Jellyfin shows. `check` moves such NFOs to
`.nfo.bak`, which Jellyfin ignores, so it falls back to the filename and refetches on the
next scan with *Replace metadata*. The move is logged and `tmdbtag undo` reverses it. Note
that a plain Jellyfin scan **rewrites the NFO from its own database**, reintroducing the
old wrong id — only *Replace metadata* refetches.

## Language

The interface is English. Set `TMDBTAG_LANG=de` for German output.

## Safety

- Dry run first; `-n` changes nothing and logs nothing.
- Every rename is logged to `~/.config/tmdbtag/renames.jsonl`; `tmdbtag undo` reverts the
  whole last run, even one cut short by Ctrl-C.
- `sample` files and videos under 50 MB are ignored when scanning.
- Files that already carry a tag are skipped when scanning a folder.
- Subtitles, NFOs and posters sharing the file's name are renamed along.
- A movie split across two files (`…CD1.mkv`, `…CD2.mkv`) is renamed to Jellyfin's
  `… [tmdbid-N] - part1` form, so both halves stay one film.
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

108 tests, no network access (the TMDB client is stubbed), no dependencies.

## Licence

MIT

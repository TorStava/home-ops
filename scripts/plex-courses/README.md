# plex-courses

Presents the video courses in `tank/data/tutorials` to Plex as a **TV Shows**
library, so lessons play in order with "next lesson", On Deck and per-course
progress. This replaces an "Other Videos" library, which sorts correctly but
plays every file as a separate movie.

| Course tree      | Plex     |
|------------------|----------|
| course folder    | show     |
| section folder   | season   |
| lesson file      | episode  |

The order comes from a natural sort, so `2.` comes before `10.`. A one-video
folder whose siblings are also one-video folders is a lesson, not a section.

**The originals are never touched.** `apply` builds a mirror at
`tank/data/tutorials-plex` out of hardlinks, so it uses no extra space and
must be on the same dataset.

Mirror files use bare names: `<Course>/Season 04/s04e12.mp4`. Lesson names
caused three problems when they were in the filename:

- Dates in course names ("Updated 6-2022") turned into episode numbers.
- Plex silently skips files named like "…Sample…".
- Plex also skipped files ending in "-Video1".

Titles come from the metadata files instead:

- `tvshow.nfo` and one `.nfo` per lesson give the course and lesson titles
  for the *Plex NFO Series* agent.
- `.plexmatch` pins the title and the sNNeNN numbering.

Subtitles (`.srt`/`.vtt`) are linked next to their lesson.

`.ts` files count as lessons only when they are MPEG-TS (first byte `0x47`).
Programming courses also ship TypeScript sources (`*.d.ts` etc.), which once
showed up as unplayable episodes named "global.d".

**Show posters.** The NFO agent never fetches artwork, so a show without a
local `poster.*` is a blank tile. `apply` links a cover image from the course
root (`poster|cover|folder|show.jpg|png`) as `poster.<ext>`. That covered 23
of 648 courses on 11 Oct 2026. `posters` makes a `poster.jpg` for every other
show: a lesson frame over a blurred copy, with the course title. If no lesson
in the first three decodes, it makes a title card. `apply --prune` keeps
those generated posters. A cover added to the course later replaces one.

`posters` also writes `fanart.jpg`, the same frame at 1920×1080, which Plex
shows as the course page background. Shows whose lessons don't decode get
none. Show tiles stay portrait: a TV Shows library always uses 2:3 posters,
and only "Other Videos" libraries use landscape tiles (rejected: no play-next).

## Run (on mimir)

```sh
python3 plex_courses.py plan  /mnt/tank/data/tutorials --out /tmp/plan    # dry run: mapping.tsv + review.tsv
python3 plex_courses.py apply /mnt/tank/data/tutorials /mnt/tank/data/tutorials-plex --prune
python3 plex_courses.py apply ... --only 'SOLIDWORKS'   # pilot a few courses (no --prune)
```

Re-run `apply --prune` after adding, renaming or removing courses. It is
idempotent and only ever deletes inside the mirror.

Then make posters and fanart for new courses. TrueNAS has no ffmpeg, so run this on a
machine that has it; it reads and writes the mirror over ssh/sftp:

```sh
python3 plex_courses.py posters /mnt/tank/data/tutorials-plex --host mimir   # --force remakes all
```

Plex only picks up new posters for existing shows on a metadata refresh
(library → Refresh All Metadata), not on a plain scan.

## Plex library

- Type **TV Shows**, scanner **Plex TV Series**, agent **Plex NFO Series**,
  advanced setting **Use season titles** on
- Folder: `/mnt/data/tutorials-plex`. The TrueNAS Plex app mounts
  `/mnt/tank/data` as `/mnt/data`.

## Known limits

- Section names come from `<namedseason>` in `tvshow.nfo`. They show only
  with the library's advanced setting **Use season titles** on (tested on
  PMS 1.43.4). `season.nfo` is not read.
- `review.tsv` flags courses whose order can't be fully derived from names,
  so check those by hand:
  - `duplicate-lesson-numbers`
  - `mixed-numbered-and-unnumbered`
  - `unnumbered-section-folders`
  - `unnumbered-lesson-folders`
- 0-byte source files (failed downloads; 157 found on 10 Oct 2026) are
  linked, but Plex ignores them.
- Plex's legacy plugin/agent system is being retired, so this deliberately
  uses no plugin.

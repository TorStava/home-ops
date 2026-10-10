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

## Run (on mimir)

```sh
python3 plex_courses.py plan  /mnt/tank/data/tutorials --out /tmp/plan    # dry run: mapping.tsv + review.tsv
python3 plex_courses.py apply /mnt/tank/data/tutorials /mnt/tank/data/tutorials-plex --prune
python3 plex_courses.py apply ... --only 'SOLIDWORKS'   # pilot a few courses (no --prune)
```

Re-run `apply --prune` after adding, renaming or removing courses. It is
idempotent and only ever deletes inside the mirror.

## Plex library

- Type **TV Shows**, scanner **Plex TV Series**, agent **Plex NFO Series**
- Folder: `/mnt/data/tutorials-plex`. The TrueNAS Plex app mounts
  `/mnt/tank/data` as `/mnt/data`.

## Known limits

- Section names show as "Season N". Plex's NFO agent ignores `namedseason`
  and `season.nfo` (tested on PMS 1.43.4).
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

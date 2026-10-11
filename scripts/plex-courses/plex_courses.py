#!/usr/bin/env python3
"""Build a Plex "TV Shows" view of a video-course tree using hardlinks.

Each course becomes a show, each section folder a season, each lesson an
episode, numbered by natural sort ("2." before "10."). The original tree is
never modified: `apply` only creates hardlinks (same ZFS dataset required)
under DEST, plus a manifest and a tvshow.nfo per show.

  plan  SRC [--from-list FILE] [--out DIR]   dry run: mapping.tsv + review.tsv
  apply SRC DEST [--prune | --only REGEX]    create/refresh the hardlink tree
  posters DEST [--host HOST] [--force]       poster.jpg + fanart.jpg where missing

Stdlib only; plan/apply run on TrueNAS SCALE's python3. `posters` needs
ffmpeg, which TrueNAS lacks: run it elsewhere with --host to reach DEST.
"""
import argparse
import collections
import concurrent.futures
import functools
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

VIDEO = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg"}
SUBS = {".srt", ".vtt", ".ass", ".ssa"}
MANIFEST = ".plex-courses-manifest.tsv"
# Course cover art at the course root, linked into the show folder as poster.<ext>.
COVER = re.compile(r"^(poster|cover|folder|show)\.(jpe?g|png)$", re.I)

# Folders that only wrap the real content (torrent/site packaging).
WRAPPER = re.compile(r"^~?\s*get your (files|course)s? here", re.I)
# Junk that release groups put into names.
JUNK = re.compile(r"-*\s*\[\s*[\w.\- ]+\.(com|net|me|org|io|cc)\s*\]\s*-*|\s-\s*[\w-]+\.(com|net|me)\s*$|\s*-?\s*\[\s*(ftu|fco|tp|video)\s*\]", re.I)
SECTIONISH = re.compile(r"^\s*(\d|(section|module|part|week|day|chapter|lesson|lecture|unit|session|step|level|phase|ch|s)\s*[-_. ]?\d|(bonus|extras?|dvd|disc|cd)\b|dvdrip)", re.I)
# Bonus material plays after the main lessons, whatever its name sorts as.
BONUS = re.compile(r"^\s*(bonus|extras?)\b", re.I)
LEADNUM = re.compile(r"^\s*(\d+)")
STRIP_LEAD = re.compile(r"^\s*\d+(\.\d+)*\s*[-_.)\]:]*\s*")


def natkey(s):
    """Natural, case-insensitive sort key; numbers sort before text."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s.lower()) if t]


def pathkey(rel):
    return [(bool(BONUS.match(p)), natkey(p)) for p in rel.split("/")] if rel else []


def clean(name):
    name = JUNK.sub(" ", name)
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -_.~")
    return name or "Untitled"


def lesson_title(stem):
    t = clean(STRIP_LEAD.sub("", stem, count=1))
    return t[:150]


def build_tree(rel_files):
    """dir -> {'files': [names], 'dirs': set(child names)} for dirs that lead to videos."""
    tree = collections.defaultdict(lambda: {"files": [], "dirs": set()})
    for rel in rel_files:
        parts = rel.split("/")
        tree["/".join(parts[:-1])]["files"].append(parts[-1])
        for i in range(len(parts) - 1):
            tree["/".join(parts[:i])]["dirs"].add(parts[i])
    return tree


def join(a, b):
    return f"{a}/{b}" if a else b


def find_courses(tree, d, depth, out, label=None):
    node = tree[d]
    kids = sorted(node["dirs"], key=natkey)
    if depth == 1 and len(kids) >= 2 and sum(1 for k in kids if SECTIONISH.match(k)) * 2 >= len(kids):
        # a course sitting directly at the top ("How to play the drums/1 - Introduction/...")
        out.append((d, "course", label))
        return
    if depth <= 1:  # library root and subject folders
        if node["files"] and depth == 1:
            out.append((d, "loose", None))
        for k in kids:
            find_courses(tree, join(d, k), depth + 1, out)
        return
    if not node["files"] and len(kids) == 1 and not WRAPPER.search(kids[0]) and not SECTIONISH.match(kids[0]):
        # "Topic/<course>" or "<course>/Packs": keep the more descriptive name
        names = [clean(label or d.rsplit("/", 1)[-1]), clean(kids[0])]
        find_courses(tree, join(d, kids[0]), depth + 1, out, max(names, key=len))
        return
    if node["files"] or len(kids) <= 1:
        out.append((d, "course", label))
        return
    wrappers = [k for k in kids if WRAPPER.search(k)]
    numbered = sum(1 for k in kids if SECTIONISH.match(k))
    if wrappers or numbered * 2 >= len(kids):
        out.append((d, "course", label))
    else:  # a category such as "DevOps/Azure": each child is its own course
        for k in kids:
            find_courses(tree, join(d, k), depth + 1, out)


def plan_course(tree, cdir, kind):
    """Return (season_dirs, episodes, flags); episodes = [(season, ep, src_rel, title)]."""
    if kind == "loose":
        dirs = [cdir]
    else:
        dirs, stack = [], [cdir]
        while stack:
            d = stack.pop()
            if tree[d]["files"]:
                dirs.append(d)
            stack.extend(join(d, k) for k in tree[d]["dirs"])
        dirs.sort(key=lambda d: pathkey(d[len(cdir):].strip("/")))
    flags = set()
    # A folder holding a single lesson is a lesson, not a section: merge it
    # into its parent so the parent becomes the season.
    siblings = collections.defaultdict(list)
    for d in dirs:
        if d != cdir:
            siblings[d.rsplit("/", 1)[0]].append(len(tree[d]["files"]))
    lesson_parents = {p for p, n in siblings.items() if len(n) >= 4 and n.count(1) * 4 >= len(n) * 3}
    groups = collections.OrderedDict()
    for d in dirs:
        g = d
        # all lesson folders under such a parent, including the odd multi-video
        # one, form one season so "25 - x" (2 videos) still plays before "26 - y"
        if kind != "loose" and d != cdir and d.rsplit("/", 1)[0] in lesson_parents:
            g = d.rsplit("/", 1)[0]
            if not SECTIONISH.match(d.rsplit("/", 1)[1]):
                flags.add("unnumbered-lesson-folders")
        groups.setdefault(g, []).extend(join(d, f) for f in tree[d]["files"])
    dirs = sorted(groups, key=lambda d: pathkey(d[len(cdir):].strip("/")))
    eps = []
    for s, d in enumerate(dirs, 1):
        paths = sorted(groups[d], key=lambda p: pathkey(p[len(d):].strip("/")))
        files = [p[len(d):].strip("/") for p in paths]
        nums = [LEADNUM.match(f) for f in files]
        if any(nums) and not all(nums):
            flags.add("mixed-numbered-and-unnumbered")
        n = [int(m.group(1)) for m in nums if m]
        if len(n) != len(set(n)):
            flags.add("duplicate-lesson-numbers")
        for e, (pth, f) in enumerate(zip(paths, files), 1):
            eps.append((s, e, pth, lesson_title(os.path.splitext(os.path.basename(f))[0])))
    rels = [d[len(cdir):].strip("/") for d in dirs]
    if kind != "loose" and len(dirs) > 1 and any(r and not SECTIONISH.match(r.split("/")[-1]) for r in rels):
        flags.add("unnumbered-section-folders")
    if len(dirs) > 99:
        flags.add("over-99-seasons")
    return rels, eps, sorted(flags)


def show_name(cdir, kind, label, taken):
    parts = cdir.split("/")
    base = (label or clean(parts[-1])) if kind == "course" else f"{clean(parts[0])} (loose videos)"
    name = base if base not in taken else f"{base} ({clean(parts[0])})"
    i = 2
    while name in taken:
        name, i = f"{base} ({i})", i + 1
    taken.add(name)
    return name


def make_plan(rel_files):
    tree = build_tree(rel_files)
    courses = []
    find_courses(tree, "", 0, courses)
    taken, shows = set(), []
    for cdir, kind, label in courses:
        rels, eps, flags = plan_course(tree, cdir, kind)
        shows.append({"src": cdir, "kind": kind, "name": show_name(cdir, kind, label, taken),
                      "subject": cdir.split("/")[0], "seasons": rels, "eps": eps, "flags": flags})
    return shows


def dest_rel(show, s, e, title, ext):
    sw = 3 if len(show["seasons"]) > 99 else 2
    ew = max(2, len(str(max(x[1] for x in show["eps"]))))
    # Bare sNNeNN: dates in course names ("Updated 6-2022") derail Plex's
    # parser, and it silently skips files named like "...Sample..." or
    # "...-Video1". The lesson title comes from the episode .nfo instead.
    fname = f"s{s:0{sw}d}e{e:0{ew}d}{ext}"
    return f"{show['name']}/Season {s:0{sw}d}/{fname}"


def is_video(name, path=None):
    """By extension, except .ts: TypeScript (*.d.ts and friends) shares it with MPEG-TS."""
    ext = os.path.splitext(name)[1].lower()
    if ext not in VIDEO or name.startswith("._") or name.lower().endswith(".d.ts"):
        return False
    if ext == ".ts" and path:
        try:
            with open(path, "rb") as fh:
                return fh.read(1) == b"\x47"  # MPEG-TS sync byte
        except OSError:
            return False
    return True


def walk(src):
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if is_video(f, os.path.join(root, f)):
                out.append(os.path.relpath(os.path.join(root, f), src).replace(os.sep, "/"))
    return out


def course_cover(src, show):
    """A cover image at the course root (or inside its "Get your files here" wrapper)."""
    if show["kind"] != "course":
        return None
    base = os.path.join(src, show["src"])
    for d in [base] + [os.path.join(base, k) for k in sorted(os.listdir(base)) if WRAPPER.search(k)]:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if COVER.match(f) and os.path.isfile(os.path.join(d, f)):
                    return os.path.relpath(os.path.join(d, f), src)
    return None


def season_title(rel):
    parts = [clean(p) for p in rel.split("/") if p and not WRAPPER.search(p)]
    return " - ".join(parts) or "Course"


def tvshow_nfo(show):
    seasons = "".join(
        f'  <namedseason number="{i}">{escape(season_title(r))}</namedseason>\n'
        for i, r in enumerate(show["seasons"], 1))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<tvshow>\n'
            f'  <title>{escape(show["name"])}</title>\n  <genre>{escape(show["subject"])}</genre>\n'
            f'{seasons}</tvshow>\n')


def episode_nfo(title, s, e, rel):
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<episodedetails>\n'
            f'  <title>{escape(title)}</title>\n  <season>{s}</season>\n  <episode>{e}</episode>\n'
            f'  <plot>{escape(rel)}</plot>\n</episodedetails>\n')


def cmd_plan(a):
    files = [l.rstrip("\n").removeprefix("./") for l in open(a.from_list)] if a.from_list else walk(a.src)
    files = [f for f in files if is_video(os.path.basename(f))]
    shows = make_plan(files)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "mapping.tsv"), "w") as m:
        m.write("show\tseason\tepisode\tsource\ttarget\n")
        for sh in shows:
            for s, e, src, title in sh["eps"]:
                m.write(f"{sh['name']}\t{s}\t{e}\t{src}\t{dest_rel(sh, s, e, title, os.path.splitext(src)[1].lower())}\n")
    with open(os.path.join(a.out, "review.tsv"), "w") as r:
        r.write("show\tsource_folder\tseasons\tepisodes\tflags\n")
        for sh in shows:
            r.write(f"{sh['name']}\t{sh['src']}\t{len(sh['seasons'])}\t{len(sh['eps'])}\t{','.join(sh['flags'])}\n")
    flagged = collections.Counter(f for sh in shows for f in sh["flags"])
    print(f"videos: {len(files)}  shows: {len(shows)}  flagged shows: {sum(1 for s in shows if s['flags'])}")
    for f, n in flagged.most_common():
        print(f"  {f}: {n}")
    print(f"wrote {a.out}/mapping.tsv and {a.out}/review.tsv")


def cmd_apply(a):
    src, dest = os.path.abspath(a.src), os.path.abspath(a.dest)
    if dest.startswith(src + os.sep) or src.startswith(dest + os.sep):
        sys.exit("DEST must not be inside SRC (or vice versa)")
    if os.stat(src).st_dev != os.stat(os.path.dirname(dest.rstrip("/")) or "/").st_dev:
        sys.exit("SRC and DEST must be on the same filesystem/dataset for hardlinks")
    if a.only and a.prune:
        sys.exit("--prune cannot be combined with --only (it would remove every other course)")
    shows = make_plan(walk(src))
    if a.only:
        shows = [sh for sh in shows if re.search(a.only, sh["src"], re.I)]
    listdir = functools.lru_cache(maxsize=4096)(os.listdir)
    wanted, texts, keep, made, kept = {}, {}, set(), 0, 0
    for sh in shows:
        texts[f"{sh['name']}/tvshow.nfo"] = tvshow_nfo(sh)
        texts[f"{sh['name']}/.plexmatch"] = f"title: {sh['name']}\npattern: Season */s{{s}}e{{e}}.*\n"
        # Plex's NFO agent never fetches artwork: poster and background must be local files.
        keep.add(f"{sh['name']}/fanart.jpg")  # made by the `posters` command
        cover = course_cover(src, sh)
        if cover:
            wanted[f"{sh['name']}/poster{os.path.splitext(cover)[1].lower()}"] = cover
        else:
            keep.add(f"{sh['name']}/poster.jpg")  # made by the `posters` command
        for s, e, rel, title in sh["eps"]:
            base, ext = os.path.splitext(rel)
            wanted[dest_rel(sh, s, e, title, ext.lower())] = rel
            texts[dest_rel(sh, s, e, title, ".nfo")] = episode_nfo(title, s, e, rel)
            # sidecar subtitles: "<lesson>.srt" or "<lesson>.en.srt"
            sdir = os.path.join(src, os.path.dirname(rel))
            stem = os.path.basename(base)
            for f in listdir(sdir):
                fb, fe = os.path.splitext(f)
                if fe.lower() in SUBS and (fb == stem or fb.startswith(stem + ".")):
                    tgt = dest_rel(sh, s, e, title, fb[len(stem):] + fe.lower())
                    wanted[tgt] = os.path.join(os.path.dirname(rel), f)
    for tgt, rel in wanted.items():
        t, s = os.path.join(dest, tgt), os.path.join(src, rel)
        if os.path.exists(t):
            if os.path.samefile(t, s):
                kept += 1
                continue
            os.unlink(t)  # target slot now points at a different lesson
        os.makedirs(os.path.dirname(t), exist_ok=True)
        os.link(s, t)
        made += 1
    written = 0
    for tgt, body in texts.items():  # NFO metadata for the "Plex NFO Series" agent
        t = os.path.join(dest, tgt)
        try:
            with open(t) as fh:
                if fh.read() == body:
                    continue
        except FileNotFoundError:
            os.makedirs(os.path.dirname(t), exist_ok=True)
        with open(t, "w") as fh:
            fh.write(body)
        written += 1
    if not a.only:
        with open(os.path.join(dest, MANIFEST), "w") as m:
            m.writelines(f"{t}\t{r}\n" for t, r in sorted(wanted.items()))
    pruned = 0
    if a.prune:  # only ever removes files inside DEST that the plan no longer wants
        for root, dirs, files in os.walk(dest, topdown=False):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), dest).replace(os.sep, "/")
                if rel not in wanted and rel != MANIFEST and rel not in texts and rel not in keep:
                    os.unlink(os.path.join(root, f))
                    pruned += 1
            if root != dest and not os.listdir(root):
                os.rmdir(root)
    print(f"shows: {len(shows)}  links created: {made}  unchanged: {kept}  nfo written: {written}  pruned: {pruned}")
    print("run `posters` to make poster.jpg/fanart.jpg for new shows")


POSTER_W, POSTER_H = 1000, 1500
FANART_W, FANART_H = 1920, 1080
FONT = os.environ.get("PLEX_COURSES_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


class Dest:
    """The mirror, on this machine or on --host via ssh/sftp (TrueNAS has no ffmpeg)."""

    def __init__(self, path, host=None):
        self.path, self.host = path.rstrip("/"), host

    def ssh(self, cmd, data=None):
        return subprocess.run(["ssh", "-o", "BatchMode=yes", self.host, cmd], input=data,
                              capture_output=True, check=True).stdout

    def read(self, rel):
        p = os.path.join(self.path, rel)
        if self.host:
            return self.ssh(f"cat {shlex.quote(p)}").decode()
        with open(p) as fh:
            return fh.read()

    def having(self, art):
        """Show folders that already have <art>.* (poster, fanart)."""
        if self.host:
            out = self.ssh(f"find {shlex.quote(self.path)} -mindepth 2 -maxdepth 2 -iname '{art}.*' -printf '%P\\n'")
            return {l.split("/")[0] for l in out.decode().splitlines()}
        return {d for d in os.listdir(self.path) if os.path.isdir(os.path.join(self.path, d))
                and any(f.lower().startswith(art + ".") for f in os.listdir(os.path.join(self.path, d)))}

    def url(self, rel):
        p = os.path.join(self.path, rel)
        return f"sftp://{self.host}{p}" if self.host else p  # ffmpeg's sftp takes the raw path, not %-escapes

    def put(self, rel, data):
        p = os.path.join(self.path, rel)
        if self.host:
            self.ssh(f"cat > {shlex.quote(p + '.tmp')} && mv {shlex.quote(p + '.tmp')} {shlex.quote(p)}", data)
        else:
            with open(p + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(p + ".tmp", p)


def ffmpeg(args, out, timeout=300):
    """One still image: args are the inputs and filters, out the image file."""
    return subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-frames:v", "1", "-q:v", "3", out],
                          capture_output=True, timeout=timeout)


def grab_frame(lessons, out):
    """A representative frame from the first lesson that decodes, past its intro card."""
    for url in lessons:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", url],
                               capture_output=True, text=True, timeout=120)
        try:
            at = min(float(probe.stdout.strip()) * 0.2, 120)  # past the intro card, before the end
        except ValueError:
            continue
        if ffmpeg(["-ss", f"{at:.1f}", "-i", url, "-vf", "thumbnail=60"], out).returncode == 0 and os.path.getsize(out):
            return True
    return False


def render_fanart(frame, out):
    """The frame as 16:9 background art, fitted over a blurred fill so 4:3 lessons aren't cropped."""
    graph = (f"[0:v]split[a][b];[a]scale={FANART_W}:{FANART_H}:force_original_aspect_ratio=increase,"
             f"crop={FANART_W}:{FANART_H},gblur=sigma=40,eq=brightness=-0.3[bg];"
             f"[b]scale={FANART_W}:{FANART_H}:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    ffmpeg(["-i", frame, "-filter_complex", graph], out).check_returncode()


def render_poster(name, frame, out, tmp):
    """Lesson frame over a blurred copy of itself, course title below; a title card without a frame."""
    lines = textwrap.wrap(name, 20)
    size = 76
    if len(lines) > 4:
        lines, size = textwrap.wrap(name, 26), 56
    if len(lines) > 6:
        lines = lines[:5] + [lines[5][:24] + "…"]
    textfile = os.path.join(tmp, "title.txt")
    with open(textfile, "w") as fh:
        fh.write("\n".join(lines))
    text = (f"drawtext=fontfile={FONT}:textfile={textfile}:fontsize={size}:fontcolor=white:line_spacing=14:"
            f"text_align=C:x=(w-text_w)/2:shadowcolor=black@0.7:shadowx=3:shadowy=3")
    if frame:
        graph = (f"[0:v]split[a][b];"
                 f"[a]scale={POSTER_W}:{POSTER_H}:force_original_aspect_ratio=increase,crop={POSTER_W}:{POSTER_H},"
                 f"gblur=sigma=40,eq=brightness=-0.3[bg];[b]scale={POSTER_W - 80}:-2[fg];"
                 f"[bg][fg]overlay=(W-w)/2:220,{text}:y=880")
        ffmpeg(["-i", frame, "-filter_complex", graph], out).check_returncode()
        return "frame"
    ffmpeg(["-f", "lavfi", "-i", f"color=c=0x24324a:s={POSTER_W}x{POSTER_H}", "-vf", f"{text}:y=(h-text_h)/2"], out,
           timeout=60).check_returncode()
    return "title card"


def cmd_posters(a):
    dest = Dest(a.dest, a.host)
    shows = collections.defaultdict(list)
    for line in dest.read(MANIFEST).splitlines():
        tgt = line.split("\t")[0]
        if is_video(os.path.basename(tgt)):
            shows[tgt.split("/")[0]].append(tgt)
    shows = {s: t for s, t in shows.items() if not a.only or re.search(a.only, s, re.I)}
    poster_ok, fanart_ok = (set(), set()) if a.force else (dest.having("poster"), dest.having("fanart"))
    todo = sorted(s for s in shows if s not in poster_ok or s not in fanart_ok)
    print(f"shows: {len(shows)}  with poster: {len(poster_ok & set(shows))}  with fanart: "
          f"{len(fanart_ok & set(shows))}  to do: {len(todo)}", flush=True)

    def make(show):
        """Grab one lesson frame, then render whichever of poster/fanart the show lacks."""
        done = []
        with tempfile.TemporaryDirectory() as tmp:
            frame = os.path.join(tmp, "frame.png")
            if not grab_frame([dest.url(t) for t in sorted(shows[show], key=natkey)[:3]], frame):
                frame = None
            if show not in poster_ok:
                out = os.path.join(tmp, "poster.jpg")
                done.append(f"poster ({render_poster(show, frame, out, tmp)})")
                with open(out, "rb") as fh:
                    dest.put(f"{show}/poster.jpg", fh.read())
            if show not in fanart_ok:
                if not frame:
                    return done + ["no fanart (no lesson decodes)"]
                out = os.path.join(tmp, "fanart.jpg")
                render_fanart(frame, out)
                with open(out, "rb") as fh:
                    dest.put(f"{show}/fanart.jpg", fh.read())
                done.append("fanart")
        return done

    made, changed = collections.Counter(), []
    with concurrent.futures.ThreadPoolExecutor(a.jobs) as pool:
        for show, fut in zip(todo, [pool.submit(make, s) for s in todo]):
            try:
                done = fut.result()
                made.update(done)
                if any(d.startswith(("poster", "fanart")) for d in done):
                    changed.append(show)
            except Exception as e:  # one broken course must not stop the rest
                made["failed"] += 1
                print(f"  failed: {show}: {e}", file=sys.stderr)
    print("made: " + "  ".join(f"{k}: {v}" for k, v in made.items()))
    if a.plex:
        plex_update(a.plex, a.plex_section, changed)
    if made["failed"]:
        sys.exit(1)


def plex_update(url, section, changed):
    """Scan the library (new lessons, new shows), and reload art for shows Plex already has.

    A plain scan only reads artwork for shows it creates; a show that gets a
    new poster or fanart later needs a forced refresh of that show.
    """
    headers = {"X-Plex-Token": os.environ["PLEX_TOKEN"], "Accept": "application/xml"}

    def call(path, method="GET"):
        req = urllib.request.Request(url.rstrip("/") + path, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    keys = {d.get("title"): d.get("ratingKey")
            for d in ET.fromstring(call(f"/library/sections/{section}/all")).iter("Directory")}
    refreshed = [s for s in changed if s in keys]
    for show in refreshed:
        call(f"/library/metadata/{keys[show]}/refresh?force=1", "PUT")
    call(f"/library/sections/{section}/refresh")  # new shows are created with their art already in place
    print(f"plex: refreshed {len(refreshed)} shows, scan of section {section} started")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    pp = sp.add_parser("plan")
    pp.add_argument("src")
    pp.add_argument("--from-list", help="file of relative video paths (skips walking SRC)")
    pp.add_argument("--out", default=".")
    ap = sp.add_parser("apply")
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--only", help="regex on the course's source folder (pilot runs)")
    po = sp.add_parser("posters")
    po.add_argument("dest")
    po.add_argument("--host", help="DEST lives on this ssh host; ffmpeg reads it over sftp")
    po.add_argument("--force", action="store_true", help="remake posters and fanart that already exist")
    po.add_argument("--only", help="regex on the show name")
    po.add_argument("--jobs", type=int, default=4)
    po.add_argument("--plex", help="Plex URL: afterwards scan the library and reload changed shows (PLEX_TOKEN env)")
    po.add_argument("--plex-section", default="37", help="library section id of Courses")
    a = p.parse_args()
    {"plan": cmd_plan, "apply": cmd_apply, "posters": cmd_posters}[a.cmd](a)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a Plex "TV Shows" view of a video-course tree using hardlinks.

Each course becomes a show, each section folder a season, each lesson an
episode, numbered by natural sort ("2." before "10."). The original tree is
never modified: `apply` only creates hardlinks (same ZFS dataset required)
under DEST, plus a manifest and a tvshow.nfo per show.

  plan  SRC [--from-list FILE] [--out DIR]   dry run: mapping.tsv + review.tsv
  apply SRC DEST [--prune | --only REGEX]    create/refresh the hardlink tree

Stdlib only; runs on TrueNAS SCALE's python3.
"""
import argparse
import collections
import functools
import os
import re
import sys
from xml.sax.saxutils import escape

VIDEO = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg"}
SUBS = {".srt", ".vtt", ".ass", ".ssa"}
MANIFEST = ".plex-courses-manifest.tsv"

# Folders that only wrap the real content (torrent/site packaging).
WRAPPER = re.compile(r"^~?\s*get your (files|course)s? here", re.I)
# Junk that release groups put into names.
JUNK = re.compile(r"-*\s*\[\s*[\w.\- ]+\.(com|net|me|org|io|cc)\s*\]\s*-*|\s-\s*[\w-]+\.(com|net|me)\s*$|\s*-?\s*\[\s*(ftu|fco|tp|video)\s*\]", re.I)
SECTIONISH = re.compile(r"^\s*(\d|(section|module|part|week|day|chapter|lesson|lecture|unit|session|step|level|phase|ch|s)\s*[-_. ]?\d)", re.I)
LEADNUM = re.compile(r"^\s*(\d+)")
STRIP_LEAD = re.compile(r"^\s*\d+(\.\d+)*\s*[-_.)\]:]*\s*")


def natkey(s):
    """Natural, case-insensitive sort key; numbers sort before text."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s.lower()) if t]


def pathkey(rel):
    return [natkey(p) for p in rel.split("/")] if rel else []


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
    if depth <= 1:  # library root and subject folders are never courses
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
        if kind != "loose" and d != cdir and len(tree[d]["files"]) == 1 and d.rsplit("/", 1)[0] in lesson_parents:
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


def walk(src):
    out = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO and not f.startswith("._"):
                out.append(os.path.relpath(os.path.join(root, f), src).replace(os.sep, "/"))
    return out


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
    files = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO]
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
    wanted, texts, made, kept = {}, {}, 0, 0
    for sh in shows:
        texts[f"{sh['name']}/tvshow.nfo"] = tvshow_nfo(sh)
        texts[f"{sh['name']}/.plexmatch"] = f"title: {sh['name']}\npattern: Season */s{{s}}e{{e}}.*\n"
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
                if rel not in wanted and rel != MANIFEST and rel not in texts:
                    os.unlink(os.path.join(root, f))
                    pruned += 1
            if root != dest and not os.listdir(root):
                os.rmdir(root)
    print(f"shows: {len(shows)}  links created: {made}  unchanged: {kept}  nfo written: {written}  pruned: {pruned}")


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
    a = p.parse_args()
    cmd_plan(a) if a.cmd == "plan" else cmd_apply(a)


if __name__ == "__main__":
    main()

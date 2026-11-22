#!/usr/bin/env python3
"""rrtidy - import, sort, group and de-duplicate RapidRAW presets.

Steps, in order (1b only with --group):
  1b. Group (runs right after import): for each --group TEXT, move presets whose name
     contains TEXT into folder TEXT. Presets in a folder whose name is part of their own
     name are then left alone by the B&W sort, so grouping sticks on later runs.
  1. Import (optional, --import): read .rrpreset files, .zip files containing them,
     or directories of either. Presets whose settings already exist in the library
     are skipped; the rest go into an import folder.
  2. Sort: move every black & white preset into the B&W folder. A preset counts as
     B&W when its global saturation is -100 (or every HSL channel's saturation is -100)
     AND nothing applied afterwards puts colour back: R/G/B curves must match each other,
     colour-grading saturation must be 0, and no LUT is applied. Presets that desaturate
     then re-colour (e.g. duotones) are moved OUT of B&W to the import folder.
  3. De-duplicate: presets with the same name AND identical settings (ignoring id and
     presetType; missing includeMasks/includeCropTransform = false). The copy with
     the most metadata is kept.
  4. Number: same name with different settings -> 'Name', 'Name (2)', 'Name (3)'...

presets.json format (written by RapidRAW with serde_json pretty printing):
    [ {"preset": {...}},                                   top-level preset (wrapped)
      {"folder": {"id", "name", "children": [{...}, ...]}} folder; children are BARE ]
Folders cannot be nested. Unknown fields are preserved.

.rrpreset format: {"creator": "...", "presets": [ <same entries as presets.json> ]}

Dry run by default; --apply writes (a timestamped .bak is made first).
"""
import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
import shutil
from datetime import datetime
from pathlib import Path

DEFAULT_FILE = os.path.expanduser(
    "~/.local/share/io.github.CyberTimon.RapidRAW/presets/presets.json")
IGNORED_FIELDS = {"id", "presetType"}
MAX_PRESET_BYTES = 5 * 1024 * 1024   # refuse absurdly large .rrpreset files / zip members
ROOT = "(top level)"


# ----------------------------------------------------------------- preset helpers
def body(entry):
    """Preset dict from a top-level {"preset": {...}} entry or a bare folder child."""
    return entry.get("preset", entry)


def desaturated(adj):
    """Saturation removed: global -100, or every HSL channel -100."""
    if (adj.get("saturation") or 0) <= -100:
        return True
    hsl = adj.get("hsl") or {}
    sats = [ch.get("saturation", 0) for ch in hsl.values() if isinstance(ch, dict)]
    return bool(sats) and all(s <= -100 for s in sats)


def _curve(points):
    """Normalise a curve; missing/empty/straight line all become the identity."""
    pts = tuple((round(p.get("x", 0), 2), round(p.get("y", 0), 2))
                for p in (points or []) if isinstance(p, dict))
    return () if pts in ((), ((0, 0), (255, 255))) else pts


def tint_reasons(adj):
    """Things applied after desaturation that add colour back to a grey image."""
    reasons = []
    curves = adj.get("curves") or {}
    rgb = {c: _curve(curves.get(c)) for c in ("red", "green", "blue")}
    if len(set(rgb.values())) > 1:
        reasons.append("R/G/B curves differ")
    cg = adj.get("colorGrading") or {}
    if any(isinstance(cg.get(z), dict) and (cg[z].get("s") or 0) > 0
           for z in ("shadows", "midtones", "highlights")):
        reasons.append("colour grading")
    if adj.get("lutPath") and (adj.get("lutIntensity") or 0) > 0:
        reasons.append("LUT")
    return reasons


def is_bw(entry):
    adj = body(entry).get("adjustments") or {}
    return desaturated(adj) and not tint_reasons(adj)


def is_tinted_mono(entry):
    """Desaturated, then re-coloured (e.g. a duotone made with RGB curves)."""
    adj = body(entry).get("adjustments") or {}
    return desaturated(adj) and bool(tint_reasons(adj))


NUMBER_SUFFIX = re.compile(r" \((\d+)\)$")


def base_name(name):
    """Name without a ' (N)' suffix added by numbering, case-folded."""
    return NUMBER_SUFFIX.sub("", name).casefold()


def dup_key(entry):
    # Older presets omit these flags; absent means false. The ' (N)' suffix added
    # by number_variants is ignored so re-imports are still recognised.
    p = {"includeMasks": False, "includeCropTransform": False}
    p.update({k: v for k, v in body(entry).items() if k not in IGNORED_FIELDS})
    p["name"] = base_name(p.get("name", ""))
    return json.dumps(p, sort_keys=True)


def richness(entry):
    """More fields = newer/more complete copy; prefer keeping it."""
    return len(body(entry))


def is_preset(obj):
    return isinstance(obj, dict) and isinstance(obj.get("name"), str) \
        and isinstance(obj.get("adjustments"), dict)


class Library:
    """Folders as an ordered list of (name, [bare presets]); ROOT holds top-level presets.

    Keeps the original top-level order (folders vs loose presets) on output."""

    def __init__(self, data):
        if not isinstance(data, list):
            raise ValueError("presets.json: expected a JSON list")
        self.order = []          # list of ("preset", bare) or ("folder", folder_dict)
        for e in data:
            if isinstance(e, dict) and "folder" in e:
                e["folder"].setdefault("children", [])
                self.order.append(("folder", e["folder"]))
            elif isinstance(e, dict) and "preset" in e:
                self.order.append(("preset", e["preset"]))
            else:
                raise ValueError(f"presets.json: unrecognised entry {str(e)[:80]}")

    def folders(self):
        return [f for kind, f in self.order if kind == "folder"]

    def folder(self, name, create=True, log=None):
        for f in self.folders():
            if f["name"] == name:
                return f
        if not create:
            return None
        f = {"id": str(uuid.uuid4()), "name": name, "children": []}
        self.order.append(("folder", f))
        if log:
            log(f"create folder '{name}'")
        return f

    def groups(self):
        """[(label, list_of_bare_presets)] - the ROOT list is a fresh list."""
        return [(ROOT, [p for kind, p in self.order if kind == "preset"])] + \
               [(f["name"], f["children"]) for f in self.folders()]

    def set_root(self, presets):
        """Replace top-level presets: fill the existing top-level slots in order,
        drop unused slots, append any extras."""
        it = iter(presets)
        new_order = []
        for kind, x in self.order:
            if kind == "folder":
                new_order.append((kind, x))
            else:
                p = next(it, None)
                if p is not None:
                    new_order.append(("preset", p))
        new_order += [("preset", p) for p in it]
        self.order = new_order

    def to_data(self):
        return [{"folder": x} if kind == "folder" else {"preset": x} for kind, x in self.order]


# ------------------------------------------------------------------------ import
def _parse_rrpreset(raw, source, log):
    """Return [(folder_name_or_None, bare_preset)] from one .rrpreset payload."""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log(f"skip   {source}: not valid JSON ({e})")
        return []
    entries = doc.get("presets") if isinstance(doc, dict) else doc
    if isinstance(doc, dict) and entries is None and is_preset(doc):
        entries = [doc]                                   # a bare preset object
    if not isinstance(entries, list):
        log(f"skip   {source}: no 'presets' list")
        return []
    out = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("folder"), dict):
            fname = e["folder"].get("name") or "Imported folder"
            out += [(fname, body(c)) for c in e["folder"].get("children", []) if is_preset(body(c))]
        elif isinstance(e, dict) and is_preset(body(e)):
            out.append((None, body(e)))
        else:
            log(f"skip   {source}: unrecognised entry {str(e)[:60]}")
    return out


def read_import_sources(paths, log):
    """Yield (source_label, [(folder_name_or_None, preset)]) for every .rrpreset found."""
    for path in map(Path, paths):
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file()
                           and p.suffix.lower() in (".rrpreset", ".zip"))
            yield from read_import_sources(files, log)
        elif not path.is_file():
            log(f"skip   {path}: not found")
        elif path.suffix.lower() == ".zip":
            try:
                zf = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                log(f"skip   {path}: not a valid zip")
                continue
            with zf:
                members = [m for m in zf.infolist()
                           if not m.is_dir() and m.filename.lower().endswith(".rrpreset")]
                if not members:
                    log(f"skip   {path}: no .rrpreset files inside")
                for m in members:                      # read in memory; never extracted
                    label = f"{path.name}:{m.filename.rsplit('/', 1)[-1]}"
                    if m.file_size > MAX_PRESET_BYTES:
                        log(f"skip   {label}: too large ({m.file_size} bytes)")
                        continue
                    with zf.open(m) as fh:
                        raw = fh.read(MAX_PRESET_BYTES + 1)
                    if len(raw) > MAX_PRESET_BYTES:
                        log(f"skip   {label}: too large")
                        continue
                    yield label, _parse_rrpreset(raw, label, log)
        elif path.suffix.lower() == ".rrpreset":
            if path.stat().st_size > MAX_PRESET_BYTES:
                log(f"skip   {path}: too large")
                continue
            yield path.name, _parse_rrpreset(path.read_bytes(), path.name, log)
        else:
            log(f"skip   {path}: not a .rrpreset or .zip file")


def import_presets(lib, paths, dest_name, log):
    where = {}                                     # dup_key -> folder label it lives in
    for label, items in lib.groups():
        for p in items:
            where.setdefault(dup_key(p), label)
    count = 0
    for source, presets in read_import_sources(paths, log):
        for folder_name, p in presets:
            k = dup_key(p)
            if k in where:
                log(f"skip   '{p['name']}' from {source}: already in {where[k]}")
                continue
            p = copy.deepcopy(p)
            p["id"] = str(uuid.uuid4())            # fresh id avoids clashing with existing ones
            target = folder_name or dest_name
            lib.folder(target, log=log)["children"].append(p)
            where[k] = target
            count += 1
            log(f"import '{p['name']}' from {source} -> {target}")
    return count


# -------------------------------------------------------------- sort and dedupe
def in_named_folder(preset, folder):
    """True when the preset sits in a folder whose name is part of the preset's name,
    i.e. it was put there by --group (or deliberately by hand). Sorting leaves these."""
    return folder["name"].casefold() in preset["name"].casefold()


def group_by_string(lib, strings, log):
    """For each string: find/create a folder named after it and move every preset whose
    name contains it (case-insensitive) into that folder. First matching string wins."""
    targets = []                                    # [needle, folder or None (created on use), name]
    for s in strings:
        s = s.strip()
        if s and s.casefold() not in [t[0] for t in targets]:
            f = next((f for f in lib.folders() if f["name"].casefold() == s.casefold()), None)
            targets.append([s.casefold(), f, s])

    def wanted(p):
        name = p["name"].casefold()
        t = next((t for t in targets if t[0] in name), None)
        if t is None:
            return None
        if t[1] is None:
            t[1] = lib.folder(t[2], log=log)        # create only when something matches
        return t[1]

    for f in lib.folders():
        keep = []
        for p in f["children"]:
            dest = wanted(p)
            if dest is None or dest is f:
                keep.append(p)
            else:
                log(f"group  '{p['name']}'  {f['name']} -> {dest['name']}")
                dest["children"].append(p)
        f["children"] = keep
    roots = []
    for p in lib.groups()[0][1]:
        dest = wanted(p)
        if dest is None:
            roots.append(p)
        else:
            log(f"group  '{p['name']}'  {ROOT} -> {dest['name']}")
            dest["children"].append(p)
    lib.set_root(roots)
    for _, f, s in targets:
        if f is None:
            log(f"note   no presets contain '{s}' - no folder created")


def evict_tinted(lib, bw_name, dest_name, log):
    """Move desaturated-then-recoloured presets out of the B&W folder."""
    bw = lib.folder(bw_name, create=False)
    if bw is None or not any(is_tinted_mono(p) for p in bw["children"]):
        return
    dest = lib.folder(dest_name, log=log)
    keep = []
    for p in bw["children"]:
        if is_tinted_mono(p):
            why = ", ".join(tint_reasons(p.get("adjustments") or {}))
            log(f"move   '{p['name']}'  {bw_name} -> {dest_name}  (not B&W: {why})")
            dest["children"].append(p)
        else:
            keep.append(p)
    bw["children"] = keep


def sort_bw(lib, bw_name, log):
    def movable(p, f):
        return is_bw(p) and not in_named_folder(p, f)

    if not any(movable(p, f) for f in lib.folders() if f["name"] != bw_name
               for p in f["children"]) and not any(is_bw(p) for p in lib.groups()[0][1]):
        return                                      # nothing to move; don't create an empty folder
    bw = lib.folder(bw_name, log=log)
    for f in lib.folders():
        if f is bw:
            continue
        keep = []
        for p in f["children"]:
            if movable(p, f):
                log(f"move   '{p['name']}'  {f['name']} -> {bw_name}")
                bw["children"].append(p)
            else:
                keep.append(p)
        f["children"] = keep
    roots = lib.groups()[0][1]
    for p in roots:
        if is_bw(p):
            log(f"move   '{p['name']}'  {ROOT} -> {bw_name}")
            bw["children"].append(p)               # folder children are stored bare
    lib.set_root([p for p in roots if not is_bw(p)])


def _dedupe(groups, log):
    """groups: [(label, list)]. Returns new lists, same order as groups."""
    best = {}                                       # key -> (group idx, item idx, keeper)
    for gi, (_, items) in enumerate(groups):
        for ii, it in enumerate(items):
            k = dup_key(it)
            if k not in best:
                best[k] = (gi, ii, it)
            elif richness(it) > richness(best[k][2]):
                best[k] = (best[k][0], best[k][1], it)
    out = []
    for gi, (label, items) in enumerate(groups):
        new = []
        for ii, it in enumerate(items):
            bgi, bii, keeper = best[dup_key(it)]
            if (gi, ii) == (bgi, bii):
                new.append(keeper)
            else:
                log(f"remove '{it['name']}'  duplicate in {label} (kept in {groups[bgi][0]})")
        out.append(new)
    return out


def dedupe(lib, global_scope, log):
    groups = lib.groups()
    results = _dedupe(groups, log) if global_scope else [_dedupe([g], log)[0] for g in groups]
    for f, children in zip(lib.folders(), results[1:]):
        f["children"] = children
    lib.set_root(results[0])


def number_variants(lib, log):
    """Same name, different settings -> 'Name', 'Name (2)', 'Name (3)'...

    Each distinct-settings variant gets one name, shared by all its copies. A variant
    keeps its current name when that name is free, so repeated runs are stable."""
    groups = {}                     # base name -> {dup_key: [(folder label, preset), ...]}
    for label, items in lib.groups():
        for p in items:
            groups.setdefault(base_name(p["name"]), {}) \
                  .setdefault(dup_key(p), []).append((label, p))
    used = {p["name"].casefold() for _, items in lib.groups() for p in items}

    for variants in groups.values():
        if len(variants) < 2:
            continue
        taken, assigned, pending = set(), {}, []
        for k, copies in variants.items():          # pass 1: keep current names if free
            current = copies[0][1]["name"]
            if current.casefold() not in taken:
                taken.add(current.casefold())
                assigned[k] = current
            else:
                pending.append(k)
        for k in pending:                           # pass 2: next free number
            stem = NUMBER_SUFFIX.sub("", variants[k][0][1]["name"])
            n = 2
            while f"{stem} ({n})".casefold() in taken | used:
                n += 1
            assigned[k] = f"{stem} ({n})"
            taken.add(assigned[k].casefold())
        for k, copies in variants.items():
            for label, p in copies:
                if p["name"] != assigned[k]:
                    log(f"rename '{p['name']}' -> '{assigned[k]}'  in {label} (different settings)")
                    p["name"] = assigned[k]


# ------------------------------------------------------------------------ main
def summary(data):
    lines = []
    for e in data:
        if "folder" in e:
            lines.append(f"  {e['folder']['name']} ({len(e['folder']['children'])})")
        else:
            lines.append(f"  [top] {e['preset']['name']}")
    return "\n".join(lines) or "  (empty)"


def app_running():
    try:
        return subprocess.run(["pgrep", "-i", "-x", "rapidraw"],
                              capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def write_atomic(path, data):
    """Back up, then write in RapidRAW's format (2-space indent, UTF-8, no trailing newline)."""
    bak = None
    if os.path.exists(path):
        bak = f"{path}.{datetime.now():%Y%m%d-%H%M%S}.bak"
        shutil.copy2(path, bak)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=".presets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        if bak:
            os.chmod(tmp, os.stat(bak).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return bak


def main():
    ap = argparse.ArgumentParser(
        description="rrtidy: import, group, sort (B&W) and de-duplicate RapidRAW presets. Dry run by default.")
    ap.add_argument("-f", "--file", default=DEFAULT_FILE, help="presets.json path")
    ap.add_argument("-i", "--import", dest="imports", nargs="+", metavar="PATH", default=[],
                    help=".rrpreset files, .zip files containing them, or directories")
    ap.add_argument("--import-folder", default="Imported",
                    help="folder for imported presets that aren't in a folder (default: Imported)")
    ap.add_argument("-g", "--group", dest="groups", action="append", default=[], metavar="TEXT",
                    help="move presets whose name contains TEXT (case-insensitive) into a folder "
                         "named TEXT; repeatable, first match wins; takes precedence over B&W")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--global", dest="global_dedupe", action="store_true",
                    help="also remove duplicates across different folders")
    ap.add_argument("--bw-folder", default="B&W", help="name of the B&W folder (default: B&W)")
    ap.add_argument("--force", action="store_true", help="write even if RapidRAW is running")
    a = ap.parse_args()

    if os.path.exists(a.file):
        with open(a.file, encoding="utf-8") as fh:
            text = fh.read()
        data = json.loads(text) if text.strip() else []
    elif a.imports:
        data = []
    else:
        print(f"{a.file}: not found", file=sys.stderr)
        return 1

    changes = []
    log = changes.append
    try:
        lib = Library(copy.deepcopy(data))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if a.imports:
        import_presets(lib, a.imports, a.import_folder, log)
    if a.groups:
        group_by_string(lib, a.groups, log)
    sort_bw(lib, a.bw_folder, log)
    evict_tinted(lib, a.bw_folder, a.import_folder, log)
    dedupe(lib, a.global_dedupe, log)
    number_variants(lib, log)
    new = lib.to_data()

    print("Before:\n" + summary(data))
    print("\nChanges:")
    print("\n".join("  " + c for c in changes) if changes else "  (none)")
    print("\nAfter:\n" + summary(new))

    if not a.apply:
        print("\nDry run - nothing written. Re-run with --apply to save.")
        return 0
    if new == data:
        print("\nNothing to change.")
        return 0
    if app_running() and not a.force:
        print("\nRapidRAW is running - close it first (or use --force).", file=sys.stderr)
        return 1
    bak = write_atomic(a.file, new)
    print(f"\nWritten.{' Backup: ' + bak if bak else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

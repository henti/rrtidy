# rrtidy

`rrtidy.py` imports, groups, sorts and de-duplicates RapidRAW presets
(`~/.local/share/io.github.CyberTimon.RapidRAW/presets/presets.json`). It runs these steps in order:

1. **Import** (optional, `-i`): reads `.rrpreset` files, `.zip` files that contain them, or directories
   (searched recursively) holding either.
   - A preset whose settings already exist anywhere in the library is skipped.
   - Presets that came in a folder export keep that folder's name. Everything else goes into
     `Imported` (change it with `--import-folder`).
   - Each imported preset gets a new `id`, so it can't clash with an existing one.
   - Zips are read in memory and never extracted. Only `*.rrpreset` members are read, up to 5 MB each.
     Invalid JSON is reported and skipped.
   **Group** (optional, `-g TEXT`, repeatable): moves every preset whose name contains `TEXT`
   (case-insensitive) into a folder named `TEXT`, exactly as you typed it. An existing folder with that name,
   in any capitalisation, is reused.
   - The folder is only created if something matches.
   - If a preset matches several `-g` strings, the first one given wins.
   - **Grouping beats B&W.** The B&W sort leaves alone any preset in a folder whose name is part of the
     preset's name, so `AVOR - Dynamic Black and White` stays in `AVOR` on later runs.
   - Grouping copies from different folders puts them in one folder, so the dedupe step then keeps one copy of each.
2. **Sort B&W:** moves every black and white preset into the `B&W` folder, creating it only if needed.
   A preset counts as B&W if it **removes colour** (global saturation -100, or every HSL channel's
   saturation -100) **and nothing adds colour back afterwards**:
   - the red, green and blue curves are identical to each other (a missing curve or a straight line counts as unchanged),
   - colour-grading saturation (`s`) is 0 for shadows, midtones and highlights,
   - no LUT is applied.

   A preset that desaturates and then re-colours the image is a duotone, not B&W. One example is
   `Chromostereopsis - Blue`, which turns the grey image blue and green with its R/G/B curves.
   Any such preset already in `B&W` is moved out to the import folder (`Imported` by default).
   Presets you put in B&W by hand that just don't match the rule are left alone.
3. **Remove duplicates:** two presets are duplicates if they have the same name and identical settings.
   `id` and `presetType` are ignored, and missing `includeMasks`/`includeCropTransform` flags count as `false`.
   The copy with the most metadata is kept.
4. **Number name clashes:** when the same name is used for presets with *different* settings, each
   settings variant gets its own name: `Name`, `Name (2)`, `Name (3)`… Brackets are used so the result
   doesn't clash with names like `Monochrome 2`.
   - Identical copies in different folders share one name.
   - A variant keeps its current name when that name is free, so re-running doesn't reshuffle numbers.
   - Names are compared case-insensitively, and a number is never reused if any preset already has that name.
   - Duplicate detection ignores a ` (N)` suffix, so re-importing the original pack is recognised as
     already present instead of creating `Name (3)`.

Python 3 standard library only, so there is nothing to install.

## Usage

```sh
./rrtidy.py                                   # dry run: show what would change
./rrtidy.py --apply                           # write changes (timestamped .bak made first)
./rrtidy.py -i ~/Downloads/pack.zip           # preview an import
./rrtidy.py -i a.rrpreset b.rrpreset ~/presets-dir --apply
./rrtidy.py -i pack.zip --import-folder "Film" --apply
./rrtidy.py -g AVOR -g Zinearama --apply      # folders "AVOR" and "Zinearama" with matching presets
./rrtidy.py --global --apply                  # also remove duplicates ACROSS folders
./rrtidy.py -f path/to/presets.json --bw-folder "Mono"
```

- By default, duplicates are only removed within the same folder. With `--global`, a preset that
  appears in two folders is kept only in the folder listed first in the file.
- `--apply` refuses to run while RapidRAW is open, because the app can save its in-memory
  presets over the file. `--force` overrides this.
- Output matches RapidRAW's own format (2-space indent, UTF-8, no trailing newline), and the write is atomic.
- Rollback: copy the `presets.json.<timestamp>.bak` over `presets.json` with RapidRAW closed.

## File formats

`presets.json` is a list. Top-level presets are wrapped as `{"preset": {...}}`. Folders are
`{"folder": {"id", "name", "children": [...]}}`, and their children are **bare** preset objects
(no wrapper). Folders can't be nested.

`.rrpreset` is `{"creator": "...", "presets": [...]}`, where the list uses the same entry shapes as `presets.json`.

## Testing (Docker)

```sh
mkdir -p /tmp/rbw && cp ~/.local/share/io.github.CyberTimon.RapidRAW/presets/presets.json /tmp/rbw/
docker run --rm -v "$PWD":/app:ro -v /tmp/rbw:/data python:3.12-slim \
  python -I /app/rrtidy.py -f /data/presets.json -i /data/some.zip
```

## Run as `rrtidy`

To call it as just `rrtidy`, link it into a directory on your `PATH`:

```sh
ln -s ~/src/RapidBW/rrtidy.py ~/.local/bin/rrtidy
```

## How it was built

rrtidy was written with [Claude Code](https://claude.com/claude-code), Anthropic's AI coding assistant.
I directed the work: I decided what the tool should do, reviewed each step, and spotted where it went wrong.

Issues and PRs are welcome.
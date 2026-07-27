# XCSoar + MapLibre hybrid basemap — proof of concept

Status: **Part 1 (mapgen) only.** Parts 2 (Linux/OpenGL runtime embedding)
and 3 (Android/OpenGL) are designed for but not implemented here.

## The hybrid principle

MapLibre is used for exactly one thing: painting the pretty picture behind
everything else. It never touches:

- terrain height queries (glide computer, arrival heights, final glide) —
  stays on XCSoar's own `RasterTerrain` / `terrain.jp2`
- topology overlays (roads, water, towns) used for on-screen decisions —
  stays on `topology.tpl` + shapefiles
- airspace, waypoints, task, traffic, FLARM, glide computer overlays —
  entirely unchanged

MapLibre's vector/raster tiles are a **second, independent, purely visual**
data source. XCSoar's own overlay renderer draws on top of it exactly as it
draws today on top of the terrain shading. If the MapLibre layer is absent,
disabled, or fails to load, XCSoar falls back to today's rendering with zero
behavioural change — this is intentionally a strict *addition*, not a
replacement.

```
┌───────────────────────────────────────────┐
│  XCSoar overlays (unchanged)               │  topology, airspace, task,
│  glide computer, traffic, ...              │  waypoints, FLARM — vector
├───────────────────────────────────────────┤  draw calls straight into
│  XCSoar terrain shading (unchanged,        │  XCSoar's existing OpenGL
│  optional if MapLibre hillshade is used)   │  canvas, as today
├───────────────────────────────────────────┤
│  MapLibre basemap texture (NEW)            │  rendered off-screen by
│  vector roads/land-use/water + hillshade   │  MapLibre Native's headless
└───────────────────────────────────────────┘  frontend, blitted as one
                                                 textured quad
```

### Why "headless render → texture", not "hand MapLibre the GL context"

MapLibre Native owns a lot of GL/Metal/Vulkan state internally once it's
driving a context (shaders, VAOs, its own render-to-texture passes for
tile compositing). Interleaving that with XCSoar's own immediate-mode-ish
OpenGL canvas is fragile and hard to keep correct across driver/version
combinations. MapLibre Native already ships a **headless frontend** used
by its own `mbgl-render` CLI tool and by desktop integrations such as
`maplibre-native-slint`: it renders a frame off-screen into its own FBO
and hands back a plain RGBA buffer in CPU memory. XCSoar can upload that
buffer as one OpenGL texture and draw it as the bottom-most quad, the same
way it would draw any other cached bitmap layer. This keeps the two GL
worlds fully decoupled — the tradeoff is a CPU readback per redraw, which
is fine for a base map that only needs to refresh on pan/zoom/rotate, not
every animation frame.

This also happens to be the same shape of integration Part 2/3 need on
Android (JNI around the same headless C++ core, or the Android AAR's
offscreen mode), so validating it on Linux first is the right order, as
you specified.

## Offline-only constraint

Nothing above ever makes a network call at XCSoar runtime:

- tiles are pre-baked at map-generation time into a `.mbtiles` file
  (SQLite, single file, trivially copyable to a device)
- the style, sprite sheet and glyph ranges are files inside the bundle,
  referenced by relative path — no `https://` URLs anywhere in `style.json`
- MapLibre Native's tile requests are resolved locally. For this first
  milestone that means either (a) a flat `{z}/{x}/{y}` file tree read via
  the stock `file://` resource loader (zero new C++, works today), or
  (b) a small custom `FileSource` registered for a `mbtiles://` scheme
  that reads tile blobs straight out of the SQLite file (the standard
  community pattern for shipping compact single-file offline tile packs,
  and the one this PoC's mapgen output is shaped for). (b) is Part 2 work;
  Part 1 produces both representations so you can start previewing
  immediately without waiting for that code — see
  `docs/GENERATE_TEST_BUNDLE.md`.

## Part 1 — what's in this folder

| Path | Purpose |
|---|---|
| `mapgen_patch/lib/xcsoar/mapgen/maplibre.py` | new mapgen module: builds the offline MapLibre bundle for one map job |
| `mapgen_patch/bin/generate-maps.diff` | wires `--maplibre` into the existing batch-build script (verified against the real file) |
| `mapgen_patch/bin/mapgen-cli.md` | shape of the equivalent change to the interactive `bin/mapgen` CLI (illustrative — see note inside) |
| `style/style.json.tmpl` | the MapLibre style template written into every bundle |
| `hypsometric-restyle/` | how the terrain got elevation colour *and* kept its relief detail: measured colour ramp, hillshade tuning, and the render-compare tooling |
| `scripts/build_test_area.sh` | orchestrates a full test build for a small French Alps area |
| `docs/DATA_SOURCES.md` | where to freely get high-resolution elevation + imagery + vector data for the French Alps (and elsewhere) |
| `docs/GENERATE_TEST_BUNDLE.md` | step-by-step: produce and sanity-check a real test bundle |

## What mapgen already does (for context)

mapgen (https://github.com/XCSoar/mapgen) is a Python 3 tool that builds
`.xcm` map files for XCSoar. `.xcm` is a zip archive containing:

- `terrain.jp2` + `terrain.j2w` — JPEG2000 DEM raster + world file, built
  from SRTM by default (`lib/xcsoar/mapgen/terrain/srtm.py`)
- `topology.tpl` + a set of shapefiles — vector topology (coastlines,
  rivers, roads, towns) built from VMAP0/OSM by a separate companion tool,
  [xcsoar-mapgen-topology](https://github.com/XCSoar/xcsoar-mapgen-topology)
- waypoints (Welt2000/SeeYou `.cup` derived) and airspace (OpenAir)
- `info.txt` describing the map's extent, author, creation time

The batch driver (`bin/generate-maps`) builds one map like this:

```python
generator = Generator(dir_data=dir_data, dir_temp=dir_temp)
generator.set_bounds(GeoRect(left, right, top, bottom))
generator.add_information_file(output_file, author="the XCSoar team")
generator.add_welt2000()
generator.add_topology(level_of_detail=level_of_detail)
generator.add_terrain(resolution)
generator.create(output_file)
generator.cleanup()
```

This PoC adds one more optional call, `generator.add_maplibre(...)`,
guarded by a flag that defaults to off — every existing `.xcm` consumer
(current XCSoar releases, other tools that read `.xcm`) is unaffected,
since it's an additive folder inside the same zip that nothing currently
looks for.

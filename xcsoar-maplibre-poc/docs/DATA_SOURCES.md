# Free data sources for a French Alps test bundle

The MapLibre bundle needs two kinds of raw data: **elevation** (for the
hillshade raster layer) and **vector features** (roads, water, land use,
places — for the vector tile layer). Below is what's freely available for
France/the Alps, roughly ordered from "best fit for this PoC" downward.

## Elevation (DEM)

### Sonny's LiDAR DTMs — https://sonny.4lima.de/ (recommended for this PoC)

Sonny compiles freely downloadable LiDAR-derived Digital Terrain Models for
most of Europe, resampled into convenient one-click downloads. For France
this includes a **1-arcsecond** and **3-arcsecond** version distributed as
`.hgt` files using the same `<n|s>NN<e|w>NNN.hgt` naming and tiling SRTM
uses, plus 20 m and 50 m GeoTIFF versions. Because the source is airborne
laser scanning rather than a satellite radar/optical pass, it is
substantially more accurate than SRTM/ASTER/Copernicus in exactly the
terrain a French-Alps test area will have: steep rock faces, forested
slopes, and narrow valleys — all places SRTM is known to be noisy or to
have radar shadow gaps. The gap-filled 1"/3" tiles are a **drop-in
replacement** for the `.hgt` tiles mapgen's `srtm.py` downloader already
fetches, so pointing mapgen's terrain step at Sonny's tiles instead of (or
merged with) plain SRTM improves `terrain.jp2` itself, not just the
MapLibre hillshade.

- License: CC BY 4.0 — attribution to "Sonny" and a link to the site is
  required in any redistribution.
- Practical note: the download links on the site are Google Drive folders,
  not directly `wget`-able; a human needs to click through once per
  country/resolution to grab the zip(s) covering the test area, then those
  files go into mapgen's local data cache for reuse across jobs. If that
  manual step is the problem, see viewfinderpanoramas.org below — near
  identical data over the Alps, and scriptable.
- The site's own comparison page links example LiDAR-vs-SRTM renders if
  you want to see the accuracy difference before committing to a data set.

### viewfinderpanoramas.org — https://viewfinderpanoramas.org/dem3.html (the scriptable 1" source)

Jonathan de Ferranti's DEMs, void-filled from topographic mapping and, for
Europe, rebuilt from the same national LiDAR releases Sonny draws on. The
1" set is the same `<n|s>NN<e|w>NNN.hgt` tiling as Sonny's, so it is a
drop-in for `data/dem/` too — but unlike Sonny's Google Drive folders it
sits at plain URLs, which is the whole reason to care about it.

**It is downloadable by script.** Tiles are packed into 4°×6° zips named
by a latitude band letter (A = 0–4°, B = 4–8°, …) plus a UTM-style
longitude zone (1 = 180°W–174°W), with an `S` prefix in the southern
hemisphere: `https://viewfinderpanoramas.org/dem1/L32.zip`. Two traps —
the `S` prefix collides with band S (72–76°N), told apart by letter count
(`S19` northern, `SK59` southern); and a southern cell's SW corner sits on
the far edge of its band, so `S44E170` is in `SK59`, not `SL59`. The zip's
inner folder drops the prefix (`SK59.zip` holds `K59/`).
`bin/mapgen-dem1-fetch` implements all of this — give it a bbox and it
fetches, extracts and caches only the cells you need.

Coverage is **not** global: roughly Europe (to about Ukraine), North
America, Japan, New Zealand and the Himalaya. 309 zips, ~25 GB for the
whole 1" archive, ~80 MB each. Blocks outside coverage return 404, and
seven links on the coverage map are dead (`H11 M01 N01`–`N05`).

Quality, measured against Sonny on the four overlapping Alpine cells: mean
difference under 1 m, std 1.7–4.3 m, 92–98 % of pixels within 5 m. It is
genuinely native 1", not upsampled 3" — a period-3 resampling signature
test scores ~1.0 (as Sonny does) against 0.03 for a real 3"→1" upsample.
For anywhere Sonny does not cover (Japan, New Zealand, North America)
this is the only free 1" `.hgt` option, and it tests as native there too.

The one real difference: **water is void** (`-32768`), where Sonny is
void-free — about 1.5 % of a valley tile, essentially zero in high alpine
terrain. Both DEM consumers mishandle that, the hillshade badly (the
Terrain-RGB packer encodes a void as −10 000 m and box-blurs it into the
surrounding terrain first, so every lake becomes a crater), so
`mapgen-dem1-fetch` interpolates the voids away before caching unless you
pass `--no-fill`. Filled tiles match Sonny to std ~6 m with 92 % of pixels
within 5 m — the same agreement as on pixels neither source voided.

- Terms of use: custom, not a standard open license. Data "may be
  reproduced for research and private use"; redistribution requires "an
  acknowledgement with a link to the appropriate source page" or written
  permission; "limited commercial use is OK, but anyone contemplating
  large scale reproduction should contact me". Since mapgen bakes DEM data
  into every `.xcm` it ships, that acknowledgement is not optional — and
  note the terms call out flight-simulator mesh distribution specifically,
  which is close enough to XCSoar's use to be worth reading directly if
  these maps are published rather than built locally.

### IGN France (Institut national de l'information géographique et
forestière) — official French mapping agency, open data

- **RGE ALTI** — 1 m and 5 m resolution DEM derived from the same
  national LiDAR HD campaign, covering all of metropolitan France. This is
  the highest-resolution free elevation data available for a French test
  area (finer than Sonny's, which deliberately keeps file sizes down).
  Free under Licence Ouverte / Etalab 2.0 (no attribution restriction
  beyond crediting IGN), downloadable via the IGN Géoplateforme /
  data.gouv.fr open-data portals by department or by tile.
- **BD ALTI** — 25 m DEM, much smaller download, fine for a country- or
  region-scale hillshade if 1 m/5 m tiles are more than the test needs.
- **BD ORTHO** — ~20 cm resolution aerial orthophotography. Not needed for
  the vector-tile approach this PoC uses, but worth knowing about if a
  later experiment wants a raster-imagery style instead of/blended with
  the vector basemap.
- **BD TOPO / BD FORET** — official vector reference layers (roads,
  buildings, forest cover, hydrography) as an alternative or supplement to
  OSM specifically for France, generally higher positional accuracy than
  OSM in rural/mountain areas.

### Copernicus GLO-30 DEM (ESA/Copernicus)

Global 30 m DEM, free via the Copernicus Data Space Ecosystem or
OpenTopography. Coarser than Sonny's or IGN's offerings for France, but
useful as a consistent fallback for any part of a map job that crosses
into a country neither of those cover, or for large-area builds where
30 m is precise enough.

## Vector features (roads, land use, water, places)

### OpenStreetMap via Geofabrik — https://download.geofabrik.de/

Geofabrik publishes daily-refreshed regional `.osm.pbf` extracts, free, no
account needed. For the Alps test area, `europe/france/rhone-alpes-latest.osm.pbf`
(or `auvergne-rhone-alpes`, depending on Geofabrik's current region
boundaries — check the France sub-index) is the right starting extract:
download it once into mapgen's shared data cache, then clip it per-job
with `osmium extract` down to each map's bounding box (this mirrors how
mapgen already treats SRTM/DEM tiles as a shared cache rather than a
per-job download). This is the same data mapgen's existing topology step
already draws on via
[xcsoar-mapgen-topology](https://github.com/XCSoar/xcsoar-mapgen-topology),
so no new licensing surface is introduced — OSM's ODbL already applies to
the shapefiles mapgen produces today.

## Sprites and glyphs (style assets)

These are job-independent — built once, reused by every map:

- **Glyphs** (PBF glyph ranges for the fonts referenced by `style.json`):
  generate offline from any TTF/OTF font with a glyph-pbf generator such
  as `node-fontnik` (used by TileServer GL/Klokantech tooling), or reuse
  the pre-built glyph sets shipped by open style repos such as
  OpenMapTiles' reference styles (OFL-licensed fonts, e.g. Noto Sans).
- **Sprites** (the icon sheet `style.json` points `"sprite"` at): build
  offline with `spreet` (MIT-licensed Rust CLI) from an SVG icon set such
  as Maki (CC0), or again reuse a prebuilt sprite sheet from an open style
  repo.

## Suggested test area: Chamonix–Mont-Blanc

A compact bounding box around the Chamonix valley / Mont Blanc massif is a
good PoC target: dramatic relief that makes the LiDAR-vs-SRTM difference
obvious, small enough to build in minutes, and well covered by every
source above.

```
left   = 6.72
right  = 7.05
top    = 46.05
bottom = 45.85
```

(this is the `left right top bottom` order mapgen's `-b` flag already
expects)

## License summary

| Source | License | Attribution needed |
|---|---|---|
| Sonny's LiDAR DTMs | CC BY 4.0 | Yes — "Sonny", link to sonny.4lima.de |
| viewfinderpanoramas.org | Custom site terms of use (not CC) | Yes — credit Jonathan de Ferranti with a link to the source page; contact him before large-scale redistribution |
| IGN RGE ALTI / BD ALTI / BD ORTHO / BD TOPO | Licence Ouverte / Etalab 2.0 | Credit IGN |
| Copernicus GLO-30 | Copernicus / ESA open data license | Credit Copernicus |
| OpenStreetMap (via Geofabrik) | ODbL | Credit OpenStreetMap contributors (already required today for mapgen's topology output) |
| Maki icons | CC0 | None required |
| Noto Sans (or other OFL fonts) | SIL OFL 1.1 | Per OFL terms if redistributing the font itself |

Whatever mix you pick, carry the attribution text into the bundle (e.g. an
`attribution` field in `style.json`'s sources, and a line in the mapgen
job's `info.txt`) so it survives the round trip into the `.xcm` file.

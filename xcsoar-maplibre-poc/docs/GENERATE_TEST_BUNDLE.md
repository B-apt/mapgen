# Generating the French Alps test bundle, step by step

Target area: Chamonix–Mont-Blanc (`left=6.72 right=7.05 top=46.05 bottom=45.85`)
— see `docs/DATA_SOURCES.md` for why this bbox.

Status: **verified end-to-end** against the real mapgen repo, running inside
the `mapgen-worker` Docker container (see `docker-compose.yml`). All
commands below run through `docker compose`, matching how this project
actually builds maps - there is no host-side install path anymore.

## 0. Dependencies (already baked into the worker image)

`container/worker/Dockerfile` installs everything the MapLibre bundle
needs: `osmium-tool`, `gdal-bin` + `python3-gdal` (for `gdal2tiles.py`),
`sqlite3`, `numpy`/`rasterio` (pip), a `planetiler` wrapper script backed
by a standalone Temurin 21 JRE (planetiler 0.10.x needs Java 21 - bullseye's
`default-jre` is only Java 11 and fails with
`UnsupportedClassVersionError`), and `spreet` (prebuilt musl binary) for
sprite sheets. Rebuild it after pulling these changes:

```bash
docker compose build mapgen-worker
```

No glyph generator (e.g. node-fontnik) is installed - prebuilt, OFL-licensed
glyph PBFs are reused from the `openmaptiles/fonts` release instead (see
step 2). `lib/` and `bin/` are bind-mounted into the container, so Python
changes there are picked up immediately without a rebuild; only
Dockerfile/dependency changes need `docker compose build`.

## 1. Fetch the raw sources (once, into the shared data cache)

The data volume (`mapgen-data`) is bind-mounted from
`/home/my-user/.xcsoar_mapgen/mapgen-data` and appears as `/opt/mapgen/data`
inside the container - this is the same cache `add_terrain()`'s SRTM
downloader already uses, just with a few new subdirectories:

```
data/osm/<name>.osm.pbf         Geofabrik regional extract (any single
                                 *.osm.pbf directly under data/osm/ is
                                 auto-detected; name it region.osm.pbf if
                                 you keep more than one around)
data/dem/<NAME>.hgt             Sonny's LiDAR DTM, uppercase names
                                 (N45E006.hgt) - preferred hillshade source
data/dem3/<name>.hgt            existing SRTM cache (lowercase), used both
                                 by add_terrain() and as the MapLibre
                                 hillshade's fallback where Sonny has no
                                 coverage
data/planetiler-sources/        3 small *global* files the default
                                 Planetiler OpenMapTiles profile needs
                                 (lake centerlines, split water polygons,
                                 Natural Earth) - one-time fetch, see below
```

`data/osm` and `data/dem`/`data/dem3` are operator-maintained exactly like
before - see `docs/DATA_SOURCES.md` for where to get them (Geofabrik,
sonny.4lima.de). Sonny's download links are Google Drive folders, not
`wget`-able, so that one is a manual step.

`data/planetiler-sources/` is new and is fetched once with planetiler
itself (`--download`, requires network - point Java at your proxy if
needed):

```bash
docker compose run --rm --no-deps --entrypoint bash mapgen-worker -c '
  mkdir -p /opt/mapgen/data/planetiler-sources
  /opt/java21/bin/java -Xmx4g \
    -Dhttp.proxyHost=<proxy-host> -Dhttp.proxyPort=<proxy-port> \
    -Dhttps.proxyHost=<proxy-host> -Dhttps.proxyPort=<proxy-port> \
    -jar /usr/local/bin/planetiler.jar \
    --osm-path=/opt/mapgen/data/osm/<your-extract>.osm.pbf \
    --output=/tmp/throwaway.mbtiles --force --download \
    --lake_centerlines_path=/opt/mapgen/data/planetiler-sources/lake_centerline.shp.zip \
    --water_polygons_path=/opt/mapgen/data/planetiler-sources/water-polygons-split-3857.zip \
    --natural_earth_path=/opt/mapgen/data/planetiler-sources/natural_earth_vector.sqlite.zip
'
```

(omit the `-Dhttp*.proxy*` flags if you don't need one - Java does **not**
read the `http_proxy`/`https_proxy` env vars the way `curl`/`wget` do, so
without them this specific one-time step silently times out even though
the container has working network access otherwise.) After this, every
normal job run passes `--download=false` (planetiler's default) and never
touches the network - same caching philosophy as the OSM/DEM tiles.

## 2. Build the one-time static assets (once, into `data/maplibre-static/`)

Identical for every job, so built once directly into the shared data cache
(no docker-compose changes needed - it's just another subdirectory of the
volume that's already mounted):

```bash
# style template
cp style/style.json.tmpl /home/my-user/.xcsoar_mapgen/mapgen-data/maplibre-static/style.json.tmpl

# glyphs: prebuilt, OFL-licensed PBF ranges (generated ahead of time with
# node-fontnik by the openmaptiles/fonts project) - avoids needing a glyph
# generator toolchain in the image at all
curl -L -o noto-sans.zip https://github.com/openmaptiles/fonts/releases/download/v2.0/noto-sans.zip
unzip -j noto-sans.zip 'Noto Sans Regular/*' -d /home/my-user/.xcsoar_mapgen/mapgen-data/maplibre-static/glyphs/'Noto Sans Regular'

# sprites: CC0 Maki icon set, built with spreet (already in the image)
curl -L -o maki.zip https://github.com/mapbox/maki/archive/refs/heads/main.zip
unzip -j maki.zip 'maki-main/icons/*.svg' -d /tmp/maki_icons
docker compose run --rm --no-deps -v /tmp/maki_icons:/workspace/maki_icons:ro \
  --entrypoint bash mapgen-worker -c '
    spreet /workspace/maki_icons /opt/mapgen/data/maplibre-static/sprites/sprite
    spreet --ratio 2 /workspace/maki_icons /opt/mapgen/data/maplibre-static/sprites/sprite@2x
  '
```

`bin/mapgen --maplibre-static` defaults to `<data>/maplibre-static`, so
nothing else needs pointing at this directory.

## 3. Build the test bundle

The changes described in the main README are already applied directly to
`lib/xcsoar/mapgen/generator.py`, `lib/xcsoar/mapgen/maplibre.py`,
`bin/mapgen` and `bin/generate-maps` in this checkout - there is no
separate patch-application step anymore. Run the CLI **through the worker
container**, overriding its `mapgen-worker` entrypoint (the image's default
entrypoint is the job-queue daemon, not the one-shot CLI):

```bash
docker compose run --rm --no-deps --entrypoint /opt/mapgen/bin/mapgen \
  -v "$PWD/out:/workspace" mapgen-worker \
  -r 3 -l 3 -b 6.72 7.05 46.05 45.85 --maplibre /workspace/chamonix_test.xcm
```

This produces `out/chamonix_test.xcm` and prints progress for each stage.
You should see the existing components unchanged (`terrain.jp2`,
`terrain.j2w`, `topology.tpl`, `info.txt`, the topology shapefiles) plus a
new `maplibre/` folder containing:

```
maplibre/style.json
maplibre/basemap.mbtiles
maplibre/hillshade.mbtiles
maplibre/sprites/
maplibre/glyphs/
maplibre/tiles/basemap/{z}/{x}/{y}.pbf     (flat preview copy, gunzipped)
maplibre/tiles/hillshade/{z}/{x}/{y}.png   (flat preview copy)
```

Add `-w2` for welt2000 waypoints if you want a more complete bundle - it's
untouched by any of this and was already working. Omit `--maplibre`
entirely to confirm the output is the same 65-entry flat file set as
before (no `maplibre/` folder at all) - this is the "off by default" the
main README requires.

## 4. Validate the bundle

No `mbgl-render` prebuilt binary or Debian package exists for Linux (only
static `.a` core libs meant to be linked into a C++ app - see the
project's GitHub releases), and building the full MapLibre Native C++
project from source just for a one-off sanity check was judged not worth
the time/weight it would add. Instead, `scripts/validate_bundle.py`
performs structural validation using tools already in the worker image:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/out_unzipped/maplibre:/workspace/maplibre:ro" \
  -v "$PWD/xcsoar-maplibre-poc/scripts/validate_bundle.py:/workspace/validate_bundle.py:ro" \
  --entrypoint bash mapgen-worker -c \
  'cd /workspace && python3 validate_bundle.py maplibre 6.72 7.05 46.05 45.85'
```

It checks: style.json is spec-shaped (version 8, valid layer types, no
dangling source references, no `http(s)://` URLs anywhere - the offline
constraint) and that every `source-layer` it references actually exists in
`basemap.mbtiles`; both `.mbtiles` files open through GDAL/OGR's native
MBTiles driver (exercises real MVT/PNG decoding, not just SQLite
structure); the flat `tiles/` tree's file count and format matches the
mbtiles content; and the sprite/glyph assets are well-formed. On the real
Chamonix build this passes with 0 failures, 0 warnings.

For an actual rendered visual check in a browser (no build step, uses
MapLibre GL JS from a CDN), copy `scripts/preview.html` into the unzipped
bundle next to `style.json`, serve that directory (`python3 -m http.server
8080`), and open `http://localhost:8080/preview.html` - it rewrites the
bundle's `mbtiles://` source URLs to the flat `tiles/{z}/{x}/{y}` tree on
the fly and renders the real style.json, layers, sprite and glyphs
unmodified. See the comment at the top of that file for details.

If you instead want to test against MapLibre Native specifically (e.g.
once Part 2/3 embeds it in XCSoar itself and a real build of the library
exists on your machine), point `mbgl-render --style style.json` at a copy
of `style.json` with the `sources[*].url` fields rewritten the same way,
to `file://.../tiles/basemap/{z}/{x}/{y}.pbf` and
`file://.../tiles/hillshade/{z}/{x}/{y}.png`.

## 5. What "success" looks like for Part 1

- `chamonix_test.xcm` opens in current XCSoar exactly as before (terrain,
  topology, waypoints, airspace all render identically) - confirmed by
  `create()` only adding an extra top-level zip entry, never touching the
  existing file list or archive names when `--maplibre` is off.
- The `maplibre/` folder inside it structurally validates as a real,
  internally-consistent, fully offline MapLibre bundle for the Chamonix
  area (553 vector tiles / 136k OSM features, 447 hillshade tiles,
  correctly geo-referenced to the requested bbox).
- The hillshade layer picked up Sonny's LiDAR DTM tiles where available
  (12 of 16 needed tiles in this run) and fell back to the existing SRTM
  cache for the rest (4 tiles) - both caches coexist without either
  affecting how `terrain.jp2` itself is built.

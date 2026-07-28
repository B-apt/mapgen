# Map generator for XCSoar

[![Codacy Badge](https://api.codacy.com/project/badge/Grade/099ae14d05e6426d82080c5e1074e38c)](https://app.codacy.com/gh/XCSoar/mapgen?utm_source=github.com&utm_medium=referral&utm_content=XCSoar/mapgen&utm_campaign=Badge_Grade_Settings)

This generates maps in the xcm format for [XCSoar](https://xcsoar.org/) a tactical
Gliding computer.

The Maps are layered out of a multitude of sources:

* terrain SRTM
* topology VMAP0
* Roads and Towns OSM
* Waypoints CUP format
* Airspaces OPENAIR format

## Deployment and Development

### Frontend

The frontend container contains the cherrypy based service and an nginx based
reverse proxy for exposing the mapgen on port 9090 Both processes in the
frontend container are started by supervisord.

Frontend produces job files that are put into a shared volume

```bash
/opt/mapgen/jobs/<jobid>.queued
```

### Worker

This is the actual map builder, that takes the queued jobs in
/opt/mapgen/jobs/jobid and starts processing all the *.queued jobs.

### Volumes

These are named volumes inside your docker service.

```bash
/opt/mapgen/jobs:
```

 This is the job directory where all jobs get stored

```bash
/opt/mapgen/data:
```

 This directory caches all the data from the data repository. WARNING: This
 volume can take up a lot of space (100GB).

 The `mapgen-data`/`mapgen-jobs` volumes declared in `docker-compose.yml` are
 bind-mounted to real host directories (via `driver_opts: device: ...`)
 rather than plain Docker-managed volumes, so the multi-GB caches survive
 image rebuilds and stay easy to inspect/back up directly from the host.
 **Before your first `docker compose build`/`up`**, edit the two `device:`
 paths under the `volumes:` section at the bottom of `docker-compose.yml` to
 point at directories on your own machine, e.g.:

 ```bash
 mkdir -p ~/.xcsoar_mapgen/mapgen-data ~/.xcsoar_mapgen/mapgen-jobs
 ```

 then set `device: /home/<you>/.xcsoar_mapgen/mapgen-data` and
 `device: /home/<you>/.xcsoar_mapgen/mapgen-jobs` accordingly (bind mounts
 need a literal absolute path - `~` is not expanded here). The directory
 must exist before `docker compose` tries to mount it.

### Ports

```bash
Port 9090
```

### Build Variables

The Following build variables can be set during build (optional):

1. GITURL: The git url for the mapgen sources
2. GITBRANCH: The branch name

#### Building

in the current directory:

```bash
docker-compose build
```

or with options:

```bash
docker-compose build \
--build-arg=GITURL=https://github.com/myuser/mapgen/mapgen.git \
--build-arg=GITBRANCH=myfeature
```

#### Starting

```bash
docker-compose up -d
```

### Optional: MapLibre visual-basemap bundle

Every generated map can optionally include an additive, fully offline
MapLibre vector+hillshade basemap bundle alongside the normal terrain/
topology/waypoints/airspace (tick "Include an offline MapLibre visual
basemap" on the web form, or pass `--maplibre` to `bin/mapgen`). This is
purely a decorative background layer for future client-side rendering; it
never changes how terrain/topology/waypoints/airspace themselves are
built, and is off by default.

`container/worker/Dockerfile` installs everything the *build process*
needs (osmium-tool, GDAL, a bundled Java 21 runtime + planetiler, spreet)
plus the static style assets - `docker compose build` picks that up
automatically. The bulk map *data* is too large to ship in an image, so
it is fetched into the data volume on demand. Steps 1-3 below therefore
happen by themselves; only step 4 is manual, and it is optional.

**1. A regional OSM extract** - *automatic.* The
vector basemap layer (roads, water, land use, place labels) is cut from
[Geofabrik](https://download.geofabrik.de/) regional extracts, and the
first job that needs one works out which region(s) cover its bounding box
and downloads them into `data/osm/geofabrik/`. Later jobs over the same
area reuse the cache.

Region choice is derived from the job's own bounds, so a map that
straddles a border pulls each side and merges them - a French/Swiss Alps
box gets `rhone-alpes` + `switzerland` rather than the 2.2 GB `alps`
extract that also covers it, or the 33 GB `europe` one. Three settings in
`lib/xcsoar/mapgen/server/config.py` bound this:

| | |
|---|---|
| `maplibre_osm_max_download_bytes` | ceiling on *new* data per job (default 3 GB). Cached regions don't count. |
| `maplibre_allow_downloads` | set `False` for an air-gapped worker (covers the Planetiler sources in step 2 too) |
| `maplibre_osm_max_age_days` | re-download cached extracts after N days (default `0` = never) |

A job needing more than the limit - a bbox over half a continent, or one
that is mostly open sea - **skips the MapLibre bundle and records why in
the provenance report**; terrain, topology, waypoints and airspace still
build normally.

`bin/mapgen-osm-cache` inspects and pre-populates the cache:

```bash
# what would this bbox pull? (no download; same -b order as bin/mapgen)
bin/mapgen-osm-cache select -b 5.28 6.99 46.13 43.78
# fetch ahead of time, off the critical path
bin/mapgen-osm-cache prefetch europe/france/rhone-alpes
bin/mapgen-osm-cache list
```

For an air-gapped worker, put the extract at the path `select` reports
under `data/osm/geofabrik/` and it is used as-is.

**2. Planetiler's global auxiliary sources** - *automatic* 
Its OpenMapTiles profile needs three global datasets
(water polygons, lake centerlines, Natural Earth) to render coastlines
and lakes at low zoom, regardless of which area you build. The first job
that finds them missing fetches them into `data/planetiler-sources/`
(~1.4 GB, one-time); every later job reuses them, and only whichever
files are actually absent get downloaded.

Planetiler does the fetching itself, so the file versions stay matched to
the bundled jar - those URLs are pinned inside it and change between
releases. `maplibre_allow_downloads: False` turns this off along with the
OSM extracts, for an air-gapped worker.

**3. Static style assets** - *automatic.* The sprite sheet (CC0 Maki
icons), the font glyphs (prebuilt OFL Noto Sans PBF ranges) and
`xcsoar-maplibre-poc/style/style.json.tmpl` are identical for every job
and only ~35MB, so `docker compose build` bakes them into the worker
image at `/opt/mapgen/maplibre-static/`.

The style template lives in the source tree and is baked in as the
image's *last* layer, so editing it and rebuilding costs a ~30kB layer
rather than re-running the downloads above. `bin/mapgen --maplibre-static
DIR` searches `DIR` first if you want to try a variant without a
rebuild; it resolves per asset, so a directory holding only a style
template still gets its glyphs and sprites from the image.

**4. (Optional, better hillshade quality for France) Sonny's LiDAR DTM** -
see `xcsoar-maplibre-poc/docs/DATA_SOURCES.md`. The download links are
Google Drive folders and can't be scripted; unzip the `.hgt` tiles into
`~/.xcsoar_mapgen/mapgen-data/dem/`. This is genuinely optional: without
it, everything still builds from plain SRTM (`data/dem3/`), which mapgen
auto-downloads per-tile as needed - you lose some elevation accuracy over
steep terrain, not functionality.

Note that this data is **not** used unless a job asks for it: choose
"Maximum" on the web form, or pass `--dem-arcsec 1`. See "Elevation data
resolution" below for why it is opt-in.

`docker compose up -d` and the checkbox on the web form (or `bin/mapgen
--maplibre`) works with no setup beyond the volumes; the first bundle a
worker builds is slower while it populates its caches. See
`xcsoar-maplibre-poc/README.md` for the design rationale.

### Elevation data resolution

Two independent things used to be conflated under one "resolution"
setting. They are now separate, because they are separate:

| | what it controls | values |
|---|---|---|
| **source tier** (`--dem-arcsec`) | which elevation dataset is *read* | `1`, `3`, `auto` |
| **output spacing** (`-r`) | the grid `terrain.jp2` is *written* on | `9`, `3` |

The old "High resolution terrain (3 arcseconds per pixel instead of 9)"
checkbox was only ever the second one - it resampled the same 3-arcsec
source onto a finer output grid. It never fetched better data.

The web form offers the sensible combinations:

| form option | source tier | `terrain.jp2` |
|---|---|---|
| Standard (default) | 3 arcsec | 9 arcsec |
| High | 3 arcsec | 3 arcsec |
| Maximum | 1 arcsec | 3 arcsec |

**The two tiers are not equally available.** `data/dem3/` (3 arcsec) is
global and auto-downloaded. `data/dem/` (1 arcsec) is curated by hand,
covers only part of Europe, and *cannot* be downloaded - so a 1-arcsec
request will routinely hit cells that have no such data. That is what the
missing-data policy decides:

* `fallback` (default) - fill those cells from the 3-arcsec tier, mark
  them downgraded, carry on.
* `fail` - stop, naming exactly which 1-degree cells fell short.

The default is 3 arcsec rather than "best available" so that the same
request produces the same data on any worker, regardless of which
1-arcsec tiles happen to be cached there.

Resolution also sets how deep the MapLibre hillshade is tiled: **z12 for
3-arcsec source data, z14 for 1-arcsec**. Baking deeper than the source
supports adds visible terracing, not detail. The server-side
`maplibre_max_zoom` in `lib/xcsoar/mapgen/server/config.py` is a separate
ceiling on top of that.

## How To

### Knowing what a map was actually built from

Every generated `.xcm` now contains a `dem_provenance.txt` (and
`maplibre/PROVENANCE.txt` for the bundle) recording what was really read,
not what was requested:

```
=== DEM provenance (MapLibre hillshade) ===
requested: 1 arcsec     policy: fallback
used:      1 arcsec  x1   (data/dem, manual)
used:      3 arcsec  x1   (data/dem3, DOWNGRADED: N43E008)
missing:   none
hillshade: z0-12   (capped by: source resolution (3"), server config maplibre_max_zoom=14 allowed more)
smoothing: 2x 3x3 box on the Mercator-resampled grid
coverage:  5.372,44.461 -> 7.292,46.381  (= requested, not clipped)
```

The `hillshade:` line names *which* constraint set the zoom. Without it,
a bundle capping at z12 is ambiguous between "the DEM is coarse" and "a
config value said so" - an ambiguity that has already caused one
misdiagnosis, and which cost two zoom levels of real detail on
1-arcsec-sourced bundles until it was found.


### Test suite

Tests are in the folder `tests`. Run the test suite with:

```bash
python3 -m unittest discover -s tests -v
```


### HTTP proxy

In case you are executing behind an HTTP proxy: 
**Java does not read `http_proxy`/`https_proxy`**, so a wrapper script:
`container/worker/planetiler` now translates whatever proxy variables are
in the environment into the `-D` system properties Java actually honours.

Put the values in a **`.env` file next to `docker-compose.yml`** - it is
git-ignored, and `docker compose` reads it automatically for both the
build and the running container:

```bash
cat > .env <<'EOF'
http_proxy=http://proxy.example.com:8080/
https_proxy=http://proxy.example.com:8080/
no_proxy=localhost,127.0.0.1
EOF
```

Note: Without a `.env` these resolve to empty strings and everything behaves 
normally.

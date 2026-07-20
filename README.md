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

`container/worker/Dockerfile` already installs everything the *build
process* needs (osmium-tool, GDAL, a bundled Java 21 runtime + planetiler,
spreet) - `docker compose build` picks that up automatically. What it
can't do for you is fetch the underlying map *data*, which is too large to
ship in the image. Do this once, after the volumes above are set up:

**1. A regional OSM extract**, for the vector basemap layer (roads, water,
land use, place labels) - pick whichever [Geofabrik](https://download.geofabrik.de/)
region(s) cover the areas you'll actually generate maps for:

```bash
mkdir -p ~/.xcsoar_mapgen/mapgen-data/osm
curl -L -o ~/.xcsoar_mapgen/mapgen-data/osm/region.osm.pbf \
  https://download.geofabrik.de/europe/france/rhone-alpes-latest.osm.pbf
```

(`region.osm.pbf` is checked first; any single `*.osm.pbf` directly under
`data/osm/` is also auto-detected if you'd rather name it after the region)

**2. Planetiler's global auxiliary sources** (water polygons, lake
centerlines, Natural Earth - required by its OpenMapTiles profile
regardless of which area you build, ~1.4GB, one-time):

```bash
mkdir -p ~/.xcsoar_mapgen/mapgen-data/planetiler-sources
docker compose run --rm --no-deps --entrypoint bash mapgen-worker -c '
  /opt/java21/bin/java -Xmx4g -jar /usr/local/bin/planetiler.jar \
    --osm-path=/opt/mapgen/data/osm/region.osm.pbf \
    --output=/tmp/throwaway.mbtiles --force --download \
    --lake_centerlines_path=/opt/mapgen/data/planetiler-sources/lake_centerline.shp.zip \
    --water_polygons_path=/opt/mapgen/data/planetiler-sources/water-polygons-split-3857.zip \
    --natural_earth_path=/opt/mapgen/data/planetiler-sources/natural_earth_vector.sqlite.zip
'
```

(add `-Dhttp.proxyHost=<host> -Dhttp.proxyPort=<port>` and the `https`
equivalent right after `-Xmx4g` if you're behind a proxy - Java does not
read the `http_proxy`/`https_proxy` env vars the way curl/wget do)

**3. Static style assets** (sprite sheet + font glyphs, identical for every
job, built once):

```bash
mkdir -p ~/.xcsoar_mapgen/mapgen-data/maplibre-static/{sprites,glyphs}
cp xcsoar-maplibre-poc/style/style.json.tmpl \
  ~/.xcsoar_mapgen/mapgen-data/maplibre-static/

curl -L -o /tmp/noto-sans.zip \
  https://github.com/openmaptiles/fonts/releases/download/v2.0/noto-sans.zip
unzip -j /tmp/noto-sans.zip 'Noto Sans Regular/*' \
  -d ~/.xcsoar_mapgen/mapgen-data/maplibre-static/glyphs/'Noto Sans Regular'

curl -L -o /tmp/maki.zip \
  https://github.com/mapbox/maki/archive/refs/heads/main.zip
unzip -j /tmp/maki.zip 'maki-main/icons/*.svg' -d /tmp/maki_icons
docker compose run --rm --no-deps -v /tmp/maki_icons:/workspace/maki_icons:ro \
  --entrypoint bash mapgen-worker -c '
    spreet /workspace/maki_icons /opt/mapgen/data/maplibre-static/sprites/sprite
    spreet --ratio 2 /workspace/maki_icons /opt/mapgen/data/maplibre-static/sprites/sprite@2x
  '
```

**4. (Optional, better hillshade quality for France) Sonny's LiDAR DTM** -
see `xcsoar-maplibre-poc/docs/DATA_SOURCES.md`. The download links are
Google Drive folders and can't be scripted; unzip the `.hgt` tiles into
`~/.xcsoar_mapgen/mapgen-data/dem/`. This is genuinely optional: without
it, the hillshade layer automatically falls back to plain SRTM
(`data/dem3/`), which mapgen already auto-downloads per-tile as needed -
you lose some elevation accuracy over steep terrain, not functionality. If
a requested map's bounds have no elevation data available at all, the
MapLibre bundle is silently skipped (the rest of the map still builds
normally); if bounds are only partially covered, the hillshade layer is
clipped to whatever coverage exists.

Once steps 1-3 are done, `docker compose up -d` and the checkbox on the
web form (or `bin/mapgen --maplibre`) works. See
`xcsoar-maplibre-poc/README.md` for the design rationale and
`xcsoar-maplibre-poc/docs/GENERATE_TEST_BUNDLE.md` for a full walkthrough
with a validation script.

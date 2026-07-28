# -*- coding: utf-8 -*-
mapgen = {
    "domain": "mapgen.xcsoar.org",
    "protocol": "http",
    # Upper ceiling for web-triggered --maplibre jobs - a runaway guard on
    # the vector-tile layer for a large web-drawn bbox (e.g. most of the
    # French Alps is 50-100x the area of a small manual test area, and the
    # worker processes one job at a time), NOT the hillshade's real limit.
    #
    # The hillshade is capped independently, and normally lower, by
    # maplibre.__hillshade_max_zoom(), which derives its cutoff from the
    # resolution of the DEM tiles actually used: z12 for 3-arcsec data,
    # z14 for 1-arcsec. That is the cap that should govern sharpness,
    # because it is the one tied to what the source data can actually
    # support.
    #
    # This was previously a flat 12. For a 3-arcsec-sourced bundle that
    # coincides exactly with the resolution-derived cap, so it looked
    # harmless; for a 1-arcsec-sourced one it silently overrode z14 and
    # cost two zoom levels of real detail the data could support - with
    # nothing in the output recording which of the two constraints had
    # won. See the DEM provenance report for that.
    "maplibre_max_zoom": 14,
    # --- OSM extract auto-download (see osm_extracts.py) ----------------
    #
    # The regional .osm.pbf the vector basemap is cut from is chosen from
    # the job's own bounds and fetched on first use. These three knobs
    # bound that.
    #
    # Ceiling on how much NEW extract data one job may download. Regions
    # already in data/osm/geofabrik/ do not count towards it, so a worker
    # settles down to zero downloads once its usual areas are cached. A
    # job over this limit skips the MapLibre bundle and says why in the
    # provenance report; the rest of the map still builds.
    #
    # 3 GB is roughly "one large country, or several regions" - enough for
    # any plausible gliding map, far short of a continent (europe-latest
    # alone is ~30 GB, and drawing a box over it on the web form is a
    # single mouse gesture).
    "maplibre_osm_max_download_bytes": 3 * 1024**3,
    # Set False for an air-gapped worker. Covers both the OSM extracts and
    # Planetiler's ~1.4 GB auxiliary sources: they must then be pre-placed
    # under data/osm/geofabrik/ (see bin/mapgen-osm-cache) and
    # data/planetiler-sources/, and a job needing anything else skips the
    # bundle instead of reaching out. One switch rather than two because
    # an air-gapped worker needs both off, and no deployment sensibly
    # wants one without the other.
    "maplibre_allow_downloads": True,
    # Days after which a cached extract is re-downloaded. 0 = never, which
    # is the right default: Geofabrik rebuilds every "-latest" file daily,
    # and re-pulling hundreds of MB to chase that would cost far more than
    # the staleness is worth on a decorative basemap layer.
    "maplibre_osm_max_age_days": 0,
}

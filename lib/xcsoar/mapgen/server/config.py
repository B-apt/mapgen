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
}

# -*- coding: utf-8 -*-
mapgen = {
    "domain": "mapgen.xcsoar.org",
    "protocol": "http",
    # Web-triggered --maplibre jobs cap zoom here rather than the CLI's
    # default of 14 - a large web-drawn bbox (e.g. most of the French
    # Alps) is 50-100x the area of a small manual test area, and the
    # worker processes one job at a time, so a lower zoom keeps tile
    # counts/turnaround reasonable for everyone else in the queue.
    "maplibre_max_zoom": 12,
}

# -*- coding: utf-8 -*-
#
# On-demand OSM extract cache for the MapLibre vector basemap layer.
#
# Before this module, the regional .osm.pbf was an operator prerequisite:
# someone picked a Geofabrik region by hand, downloaded it into
# data/osm/, and every job clipped whatever single file happened to be
# there. That had two failure modes, both silent:
#
#   * a job whose bbox fell outside the operator's chosen region still
#     "succeeded", producing a bundle with empty vector tiles - the
#     extract clipped to zero features and nothing complained;
#   * setting up a new worker meant reading the README and guessing which
#     region covered the areas that worker would be asked for.
#
# So region choice is derived from the job's own bounds instead, against
# Geofabrik's published region index, and the extract is fetched on first
# use into a managed cache. Jobs still never touch the network for data
# that is already cached - the download happens once per region, not once
# per job, exactly like data/dem3 and the Planetiler auxiliary sources.
#
# Layout under the shared data cache:
#
#     data/osm/geofabrik-index-v1.json           the region index
#     data/osm/geofabrik-index-v1.json.meta      etag / fetch time / sizes
#     data/osm/geofabrik/<geofabrik/path>-latest.osm.pbf
#     data/osm/geofabrik/<geofabrik/path>-latest.osm.pbf.meta.json
#     data/.locks/<region>.lock                  cross-process download lock
#
# The cache path deliberately mirrors Geofabrik's own URL layout, so what
# is on disk can be read off against download.geofabrik.de without a
# lookup table. A pre-placed .osm.pbf at one of those paths is accepted
# with no sidecar and no network access at all - that is the offline /
# air-gapped path.

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from xcsoar.mapgen.util import FileLock

GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1.json"

# Ceiling on how much *new* data one job may pull. A bbox drawn across
# half a continent on the web form can otherwise select several multi-GB
# extracts and tie up the single-threaded worker for hours. Regions
# already in the cache do not count against this - see __download_bytes().
DEFAULT_MAX_DOWNLOAD_BYTES = 3 * 1024**3

# Geofabrik rebuilds every "-latest" extract daily. Re-pulling hundreds of
# MB per job to chase that would cost far more than the staleness is worth
# on a decorative basemap layer, so cached extracts are never refreshed on
# age unless an operator asks for it.
DEFAULT_EXTRACT_MAX_AGE_DAYS = 0

# The index itself is small (~4 MB) and does change - regions get split
# and renamed - so it is refreshed on age, with a conditional GET so the
# usual case costs one 304.
DEFAULT_INDEX_MAX_AGE_DAYS = 30

# A candidate region must cover at least this fraction of the bbox to be
# worth downloading at all. Without it, a bbox that overlaps a neighbouring
# country by a few hundred metres of border pulls that country's entire
# extract for a sliver of map nobody will look at.
_CONTRIBUTION_FLOOR = 0.005

# Stop selecting once this fraction of the bbox is covered. The remainder
# is normally sea, which no land extract will ever cover - without a
# tolerance the greedy loop below would keep buying regions trying to
# cover open water.
_COVERAGE_TOLERANCE = 0.995

# There is deliberately NO "this region costs too much for what it
# covers" rule here, and the absence is load-bearing enough to be worth
# recording, because it is an easy and attractive thing to add back.
#
# The motivating case is real: Geofabrik's polygons include territorial
# waters, so for a bbox that is half open sea, `europe` genuinely does
# cover ground no country extract does, and the loop below will select a
# 33 GB continental extract to reach it. Two shapes of ceiling were tried
# to stop that, and both were worse than the problem:
#
#   * cost per FRACTION OF BBOX covered scales with the size of the
#     request, so on a large bbox every genuinely useful region
#     contributes only a small percentage and is rejected. A box drawn
#     over half of Europe selected Macedonia and Albania - 79 MB, no
#     error, silently useless. Silent wrong answers are the worst
#     failure available here.
#   * cost per SQUARE DEGREE of overlap breaks in the opposite direction:
#     an extract is a fixed cost regardless of how little of it a job
#     uses, so a small bbox - the commonest case by far - makes every
#     region look extortionate and nothing is selected at all.
#
# Neither had a threshold defensible on anything but the example that
# motivated it. So the job is left to the two guards that are honest
# about what they are: the greedy score below, which already strongly
# prefers cheap regional extracts, and max_download_bytes, which refuses
# loudly and names what it refused. A mostly-ocean or continental bbox
# therefore fails with an actionable message rather than quietly
# building from the wrong data, and every realistic gliding map - the
# French Alps, cross-border, a whole country - selects regional extracts
# well inside the limit.

# Effective cost, in bytes, of a region that is already in the cache.
# Not zero, so that between two already-cached regions the loop still has
# something to order by; small enough that any cached region always beats
# any download. This is what makes the selection reuse whatever an
# operator has already fetched rather than picking a marginally
# better-fitting region and downloading it again.
_CACHED_COST_BYTES = 1

# Used when a HEAD gives no Content-Length. Ranks the region below every
# region whose size is known, without excluding it outright - it may be
# the only thing covering part of the bbox.
_UNKNOWN_SIZE_BYTES = 64 * 1024**3

_CMD_OSMIUM = "osmium"

_HTTP_TIMEOUT = 60


class OsmExtractError(RuntimeError):
    """
    Base class for every way this module can fail to produce an extract.

    Generator.add_maplibre() catches this one class and skips the MapLibre
    bundle rather than failing the whole map job - the same treatment
    NoDemCoverageError already gets, and for the same reason: an optional,
    purely decorative layer should not take a map down with it. Terrain,
    topology, waypoints and airspace are unaffected.
    """


class OsmCoverageError(OsmExtractError):
    """No Geofabrik region overlaps the requested bounds at all."""


class OsmDownloadTooLargeError(OsmExtractError):
    """
    The regions needed for these bounds exceed max_download_bytes.

    The message always names each region and its size, because "too large"
    on its own gives an operator nothing to act on - they need to know
    whether to raise the limit, pre-fetch one region by hand, or draw a
    smaller box.
    """


class OsmDownloadDisabledError(OsmExtractError):
    """Downloads are switched off and the needed regions are not cached."""


# --- HTTP ----------------------------------------------------------------
#
# urllib rather than the wget subprocess Downloader uses, for two reasons:
# HEAD requests and conditional GETs are awkward to drive through wget and
# parse back out of its stderr, and urllib honours the http_proxy /
# https_proxy environment variables natively - which matters, because
# every worker behind a corporate proxy has them set. (Planetiler is the
# one thing here that does not read them; see the wrapper script in
# container/worker/Dockerfile.)


def _http_head(url):
    """
    Response headers for `url`, lower-cased, or None if unreachable.

    Never raises: a failed HEAD costs the caller size information, which
    it can work around, and this runs against a third-party server in the
    middle of a map job.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            return {k.lower(): v for k, v in response.headers.items()}
    except (urllib.error.URLError, OSError) as e:
        print("HEAD {} failed: {}".format(url, e))
        return None


def _http_download(url, dest, headers=None):
    """
    Streams `url` to `dest`, atomically.

    Returns the response headers (lower-cased), or None for a 304 Not
    Modified - which is why the conditional-request headers are a
    parameter rather than something the caller sets up itself.

    Written to `dest`.part and renamed only once the body is complete, so
    an interrupted download can never leave a truncated .osm.pbf behind
    looking like a valid cache entry. That matters more than it sounds:
    the failure would surface much later as a corrupt-file error deep
    inside planetiler, on a subsequent job.
    """
    request = urllib.request.Request(url, headers=headers or {})
    try:
        response = urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise

    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    total = int(response.headers.get("Content-Length") or 0)
    done = 0
    next_report = 0
    try:
        with response, open(part, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    print(
                        "  {:.0f} MB{}".format(
                            done / 1e6,
                            " / {:.0f} MB".format(total / 1e6) if total else "",
                        )
                    )
                    next_report = done + 50 * 1024 * 1024
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, dest)
    except BaseException:
        if os.path.exists(part):
            os.unlink(part)
        raise

    return {k.lower(): v for k, v in response.headers.items()}


# --- regions -------------------------------------------------------------


class Region(object):
    """
    One Geofabrik extract: where to get it, and what ground it covers.

    `id` is the full slash-separated path ("europe/france/rhone-alpes"),
    reconstructed from the index's parent chain - the index itself stores
    only the leaf name ("rhone-alpes"), which is not unique across the
    whole tree and is not what the download URL is keyed on.
    """

    def __init__(self, id, name, url, geometry):
        self.id = id
        self.name = name
        self.url = url
        self.geometry = geometry

    @property
    def filename(self):
        return self.id.split("/")[-1] + "-latest.osm.pbf"

    @property
    def relpath(self):
        """Path under data/osm/geofabrik/, mirroring Geofabrik's own layout."""
        parts = self.id.split("/")
        return os.path.join(*(parts[:-1] + [self.filename]))

    def __repr__(self):
        return "Region({})".format(self.id)


def _ogr():
    """
    Imported lazily, and through one accessor, so that importing this
    module (and therefore maplibre.py, and therefore generator.py) does
    not require GDAL's Python bindings. Only the region *selection* needs
    them; the cache paths, sidecars and `mapgen-osm-cache list` do not.
    """
    from osgeo import ogr

    ogr.UseExceptions()
    return ogr


def _bbox_geometry(bounds):
    ogr = _ogr()
    return ogr.CreateGeometryFromWkt(
        "POLYGON(({l} {b},{r} {b},{r} {t},{l} {t},{l} {b}))".format(
            l=bounds.left, r=bounds.right, t=bounds.top, b=bounds.bottom
        )
    )


def _intersection(geometry, bbox):
    """
    geometry ∩ bbox, or None if they do not meet.

    Several Geofabrik region polygons are self-intersecting (they are
    generalised administrative outlines, not validated topology), which
    makes GEOS refuse the intersection outright. Buffer(0) is the standard
    repair for that; if even that fails the region is skipped rather than
    taking the job down, since it is one candidate among many and the
    greedy loop can cover its ground with another.
    """
    try:
        result = geometry.Intersection(bbox)
    except Exception:
        try:
            result = geometry.Buffer(0).Intersection(bbox)
        except Exception:
            return None
    if result is None or result.IsEmpty():
        return None
    return result


def select_regions(regions, bounds, cost_of):
    """
    The smallest-cost set of regions covering `bounds`, greedily.

    This is deliberately NOT a walk down the index's parent/child
    hierarchy, which is the obvious approach and is wrong: Geofabrik's
    children do not partition their parent. `europe` has both `france`,
    `switzerland`, `italy`, `austria` AND the convenience extracts `alps`
    (which overlaps all four) and `dach` (which overlaps three), plus
    `great-britain` / `united-kingdom` / `britain-and-ireland` overlapping
    each other. Taking "every intersecting child" for an Alps bbox
    therefore selects france + switzerland + italy + alps and downloads
    the same ground three times over.

    So instead every region at every depth is a flat candidate, and the
    loop repeatedly takes whichever one covers the most still-uncovered
    bbox area per unit of cost. That handles the overlaps as a side
    effect, and gets the right answer in each of the cases that matter:

      * a bbox inside Rhone-Alpes picks `rhone-alpes` (~0.5 GB) over
        `alps` (~1.6 GB) and `france` (~4 GB) - all three cover it fully,
        so cost decides;
      * a bbox straddling the French/Swiss border picks `rhone-alpes` +
        `switzerland` (~0.9 GB together) rather than the single `alps`
        extract that also covers it, because two cheap regions beat one
        expensive one;
      * `europe` is a candidate for every European bbox and is never
        chosen, because its cost per unit of coverage is hopeless. The
        size guard in OsmExtractCache is the backstop for that, not the
        primary defence.

    `cost_of(region)` returns bytes; OsmExtractCache passes a function
    that reports an already-cached region as near-free, which is what
    makes an existing cache get reused instead of re-derived.

    Areas are computed in square degrees. That is not an equal-area
    measure, but every comparison here is between candidates for the
    *same* bbox at the same latitude, so the distortion is common to all
    of them and cancels.

    Known limitation: Geofabrik's polygons include territorial waters, so
    a bbox that is largely open sea keeps finding "uncovered" water and
    works its way up to a continental extract to reach it. That is caught
    by the download limit rather than here - see the note above
    _CONTRIBUTION_FLOOR for why no cost ceiling lives in this function.
    Telling sea from land would need a coastline dataset, which is a lot
    of machinery for a case that only costs bandwidth, only on maps that
    are mostly ocean, and already fails safely and audibly.

    Returns the chosen regions, ordered as selected (most valuable first).
    """
    bbox = _bbox_geometry(bounds)
    bbox_area = bbox.GetArea()
    if bbox_area <= 0:
        raise OsmCoverageError("Empty bounds: {}".format(bounds))

    candidates = []
    for region in regions:
        overlap = _intersection(region.geometry, bbox)
        if overlap is None:
            continue
        if overlap.GetArea() < _CONTRIBUTION_FLOOR * bbox_area:
            continue
        candidates.append((region, overlap))

    if not candidates:
        raise OsmCoverageError(
            "No Geofabrik region overlaps {} by more than {:.1%} of its "
            "area.".format(bounds, _CONTRIBUTION_FLOOR)
        )

    chosen = []
    covered = None
    covered_area = 0.0
    remaining = list(candidates)

    while covered_area < _COVERAGE_TOLERANCE * bbox_area and remaining:
        best = None
        for index, (region, overlap) in enumerate(remaining):
            gain_geometry = overlap if covered is None else overlap.Difference(covered)
            gain = 0.0 if gain_geometry is None else gain_geometry.GetArea()
            if gain <= _CONTRIBUTION_FLOOR * bbox_area:
                continue
            cost = max(1, cost_of(region))
            score = gain / cost
            # Ties broken by id so the same bbox always yields the same
            # region set - a job that silently picks a different extract
            # on a rerun is not reproducible.
            key = (score, -cost, region.id)
            if best is None or key > best[0]:
                best = (key, index, overlap)
        if best is None:
            break
        _key, index, overlap = best
        region, _overlap = remaining.pop(index)
        chosen.append(region)
        covered = overlap if covered is None else covered.Union(overlap)
        covered_area = covered.GetArea()

    if not chosen:
        raise OsmCoverageError(
            "No Geofabrik region covers a usable part of {}.".format(bounds)
        )
    return chosen


# --- the index -----------------------------------------------------------


class GeofabrikIndex(object):
    """
    Geofabrik's index-v1.json, cached on disk and parsed into Regions.

    Sizes are kept in a sidecar next to the index rather than re-HEADed
    per job: they only matter for ranking candidates against each other,
    they change by a few percent a month, and one HEAD per candidate on
    every single job is a lot of round-trips to a third party for a number
    that barely moves.
    """

    def __init__(self, dir_data, max_age_days=DEFAULT_INDEX_MAX_AGE_DAYS,
                 allow_download=True, url=GEOFABRIK_INDEX_URL):
        self.__dir_data = dir_data
        self.__max_age_days = max_age_days
        self.__allow_download = allow_download
        self.__url = url
        self.__path = os.path.join(dir_data, "osm", "geofabrik-index-v1.json")
        self.__meta_path = self.__path + ".meta"
        self.__regions = None
        self.__meta = None

    @property
    def path(self):
        return self.__path

    def __load_meta(self):
        if self.__meta is None:
            try:
                with open(self.__meta_path) as f:
                    self.__meta = json.load(f)
            except (OSError, ValueError):
                self.__meta = {}
        return self.__meta

    def __save_meta(self):
        os.makedirs(os.path.dirname(self.__meta_path), exist_ok=True)
        with open(self.__meta_path, "w") as f:
            json.dump(self.__meta, f, indent=1, sort_keys=True)

    def __is_stale(self):
        if not os.path.exists(self.__path):
            return True
        if not self.__max_age_days:
            return False
        age = time.time() - os.path.getmtime(self.__path)
        return age > self.__max_age_days * 86400

    def ensure(self):
        """
        Makes sure the index is on disk and reasonably fresh.

        A stale-but-present index is used as-is if the refresh fails: the
        region polygons are years-stable, so a network blip should not
        stop a job that could have been served from a slightly old copy.
        A *missing* index is fatal, since there is then nothing to select
        against.
        """
        if not self.__is_stale():
            return self.__path

        if not self.__allow_download:
            if os.path.exists(self.__path):
                return self.__path
            raise OsmDownloadDisabledError(
                "The Geofabrik region index is not cached at {} and "
                "downloads are disabled.".format(self.__path)
            )

        meta = self.__load_meta()
        headers = {}
        if os.path.exists(self.__path) and meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]

        with FileLock(self.__dir_data, "geofabrik-index"):
            print("Fetching Geofabrik region index from {} ...".format(self.__url))
            try:
                response = _http_download(self.__url, self.__path, headers)
            except (urllib.error.URLError, OSError) as e:
                if os.path.exists(self.__path):
                    print("Keeping cached region index ({}).".format(e))
                    return self.__path
                raise OsmExtractError(
                    "Could not fetch the Geofabrik region index from {}: "
                    "{}".format(self.__url, e)
                )
            if response is None:
                # 304: the copy on disk is current, but its mtime is not,
                # and the staleness check above is mtime-based - so touch
                # it, or every job from here on repeats this request.
                os.utime(self.__path, None)
            else:
                meta["etag"] = response.get("etag")
                meta["fetched_at"] = int(time.time())
                self.__save_meta()
        return self.__path

    def regions(self):
        """Every region in the index, with full-path ids."""
        if self.__regions is not None:
            return self.__regions

        with open(self.ensure()) as f:
            index = json.load(f)

        ogr = _ogr()
        by_leaf = {}
        for feature in index.get("features", []):
            properties = feature.get("properties", {})
            leaf = properties.get("id")
            if leaf:
                by_leaf[leaf] = (properties, feature.get("geometry"))

        def full_id(leaf):
            parts = []
            seen = set()
            while leaf and leaf in by_leaf and leaf not in seen:
                seen.add(leaf)
                parts.append(leaf)
                leaf = by_leaf[leaf][0].get("parent")
            return "/".join(reversed(parts))

        regions = []
        for leaf, (properties, geometry) in by_leaf.items():
            url = (properties.get("urls") or {}).get("pbf")
            if not url or not geometry:
                continue
            try:
                geom = ogr.CreateGeometryFromJson(json.dumps(geometry))
            except Exception as e:
                print("Skipping region {} with unusable geometry: {}".format(leaf, e))
                continue
            if geom is None:
                continue
            regions.append(
                Region(
                    id=full_id(leaf),
                    name=properties.get("name") or leaf,
                    url=url,
                    geometry=geom,
                )
            )

        self.__regions = sorted(regions, key=lambda r: r.id)
        return self.__regions

    def region(self, region_id):
        for region in self.regions():
            if region.id == region_id:
                return region
        return None

    def size_of(self, region):
        """
        Download size in bytes, from the sidecar cache or a fresh HEAD.

        An unreachable server yields _UNKNOWN_SIZE_BYTES rather than an
        error: not knowing a size should rank a region last, not abort a
        selection that may not even need it.
        """
        meta = self.__load_meta()
        sizes = meta.setdefault("sizes", {})
        cached = sizes.get(region.id)
        if cached:
            return cached

        if not self.__allow_download:
            return _UNKNOWN_SIZE_BYTES

        headers = _http_head(region.url)
        length = int((headers or {}).get("content-length") or 0)
        if not length:
            return _UNKNOWN_SIZE_BYTES

        sizes[region.id] = length
        self.__save_meta()
        return length


# --- the cache -----------------------------------------------------------


def human_bytes(count):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if count < 1024 or unit == "TB":
            return "{:.1f} {}".format(count, unit)
        count /= 1024.0


class OsmExtractCache(object):
    """
    Resolves a job's bounds to a bbox-clipped .osm.pbf, downloading the
    regional extracts it needs the first time they are needed.

    Injected into MapLibreBundle the same way DemCache is, so the server
    can hand it configured limits while bin/mapgen and the tests can use
    plain defaults.
    """

    def __init__(
        self,
        dir_data,
        allow_download=True,
        max_download_bytes=DEFAULT_MAX_DOWNLOAD_BYTES,
        max_age_days=DEFAULT_EXTRACT_MAX_AGE_DAYS,
        index=None,
    ):
        self.__dir_data = dir_data
        self.__allow_download = allow_download
        self.__max_download_bytes = max_download_bytes
        self.__max_age_days = max_age_days
        self.__index = index or GeofabrikIndex(
            dir_data, allow_download=allow_download
        )

    @property
    def index(self):
        return self.__index

    @property
    def dir_cache(self):
        return os.path.join(self.__dir_data, "osm", "geofabrik")

    def path_for(self, region):
        return os.path.join(self.dir_cache, region.relpath)

    def is_cached(self, region):
        """
        A region counts as cached when its .osm.pbf is present and
        non-empty - sidecar or not. An operator putting a file at the
        right path is a supported way to populate this cache offline, so
        the file is the source of truth and the sidecar is only
        provenance.
        """
        path = self.path_for(region)
        try:
            return os.path.getsize(path) > 0
        except OSError:
            return False

    def __is_stale(self, region):
        if not self.__max_age_days:
            return False
        try:
            age = time.time() - os.path.getmtime(self.path_for(region))
        except OSError:
            return True
        return age > self.__max_age_days * 86400

    def __cost_of(self, region):
        if self.is_cached(region) and not self.__is_stale(region):
            return _CACHED_COST_BYTES
        return self.__index.size_of(region)

    def select(self, bounds):
        """The regions needed to cover `bounds` - see select_regions()."""
        return select_regions(self.__index.regions(), bounds, self.__cost_of)

    def __download_bytes(self, regions):
        """
        What this job would actually pull, i.e. ignoring regions already
        on disk. Guarding on the *selection's* total size instead would
        refuse a job that needs no network at all, purely because the
        operator had pre-fetched something large.
        """
        pending = [r for r in regions if not self.is_cached(r)]
        return pending, sum(self.__index.size_of(r) for r in pending)

    def ensure(self, regions):
        """Downloads whichever of `regions` are not cached yet."""
        pending, total = self.__download_bytes(regions)
        if not pending:
            return

        detail = ", ".join(
            "{} ({})".format(r.id, human_bytes(self.__index.size_of(r)))
            for r in pending
        )
        if not self.__allow_download:
            raise OsmDownloadDisabledError(
                "OSM extract download is disabled, and these regions are "
                "not in the cache at {}: {}. Pre-fetch them there (see "
                "bin/mapgen-osm-cache) or re-enable "
                "downloads.".format(self.dir_cache, detail)
            )
        if self.__max_download_bytes and total > self.__max_download_bytes:
            raise OsmDownloadTooLargeError(
                "These bounds need {} of new OSM extracts, over the {} "
                "limit: {}. Either raise maplibre_osm_max_download_bytes, "
                "pre-fetch a region with bin/mapgen-osm-cache, or use a "
                "smaller bounding box.".format(
                    human_bytes(total),
                    human_bytes(self.__max_download_bytes),
                    detail,
                )
            )

        for region in pending:
            self.__fetch(region)

    def __fetch(self, region):
        path = self.path_for(region)
        with FileLock(self.__dir_data, region.id):
            # Re-checked inside the lock: another process may have been
            # downloading this very region while we waited for it, and
            # doing it again would be a pure waste of a few hundred MB.
            if self.is_cached(region) and not self.__is_stale(region):
                return path

            print(
                "Downloading OSM extract {} ({}) from {} ...".format(
                    region.id,
                    human_bytes(self.__index.size_of(region)),
                    region.url,
                )
            )
            headers = _http_download(region.url, path)
            self.__write_sidecar(region, headers)
        return path

    def __write_sidecar(self, region, headers):
        headers = headers or {}
        meta = {
            "region_id": region.id,
            "name": region.name,
            "url": region.url,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "size": os.path.getsize(self.path_for(region)),
            "downloaded_at": int(time.time()),
        }
        with open(self.path_for(region) + ".meta.json", "w") as f:
            json.dump(meta, f, indent=1, sort_keys=True)

    def cached_regions(self):
        """
        Every .osm.pbf under the managed cache, with its sidecar if there
        is one. Drives `mapgen-osm-cache list`, and needs neither the
        index nor GDAL - so it still works on a worker that has never
        been able to reach Geofabrik.
        """
        found = []
        for root, _dirs, files in os.walk(self.dir_cache):
            for name in sorted(files):
                if not name.endswith(".osm.pbf"):
                    continue
                path = os.path.join(root, name)
                meta = {}
                try:
                    with open(path + ".meta.json") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    pass
                relpath = os.path.relpath(path, self.dir_cache)
                meta.setdefault(
                    "region_id",
                    os.path.join(
                        os.path.dirname(relpath),
                        os.path.basename(relpath)[: -len("-latest.osm.pbf")],
                    ).replace(os.sep, "/"),
                )
                found.append((path, meta))
        return found

    # ---- clipping -------------------------------------------------------

    def extract_for(self, bounds, dir_temp, out_path=None):
        """
        A single .osm.pbf clipped to `bounds`, plus the region ids it came
        from (for the bundle's provenance report).

        Each region is clipped BEFORE the merge, not after. Merging two
        500 MB national extracts and then clipping would work, but it
        moves an order of magnitude more data through osmium than merging
        two 20 MB clips does - and the merge is the expensive half.
        `osmium merge` drops duplicate objects, which is what makes this
        safe across a border where both extracts contain the same ways.
        """
        require_osmium()
        regions = self.select(bounds)
        self.ensure(regions)

        bbox = "{},{},{},{}".format(
            bounds.left, bounds.bottom, bounds.right, bounds.top
        )
        out_path = out_path or os.path.join(dir_temp, "extract.osm.pbf")

        clips = []
        for index, region in enumerate(regions):
            clip = (
                out_path
                if len(regions) == 1
                else os.path.join(dir_temp, "clip-{}.osm.pbf".format(index))
            )
            print("Clipping {} to {} ...".format(region.id, bbox))
            subprocess.check_call([
                _CMD_OSMIUM, "extract",
                "--bbox", bbox,
                "--strategy", "smart",
                "--overwrite",
                "-o", clip,
                self.path_for(region),
            ])
            clips.append(clip)

        if len(clips) > 1:
            print("Merging {} clipped extracts ...".format(len(clips)))
            subprocess.check_call(
                [_CMD_OSMIUM, "merge", "--overwrite", "-o", out_path] + clips
            )
            for clip in clips:
                os.unlink(clip)

        return out_path, [region.id for region in regions]

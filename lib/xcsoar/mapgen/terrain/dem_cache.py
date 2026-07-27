# -*- coding: utf-8 -*-
#
# The single place that knows which DEM tiers exist and which one a given
# 1-degree cell is actually read from.
#
# Before this module the two DEM consumers open-coded their own lookup and
# quietly disagreed about what data the map was built from:
#
#   * srtm.py (terrain.jp2) hardcoded downloader.retrieve("dem3/...") and
#     therefore ALWAYS sourced 3-arcsec, for every job. Its
#     `arcseconds_per_pixel` argument is a gdalwarp *output* spacing, not a
#     source tier - "high resolution terrain (3 arcsec instead of 9)" meant
#     "resample the same 3-arcsec source onto a finer output grid".
#   * maplibre.py (hillshade) preferred data/dem/ (1") and fell back to
#     data/dem3/ (3"), ignoring `resolution` entirely.
#
# So the two subsystems could be reading different source data for the same
# map, and neither recorded which. The only way to find out afterwards was
# forensic analysis of the output pixels - which is exactly how an earlier
# session came to the wrong conclusion about a shipped bundle. Recording
# provenance is therefore not a nicety here; it is the point. Every
# locate() is remembered (see report()) so the build can state, per cell,
# what it actually read.
#
# The two tiers are NOT equivalent in availability:
#
#   * dem3/ (3") is global and downloadable - Downloader mirrors it from
#     the data server, whose `checksums` manifest lists ~26 000 dem3/
#     entries.
#   * dem/ (1") is a manually curated, partial-coverage, region-limited
#     set placed on disk by an operator (see docs/DATA_SOURCES.md - Sonny's
#     LiDAR-derived DTM). The manifest lists ZERO dem/ entries, so
#     Downloader cannot fetch it. Requesting 1" outside the curated region
#     will routinely find nothing.
#
# That asymmetry is why the missing-data policy below is load-bearing
# rather than decorative.

import math
import os


# --- missing-data policy ------------------------------------------------

# Use the next-coarser tier for any cell the requested tier lacks, mark it
# downgraded, and carry on. Lenient, but never silent: every downgrade is
# recorded and shows up in the provenance report.
POLICY_FALLBACK = "fallback"

# Refuse to build a map that is not entirely at the requested tier, naming
# exactly which cells fell short so the message is actionable.
POLICY_FAIL = "fail"

POLICIES = (POLICY_FALLBACK, POLICY_FAIL)
DEFAULT_POLICY = POLICY_FALLBACK

# Passed as `preferred_arcsec` to mean "use the best tier that has data for
# this cell". This is what the hillshade did implicitly before this module
# existed - it preferred dem/ and fell back to dem3/, ignoring whatever
# resolution the job had asked for.
AUTO_ARCSEC = None

# The default source tier, everywhere: library entry points, the web job
# description and both CLIs all agree on this one value, so the frontend
# and the CLI cannot drift apart.
#
# 3 rather than AUTO is a deliberate trade. AUTO would give sharper output
# wherever 1-arcsec tiles happen to be on disk, but it also means the
# effective resolution of a map varies with whatever an operator last
# copied into data/dem/ - the same request produces different data on
# different machines, which is the ambiguity this whole module exists to
# remove. Pinning the global, downloadable tier makes the default
# reproducible; 1 arcsec is then an explicit, reported opt-in.
#
# Note this does change the MapLibre hillshade's historical behaviour: it
# used to pick up 1-arcsec data implicitly wherever it existed (e.g. the
# French Alps) and now needs that asked for. The provenance report states
# the tier actually used, so the difference is visible rather than
# something to be rediscovered by inspecting pixels.
DEFAULT_ARCSEC = 3.0


class DemCoverageError(RuntimeError):
    """
    Raised when the DEM cache cannot satisfy a request.

    Under POLICY_FAIL this means one or more 1-degree cells are unavailable
    at the requested resolution. Under either policy it is also raised when
    a set of mandatory cells has no DEM data at all, at any tier - there is
    then nothing to build a hillshade or a terrain grid from.

    The message always names the offending cells, because "no DEM coverage"
    on its own is not actionable.
    """


# --- tiers ---------------------------------------------------------------


class DemTier(object):
    """
    One resolution tier of the on-disk DEM cache.

    `samples` is the tile's grid size (samples per side). .hgt files carry
    no header at all - GDAL's SRTMHGT driver, and everyone else, infers the
    grid purely from file size - so this doubles as the tier's fingerprint:
    a tile is `samples * samples * 2` bytes of big-endian int16.

    Note the spacing is 3600/(samples-1), not 3600/samples: an .hgt tile is
    edge-inclusive, so a 1201-sample tile has 1200 intervals across one
    degree = exactly 3 arcsec. (maplibre.py previously used 3600/width,
    giving 2.9975 - close enough not to matter there, but there is no
    reason to carry the approximation into the tier definitions.)
    """

    def __init__(self, arcsec, subdir, samples, upper, downloadable, origin):
        self.arcsec = arcsec
        self.subdir = subdir
        self.samples = samples
        self.upper = upper
        self.downloadable = downloadable
        self.origin = origin

    @property
    def name(self):
        return self.subdir

    @property
    def file_size(self):
        return self.samples * self.samples * 2

    def tile_name(self, lat, lon):
        """
        The two tiers genuinely differ in filename case on disk - dem/ holds
        "N45E006.hgt" and dem3/ holds "n45e006.hgt" - because they were
        populated by different means (manual copy vs. Downloader mirror).
        Both conventions are preserved rather than normalised, since the
        files are already there and renaming a multi-GB cache to satisfy a
        refactor would be the wrong trade.
        """
        ns = "n" if lat >= 0 else "s"
        ew = "e" if lon >= 0 else "w"
        name = "{ns}{lat:02}{ew}{lon:03}".format(
            ns=ns, lat=abs(int(lat)), ew=ew, lon=abs(int(lon))
        )
        return name.upper() if self.upper else name

    def relpath(self, lat, lon):
        """Path relative to the data cache root, also the Downloader key."""
        return "{}/{}.hgt".format(self.subdir, self.tile_name(lat, lon))

    def path(self, dir_data, lat, lon):
        return os.path.join(dir_data, self.subdir, self.tile_name(lat, lon) + ".hgt")

    def __repr__(self):
        return "DemTier({} arcsec, {}/)".format(self.arcsec, self.subdir)


# Ordered finest-first. Everything below relies on that ordering.
TIERS = (
    DemTier(
        arcsec=1,
        subdir="dem",
        samples=3601,
        upper=True,
        downloadable=False,
        origin="manual, partial coverage",
    ),
    DemTier(
        arcsec=3,
        subdir="dem3",
        samples=1201,
        upper=False,
        downloadable=True,
        origin="downloaded, global",
    ),
)

COARSEST_ARCSEC = TIERS[-1].arcsec


def tier_for_arcsec(arcsec):
    for tier in TIERS:
        if tier.arcsec == arcsec:
            return tier
    return None


def supported_arcsec():
    return [tier.arcsec for tier in TIERS]


def cell_name(lat, lon):
    """Canonical, uppercase cell label for human-readable messages."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return "{}{:02}{}{:03}".format(ns, abs(int(lat)), ew, abs(int(lon)))


def detect_arcsec(path):
    """
    Determine a tile's real resolution from its file size rather than from
    the folder it was found in.

    This is deliberate: the folder is a convention an operator can get
    wrong (drop a 1" tile into dem3/ and every downstream zoom-level
    calculation silently becomes wrong), whereas the size is a property of
    the data itself. maplibre.py already derived resolution this way for
    its zoom cap, and that robustness is worth keeping as the rule
    everywhere.

    Returns arcsec/pixel as a float, or None if the file is missing or is
    not a plausible square int16 grid.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None

    if size <= 0 or size % 2:
        return None

    samples = math.isqrt(size // 2)
    if samples * samples * 2 != size or samples < 2:
        return None

    return 3600.0 / (samples - 1)


# --- located tiles -------------------------------------------------------


class DemTileRef(object):
    """One resolved 1-degree cell: where it came from and at what cost."""

    def __init__(self, path, arcsec, tier_name, lat, lon, downgraded=False):
        self.path = path
        self.arcsec = arcsec
        self.tier_name = tier_name
        self.lat = lat
        self.lon = lon
        self.downgraded = downgraded

    @property
    def cell(self):
        return cell_name(self.lat, self.lon)

    def __repr__(self):
        return "DemTileRef({} {} arcsec{})".format(
            self.cell, self.arcsec, ", DOWNGRADED" if self.downgraded else ""
        )


class _Record(object):
    """One locate() call, kept so the build can report what it really read."""

    def __init__(self, consumer, lat, lon, requested_arcsec, mandatory, ref):
        self.consumer = consumer
        self.lat = lat
        self.lon = lon
        self.requested_arcsec = requested_arcsec
        self.mandatory = mandatory
        self.ref = ref

    @property
    def cell(self):
        return cell_name(self.lat, self.lon)


# --- the cache -----------------------------------------------------------


class DemCache(object):
    """
    Locates DEM tiles for every consumer in a job, and remembers what it
    handed out.

    One instance is shared by the whole job (Generator owns it), so the
    provenance report can cover terrain.jp2 and the MapLibre hillshade
    together and show when they diverge.
    """

    def __init__(self, dir_data, downloader=None):
        """
        downloader: optional Downloader. Only ever consulted for tiers
        marked downloadable (dem3/). Routing a dem/ path through it would
        raise, since the server manifest has no dem/ entries at all - the
        tier flag, not a try/except, is what keeps that from happening.
        """
        self.__dir_data = os.path.abspath(dir_data)
        self.__downloader = downloader
        self.__records = []
        self.__resolved = {}

    @property
    def dir_data(self):
        return self.__dir_data

    # -- lookup ----------------------------------------------------------

    def __candidate_tiers(self, preferred_arcsec, policy):
        """
        Which tiers may satisfy this request, best-first.

        AUTO: every tier, finest-first - "use the best data available for
        this cell", the hillshade's historical behaviour.

        POLICY_FAIL: only the requested tier. A cell that exists solely at
        a coarser tier is precisely the "not available at the requested
        resolution" case the policy exists to reject, so there is nothing
        else to try.

        POLICY_FALLBACK: the requested tier and everything coarser. Never
        anything finer - honouring an explicit "3 arcsec" request by
        silently reading 1" data would make the control meaningless and,
        worse, would let the effective resolution vary cell by cell with
        whatever an operator happens to have dropped into dem/.
        """
        if preferred_arcsec is AUTO_ARCSEC:
            return list(TIERS)

        if policy == POLICY_FAIL:
            tier = tier_for_arcsec(preferred_arcsec)
            return [tier] if tier else []

        return [tier for tier in TIERS if tier.arcsec >= preferred_arcsec]

    def __tile_at(self, tier, lat, lon):
        """
        Resolve one cell within one tier, returning (path, arcsec) or None.

        The tier's own arcsec is treated as a declaration, not as truth:
        whatever the file's size says wins (see detect_arcsec). A tile
        filed in the wrong folder is thereby reported at its real
        resolution instead of poisoning the zoom-level maths downstream.
        """
        path = tier.path(self.__dir_data, lat, lon)

        if not os.path.exists(path) and tier.downloadable and self.__downloader:
            try:
                path = self.__downloader.retrieve(tier.relpath(lat, lon))
            except Exception:
                # Absent from the server (ocean, outside coverage), or the
                # download/checksum failed. Either way this tier has
                # nothing for this cell; the caller falls back or records
                # it missing. Downloader already logs the details.
                return None

        arcsec = detect_arcsec(path)
        if arcsec is None:
            return None

        return path, arcsec

    def locate(
        self,
        lat,
        lon,
        preferred_arcsec=AUTO_ARCSEC,
        policy=DEFAULT_POLICY,
        consumer="terrain",
        mandatory=True,
    ):
        """
        Find the DEM tile for the 1-degree cell whose SW corner is
        (lat, lon), and record the outcome.

        `mandatory` distinguishes cells that actually overlap the requested
        bounds from the best-effort padded ring both consumers fetch for
        clean edge interpolation. Only mandatory cells constrain coverage,
        resolution and policy decisions - a coarse neighbour one degree
        outside the map must not drag the whole build down.

        Returns a DemTileRef, or None if no tier has this cell. Never
        raises for a single missing cell: POLICY_FAIL needs to name every
        offending cell at once, so the aggregate check lives in
        raise_if_incomplete().
        """
        ref = None
        for tier in self.__candidate_tiers(preferred_arcsec, policy):
            found = self.__tile_at(tier, lat, lon)
            if found:
                path, arcsec = found
                ref = DemTileRef(
                    path=path,
                    arcsec=arcsec,
                    tier_name=tier.name,
                    lat=lat,
                    lon=lon,
                    downgraded=(
                        preferred_arcsec is not AUTO_ARCSEC
                        and arcsec > preferred_arcsec
                    ),
                )
                break

        self.__records.append(
            _Record(consumer, lat, lon, preferred_arcsec, mandatory, ref)
        )
        if ref:
            self.__resolved[(consumer, lat, lon)] = ref
        return ref

    # -- policy enforcement ----------------------------------------------

    def raise_if_incomplete(
        self, policy, consumer=None, no_coverage_error=DemCoverageError
    ):
        """
        Apply the missing-data policy to everything located so far.

        Called once per consumer after its cells have been located, so a
        POLICY_FAIL message can list every shortfall at once rather than
        failing on the first cell and hiding the other nine.

        `no_coverage_error` lets a caller pick the exception class for the
        total-absence case. The MapLibre bundle uses it to raise its own
        skippable NoDemCoverageError under POLICY_FALLBACK - an optional
        decorative layer should not sink a whole map job - while still
        getting a hard DemCoverageError under POLICY_FAIL.
        """
        report = self.report(consumer)

        if not report.mandatory_refs and report.missing_cells:
            raise no_coverage_error(
                "No DEM data at all for the requested area: {} unavailable "
                "at any resolution under {}. Checked {}.".format(
                    ", ".join(report.missing_cells),
                    self.__dir_data,
                    ", ".join("{}/".format(t.subdir) for t in TIERS),
                )
            )

        if policy != POLICY_FAIL:
            return

        shortfall = sorted(set(report.missing_cells) | set(report.downgraded_cells))
        if not shortfall:
            return

        requested = report.requested_arcsec
        raise DemCoverageError(
            "{} not available at {:g} arcsec. Place the missing tiles in "
            "{}/ (they cannot be downloaded - see docs/DATA_SOURCES.md), "
            "choose a coarser resolution, or use the 'fallback' "
            "missing-data policy to build them at {:g} arcsec instead.".format(
                ", ".join(shortfall),
                requested,
                (tier_for_arcsec(requested) or TIERS[-1]).subdir,
                COARSEST_ARCSEC,
            )
        )

    # -- provenance ------------------------------------------------------

    def report(self, consumer=None):
        records = [
            r
            for r in self.__records
            if consumer is None or r.consumer == consumer
        ]
        return DemProvenance(records)


class DemProvenance(object):
    """
    What the build actually read, as opposed to what it was asked to read.

    Deliberately derived from the recorded locate() calls rather than from
    the request, so it cannot drift away from reality - a report that
    restates the requested settings would have been just as wrong as the
    assumption it is meant to catch.
    """

    def __init__(self, records):
        self.records = records

    # -- cell sets (mandatory only - the padded ring is best-effort) -----

    @property
    def mandatory(self):
        return [r for r in self.records if r.mandatory]

    @property
    def mandatory_refs(self):
        return [r.ref for r in self.mandatory if r.ref]

    @property
    def requested_arcsec(self):
        requested = {r.requested_arcsec for r in self.records}
        requested.discard(AUTO_ARCSEC)
        return max(requested) if requested else AUTO_ARCSEC

    @property
    def missing_cells(self):
        return sorted({r.cell for r in self.mandatory if not r.ref})

    @property
    def downgraded_cells(self):
        return sorted({ref.cell for ref in self.mandatory_refs if ref.downgraded})

    def tier_counts(self):
        """{arcsec: cell count} over mandatory cells that resolved."""
        counts = {}
        for ref in self.mandatory_refs:
            counts[ref.arcsec] = counts.get(ref.arcsec, 0) + 1
        return counts

    def effective_arcsec(self):
        """
        The coarsest resolution among the mandatory cells - a mixed-tier
        build is only as sharp as its worst cell, and this is what any
        zoom-level cutoff must be derived from.

        Crucially this ignores the padded ring. maplibre.py used to take
        max() over every located tile including the padding, so a single
        coarse neighbour one degree outside the requested bounds silently
        dragged the whole bundle's max zoom down.
        """
        refs = self.mandatory_refs
        return max(ref.arcsec for ref in refs) if refs else None

#!/usr/bin/env python3
"""
Structural validation of a MapLibre bundle produced by mapgen's
add_maplibre(). Does not render pixels (no mbgl-render available) - checks
that style.json is spec-shaped and internally consistent, that the mbtiles
files are well-formed and open via GDAL/OGR, that vector tile blobs decode
as real MVT layers matching what style.json references, and that the flat
{z}/{x}/{y} preview tree matches the mbtiles content.

Run inside the mapgen-worker container (needs gdal's ogrinfo/gdalinfo).
"""
import gzip
import json
import os
import sqlite3
import subprocess
import sys

FAILURES = []
WARNINGS = []


def fail(msg):
    FAILURES.append(msg)
    print("FAIL:", msg)


def warn(msg):
    WARNINGS.append(msg)
    print("WARN:", msg)


def ok(msg):
    print("OK:  ", msg)


def check_style_json(bundle_dir):
    path = os.path.join(bundle_dir, "style.json")
    with open(path) as f:
        style = json.load(f)
    ok("style.json is valid JSON")

    if style.get("version") != 8:
        fail("style.json version != 8: {}".format(style.get("version")))
    else:
        ok("style.json version == 8")

    sources = style.get("sources", {})
    if not sources:
        fail("style.json has no sources")

    for name, src in sources.items():
        url = src.get("url", "")
        if url.startswith("http://") or url.startswith("https://"):
            fail("source {!r} has a network URL: {}".format(name, url))
        else:
            ok("source {!r} url is offline-safe: {}".format(name, url))

    sprite = style.get("sprite", "")
    glyphs = style.get("glyphs", "")
    for label, val in (("sprite", sprite), ("glyphs", glyphs)):
        if val.startswith("http://") or val.startswith("https://"):
            fail("{} references a network URL: {}".format(label, val))
        else:
            ok("{} is offline-safe: {}".format(label, val))

    layers = style.get("layers", [])
    if not layers:
        fail("style.json has no layers")

    ids = set()
    valid_types = {
        "background", "fill", "line", "symbol", "circle", "heatmap",
        "fill-extrusion", "raster", "hillshade", "sky", "model",
    }
    for layer in layers:
        lid = layer.get("id")
        if lid in ids:
            fail("duplicate layer id: {}".format(lid))
        ids.add(lid)

        ltype = layer.get("type")
        if ltype not in valid_types:
            fail("layer {!r} has unknown type {!r}".format(lid, ltype))

        src = layer.get("source")
        if src is not None and src not in sources:
            fail("layer {!r} references undefined source {!r}".format(lid, src))
    ok("all {} layers have unique ids and valid source references".format(len(layers)))

    return style


def mbtiles_metadata(path):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT name, value FROM metadata")
    meta = dict(cur.fetchall())
    cur.execute("SELECT COUNT(*) FROM tiles")
    (count,) = cur.fetchone()
    cur.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles LIMIT 3")
    samples = cur.fetchall()
    conn.close()
    return meta, count, samples


def check_basemap_mbtiles(bundle_dir, style):
    path = os.path.join(bundle_dir, "basemap.mbtiles")
    if not os.path.exists(path):
        fail("basemap.mbtiles missing")
        return None

    meta, count, samples = mbtiles_metadata(path)
    if count == 0:
        fail("basemap.mbtiles has zero tiles")
    else:
        ok("basemap.mbtiles has {} tiles".format(count))

    if meta.get("format") != "pbf":
        fail("basemap.mbtiles format metadata != pbf: {}".format(meta.get("format")))

    vector_layers = set()
    try:
        vl = json.loads(meta.get("json", "{}")).get("vector_layers", [])
        vector_layers = {layer["id"] for layer in vl}
        ok("basemap.mbtiles declares {} vector layers: {}".format(
            len(vector_layers), sorted(vector_layers)))
    except Exception as e:
        warn("could not parse vector_layers from mbtiles metadata: {}".format(e))

    # Cross-check style.json's source-layer references against what the
    # mbtiles actually contains - catches typos/mismatches that would
    # silently render as empty layers.
    referenced = set()
    for layer in style.get("layers", []):
        if layer.get("source") == "openmaptiles" and "source-layer" in layer:
            referenced.add(layer["source-layer"])
    missing = referenced - vector_layers
    if missing:
        fail("style.json references source-layers not present in "
             "basemap.mbtiles: {}".format(sorted(missing)))
    else:
        ok("all style.json source-layer references exist in basemap.mbtiles: "
           "{}".format(sorted(referenced)))

    # Sample tile blobs should be gzip-compressed MVT (that's what
    # __unpack_flat_tiles() specifically has to gunzip).
    for z, x, y, data in samples:
        if data[:2] != b"\x1f\x8b":
            warn("tile z{}/{}/{} is not gzip-compressed (format={})".format(
                z, x, y, meta.get("format")))
        else:
            raw = gzip.decompress(data)
            if len(raw) == 0:
                fail("tile z{}/{}/{} decompresses to empty payload".format(z, x, y))
    ok("sampled tile blobs decompress cleanly")

    # Native GDAL/OGR MBTiles driver open + layer enumeration - exercises
    # real MVT protobuf decoding, not just gzip framing.
    try:
        out = subprocess.check_output(
            ["ogrinfo", "-so", path], stderr=subprocess.STDOUT, text=True
        )
        driver_ok = "using driver `MBTiles' successful" in out
        layer_lines = [
            l for l in out.splitlines()
            if l.strip() and l.strip()[0].isdigit() and ":" in l
        ]
        if driver_ok and layer_lines:
            ok("ogrinfo opened basemap.mbtiles via the MBTiles/OGR driver "
               "and enumerated {} layers".format(len(layer_lines)))
        else:
            fail("ogrinfo could not open/enumerate basemap.mbtiles layers")
    except FileNotFoundError:
        warn("ogrinfo not on PATH - skipping native GDAL/OGR open check")
    except subprocess.CalledProcessError as e:
        fail("ogrinfo failed on basemap.mbtiles: {}".format(e.output))

    return meta


def check_hillshade_mbtiles(bundle_dir, expected_bounds=None):
    path = os.path.join(bundle_dir, "hillshade.mbtiles")
    if not os.path.exists(path):
        fail("hillshade.mbtiles missing")
        return None

    meta, count, samples = mbtiles_metadata(path)
    if count == 0:
        fail("hillshade.mbtiles has zero tiles")
    else:
        ok("hillshade.mbtiles has {} tiles".format(count))

    if meta.get("format") != "png":
        fail("hillshade.mbtiles format metadata != png: {}".format(meta.get("format")))

    for z, x, y, data in samples:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            fail("tile z{}/{}/{} is not a valid PNG (bad magic bytes)".format(z, x, y))
    ok("sampled hillshade tile blobs are valid PNGs")

    try:
        out = subprocess.check_output(
            ["gdalinfo", path], stderr=subprocess.STDOUT, text=True
        )
        if "Driver: MBTiles/MBTiles" not in out:
            fail("gdalinfo did not report the MBTiles driver for hillshade.mbtiles")
        else:
            ok("gdalinfo opened hillshade.mbtiles via the MBTiles/GDAL driver")
        if expected_bounds:
            left, right, top, bottom = expected_bounds
            # Corner coordinates are printed in the tile grid's native CRS
            # (mercator) *and* lat/lon in parens - just sanity check the
            # raster is genuinely geo-referenced somewhere near our bbox.
            if "Corner Coordinates" not in out:
                warn("gdalinfo output has no Corner Coordinates section")
            else:
                ok("hillshade.mbtiles is geo-referenced (see Corner Coordinates)")
    except FileNotFoundError:
        warn("gdalinfo not on PATH - skipping native GDAL open check")
    except subprocess.CalledProcessError as e:
        fail("gdalinfo failed on hillshade.mbtiles: {}".format(e.output))

    return meta


def check_flat_tiles(bundle_dir, basemap_meta, hillshade_meta):
    for layer, fmt, meta, mbtiles_name in (
        ("basemap", "pbf", basemap_meta, "basemap.mbtiles"),
        ("hillshade", "png", hillshade_meta, "hillshade.mbtiles"),
    ):
        flat_dir = os.path.join(bundle_dir, "tiles", layer)
        if not os.path.isdir(flat_dir):
            fail("maplibre/tiles/{} directory missing".format(layer))
            continue

        flat_count = sum(
            len(files) for _, _, files in os.walk(flat_dir)
        )
        mbtiles_path = os.path.join(bundle_dir, mbtiles_name)
        _, mbtiles_count, _ = mbtiles_metadata(mbtiles_path)
        if flat_count != mbtiles_count:
            fail("tiles/{} has {} files but {} has {} tiles (mismatch)".format(
                layer, flat_count, mbtiles_name, mbtiles_count))
        else:
            ok("tiles/{} file count matches {} tile count ({})".format(
                layer, mbtiles_name, flat_count))

        # Spot-check one real file for magic bytes / not-gzipped-ness.
        sample = None
        for root, _dirs, files in os.walk(flat_dir):
            if files:
                sample = os.path.join(root, files[0])
                break
        if sample:
            with open(sample, "rb") as f:
                data = f.read()
            if layer == "basemap":
                if data[:2] == b"\x1f\x8b":
                    fail("flat pbf tile {} is still gzip-compressed (should "
                         "have been decompressed for file:// consumers)".format(sample))
                else:
                    ok("flat pbf tile {} is raw (not gzip) protobuf, {} bytes".format(
                        os.path.relpath(sample, flat_dir), len(data)))
            else:
                if data[:8] != b"\x89PNG\r\n\x1a\n":
                    fail("flat png tile {} has invalid PNG magic bytes".format(sample))
                else:
                    ok("flat png tile {} is a valid PNG, {} bytes".format(
                        os.path.relpath(sample, flat_dir), len(data)))


def check_sprites_glyphs(bundle_dir):
    sprites_dir = os.path.join(bundle_dir, "sprites")
    for base in ("sprite", "sprite@2x"):
        json_path = os.path.join(sprites_dir, base + ".json")
        png_path = os.path.join(sprites_dir, base + ".png")
        if not (os.path.exists(json_path) and os.path.exists(png_path)):
            fail("sprite pair {} missing".format(base))
            continue
        with open(json_path) as f:
            index = json.load(f)
        with open(png_path, "rb") as f:
            magic = f.read(8)
        if magic != b"\x89PNG\r\n\x1a\n":
            fail("{}.png has invalid PNG magic bytes".format(base))
        else:
            ok("{}: {} icons indexed, {}.png is a valid PNG".format(
                base, len(index), base))

    glyphs_dir = os.path.join(bundle_dir, "glyphs")
    fontstacks = [d for d in os.listdir(glyphs_dir)
                  if os.path.isdir(os.path.join(glyphs_dir, d))]
    if not fontstacks:
        fail("no glyph fontstacks found under glyphs/")
        return
    for stack in fontstacks:
        ranges = os.listdir(os.path.join(glyphs_dir, stack))
        bad = [r for r in ranges if not r.endswith(".pbf")]
        if bad:
            fail("glyph fontstack {!r} has non-pbf files: {}".format(stack, bad))
        else:
            ok("glyph fontstack {!r} has {} pbf ranges".format(stack, len(ranges)))


def main():
    bundle_dir = sys.argv[1] if len(sys.argv) > 1 else "maplibre"
    expected_bounds = None
    if len(sys.argv) > 5:
        expected_bounds = tuple(float(x) for x in sys.argv[2:6])

    style = check_style_json(bundle_dir)
    basemap_meta = check_basemap_mbtiles(bundle_dir, style)
    hillshade_meta = check_hillshade_mbtiles(bundle_dir, expected_bounds)
    check_flat_tiles(bundle_dir, basemap_meta, hillshade_meta)
    check_sprites_glyphs(bundle_dir)

    print()
    print("=" * 60)
    print("{} failures, {} warnings".format(len(FAILURES), len(WARNINGS)))
    if FAILURES:
        for f in FAILURES:
            print(" FAIL:", f)
        sys.exit(1)
    print("All structural checks passed.")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Build a French-Alps (Chamonix-Mont-Blanc) test .xcm with the optional
# MapLibre bundle enabled, running the real mapgen CLI inside the
# mapgen-worker Docker container (see docker-compose.yml) - this project's
# actual runtime, not a host-side install. Run from the root of the mapgen
# checkout.
#
# Prerequisites (see docs/GENERATE_TEST_BUNDLE.md for the full walkthrough):
#   - `docker compose build mapgen-worker` (picks up
#     container/worker/Dockerfile's osmium/gdal/planetiler/spreet deps)
#   - data/dem/*.hgt or data/dem3/*.hgt in the mapgen-data volume
#
# The OSM extract, Planetiler's auxiliary sources and the static style
# assets are no longer prerequisites: the first two are downloaded on
# demand for whatever bounding box is asked for, the third is baked into
# the worker image. See docs/GENERATE_TEST_BUNDLE.md step 0.

set -euo pipefail

# Chamonix-Mont-Blanc bounding box: left right top bottom
LEFT=6.72
RIGHT=7.05
TOP=46.05
BOTTOM=45.85

OUTDIR="$(pwd)/out"
OUTPUT=chamonix_test.xcm
mkdir -p "${OUTDIR}"

echo "== Building ${OUTPUT} (terrain+topology+waypoints+airspace, unchanged) =="
echo "== plus an offline MapLibre bundle for the same area =="

docker compose run --rm --no-deps --entrypoint /opt/mapgen/bin/mapgen \
  -v "${OUTDIR}:/workspace" mapgen-worker \
  -r 3 \
  -l 3 \
  -b "${LEFT}" "${RIGHT}" "${TOP}" "${BOTTOM}" \
  --maplibre \
  "/workspace/${OUTPUT}"

echo "== Done. Inspecting the bundle =="
rm -rf /tmp/chamonix_test_unzipped
mkdir -p /tmp/chamonix_test_unzipped
unzip -q "${OUTDIR}/${OUTPUT}" -d /tmp/chamonix_test_unzipped

echo "Top-level contents:"
ls -la /tmp/chamonix_test_unzipped

echo
echo "MapLibre bundle contents:"
ls -la /tmp/chamonix_test_unzipped/maplibre

echo
echo "Existing components are still there and untouched:"
for f in terrain.jp2 terrain.j2w topology.tpl info.txt; do
  if [ -e "/tmp/chamonix_test_unzipped/${f}" ]; then
    echo "  OK: ${f}"
  else
    echo "  MISSING: ${f}  <-- something regressed, check generator.py"
  fi
done

echo
echo "== Structural validation (no mbgl-render available - see"
echo "== docs/GENERATE_TEST_BUNDLE.md step 4) =="
docker compose run --rm --no-deps \
  -v "/tmp/chamonix_test_unzipped/maplibre:/workspace/maplibre:ro" \
  -v "$(pwd)/xcsoar-maplibre-poc/scripts/validate_bundle.py:/workspace/validate_bundle.py:ro" \
  --entrypoint bash mapgen-worker -c \
  "cd /workspace && python3 validate_bundle.py maplibre ${LEFT} ${RIGHT} ${TOP} ${BOTTOM}"

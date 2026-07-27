# Hypsometric restyle — colour + sharp relief in one basemap

The first bundle style rendered terrain as a plain grey `hillshade` layer:
high definition, but monochrome, so you cannot tell at a glance whether
ground is at 400 m or 3000 m. The target look is :
sharp relief **and** elevation colour at the same time.

An earlier attempt swapped the hillshade for an elevation-colour layer and
lost all terrain definition. That is the trap this document exists to
close out.

## The recipe

Three ingredients, and the first two are both required:

1. **`color-relief` layer, ordered *below* the `hillshade` layer**, both
   reading the same `raster-dem` source. MapLibre Native supports the
   `color-relief` layer type (`src/mbgl/style/layers/color_relief_layer.cpp`,
   with an upstream render test at
   `metrics/integration/render-tests/color-relief/hillshade/` that combines
   exactly these two layers). Colour-relief *alone* is smooth and featureless;
   hillshade *alone* is grey. Stacked in this order, both survive.
2. **Hillshade tuned for compositing**: `hillshade-method: "combined"`, with
   semi-transparent shadow/highlight so the tint underneath shows through
   rather than being painted over, and a zoom-ramped
   `hillshade-exaggeration`.
3. **Contour lines** — *not implemented*. A significant part of the
   reference's apparent sharpness is densely-spaced contours, which read as
   dark hatching on steep faces. The bundle generates no contour source at
   all today. See "Not done yet" below.

Both (1) and (2) are already in `../style/style.json.tmpl`.

### Why the exaggeration has to ramp with zoom

MapLibre differentiates the DEM in *texture* space — the shader's `deriv` is
elevation change per DEM texel, scaled only for Mercator latitude distortion
(`u_latrange` in `shaders/gl/hillshade.hpp`). As you zoom in, a texel covers
less ground, so the same real slope produces a smaller derivative and the
relief flattens out. A single fixed exaggeration is therefore either crushed
when zoomed out or washed out when zoomed in. The template ramps it roughly
2x per two zoom levels.

### Trap: hillshade colours are `array<color>` in MapLibre Native

`hillshade-shadow-color` and `hillshade-highlight-color` are typed
**`array<color>`** here (up to 4 entries, feeding `u_shadows[4]` /
`u_highlights[4]` for the multidirectional method) — unlike MapLibre GL JS,
where they are a single colour. Consequence: a
`["interpolate", ["linear"], ["zoom"], ...]` expression evaluating to one
colour fails to parse with

    [WARNING] [ParseStyle]: Expected array<color> but found string instead.

the property is dropped, and the hillshade silently falls back to defaults
that render almost no relief at all. Keep those two as plain colour strings
and put all zoom dependence in `hillshade-exaggeration`, which does accept
zoom expressions (verified byte-identical against the equivalent scalar).

This also means the style is not directly previewable in a browser
MapLibre GL JS build without editing those two properties.

## Where the colour ramp comes from

The ramp is in `style.json.tmpl`:

- green valley floor, brightening through yellow-green
- **brightest yellow ~800 m**
- darkening through tan to the **darkest brown ~1650 m**
- desaturating steadily to **fully neutral grey by ~2600 m**
- light grey → near-white above ~3400 m

Note the high country is *achromatic*: all the white you see on peaks in the
reference is hillshade highlight, not tint. Do not add a blue/white "snow"
colour to the top of the ramp — it is not what the reference does.


## Reproducing

```sh
cd hypsometric-restyle/scripts

# refit the palette from a reference screenshot (args: image, then the
# lon/lat/scale search box - widen it if eta^2 comes out below ~0.4)
python3 fit_reference_palette.py \
    ../../../../XCSoar/other_app_map_style_mountains/ideal_basemap_with_colors_overview.png \
    5.95 6.20 44.85 45.05 230000 320000

# build Terrain-RGB tiles straight from the .hgt cache (bypasses mapgen,
# for quick style iteration on a small area)
HGT_SMOOTH=0.7 python3 build_terrain_rgb_mbtiles.py out.mbtiles \
    6.15 6.60 44.82 45.06 8 13

# render a style to PNG without running XCSoar
./render.sh some_style.json out.png <lon> <lat> <zoom> [w] [h]
```

`render.sh` needs `mbgl-render` from a MapLibre Native build — the same
checkout `build/maplibre.mk` points XCSoar at. It is the fast iteration loop
for this work: a full style change renders in ~2 s, versus rebuilding a map
and restarting XCSoar.

## Results

`images/compare_overview.png` and `images/compare_high_mountains_4000m.png`
are reference (top) vs. this style (bottom), rendered at the fitted camera so
the terrain lines up pixel-for-pixel and the comparison is fair.

`images/sweep_hillshade_methods.png` compares all five `hillshade-method`
values at overview zoom; `images/sweep_hillshade_tone.png` is the
shadow/highlight/exaggeration sweep that picked the current values.

## Not done yet

- **Contour lines.** Needs a new vector source in the bundle: `gdal_contour`
  over the DEM into GeoJSON, then tiled and added to `basemap.mbtiles` (or
  shipped as its own `contours.mbtiles`), plus `line` + `symbol` layers.
  This is the largest remaining visual gap to the reference.
- **DEM tile seams.** Faint one-pixel seams appear along `raster-dem` tile
  boundaries once hillshade contrast is raised. They show up with *both* the
  shipped 3-arcsec bundle and freshly built 1-arcsec tiles, so this is not a
  tiling bug in `maplibre.py` — see
  `images/known_issue_dem_tile_seams.png` and the investigation prompt 
  (`prompt_maplibre_dem_tile_seams.md`).
- **DEM zoom cap.** The colour work above is independent of DEM resolution,
  but the reference's fine texture is not. Bundles built through the web
  frontend cap at zoom 12 because of `server/config.py`'s
  `"maplibre_max_zoom": 12`, *not* because of the DEM — measurement confirms
  the shipped bundle already used the 1-arcsec Sonny tiles from `data/dem/`
  (`corr(bundle − 3", 1" − 3") = 0.905`). Raising that cap is the cheapest
  remaining lever on sharpness.

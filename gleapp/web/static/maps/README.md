# Vendored map assets

Everything the gallery's map needs, served by GLEAPP's own server. Nothing here is
fetched from the network at run time, and no basemap data ships with GLEAPP: the
examiner imports a basemap file after installation (see the README's Maps section).

| file | source | version | license |
|---|---|---|---|
| maplibre-gl.js, maplibre-gl.css | npm maplibre-gl (dist) | 5.24.0 | BSD-3-Clause, LICENSE-maplibre-gl.txt |
| pmtiles.js | npm pmtiles (dist) | 4.5.0 | BSD-3-Clause, LICENSE-pmtiles.txt |
| layers-dark.json, layers-light.json | generated with npm @protomaps/basemaps (layers("basemap", namedFlavor(flavor), {lang: "en"})) | 5.7.2 | BSD-3-Clause, LICENSE-protomaps-basemaps.txt |
| fonts/Noto Sans {Regular,Medium,Italic} | github.com/protomaps/basemaps-assets, fonts/ | as cloned 2026-09-04 | SIL Open Font License, fonts/OFL.txt |
| sprites/v4/{dark,light}* | github.com/protomaps/basemaps-assets, sprites/v4 | as cloned 2026-09-04 | derived from MIT-licensed tangrams/icons |

MapLibre 5.x is used rather than 6.x because 6.x ships only ES modules and a module
worker; the gallery is a classic-script page and 5.x's single UMD file needs nothing
else. Regenerate the layer files with a newer @protomaps/basemaps only together with
matching sprites, since the style references sprite names.

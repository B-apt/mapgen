# Creates a mapgen-worker instance for building maps
FROM debian:bullseye-slim
ENV MAPWRK_PKGS="ca-certificates python3-jsonschema python3-requests p7zip gdal-bin python3-gdal mapserver-bin wget osmium-tool sqlite3 python3-pip vim less curl unzip"
ENV DEBIAN_FRONTEND=noninteractive

# install base packages needed for mapgen
# python3-gdal provides gdal2tiles.py's osgeo bindings (gdal-bin alone
# ships gdalwarp/gdal_translate/gdalbuildvrt but not gdal2tiles.py's deps)
RUN apt-get update && apt install -y --no-install-recommends $MAPWRK_PKGS && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install numpy rasterio

# Planetiler 0.10.x is compiled for Java 21 class files; bullseye's
# default-jre only ships OpenJDK 11 (UnsupportedClassVersionError), so
# fetch a standalone Temurin 21 JRE instead of relying on apt for Java.
RUN curl -L -o /tmp/jre21.tar.gz \
        https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.11%2B10/OpenJDK21U-jre_x64_linux_hotspot_21.0.11_10.tar.gz \
    && mkdir -p /opt/java21 \
    && tar -xzf /tmp/jre21.tar.gz -C /opt/java21 --strip-components=1 \
    && rm /tmp/jre21.tar.gz
RUN curl -L -o /usr/local/bin/planetiler.jar \
        https://github.com/onthegomap/planetiler/releases/download/v0.10.2/planetiler.jar

# Wrapper script to run planetiler behind proxy (if proxy env variables presents)
COPY container/worker/planetiler /usr/local/bin/planetiler
RUN chmod +x /usr/local/bin/planetiler

# spreet: builds the MapLibre sprite sheet (sprite.png + sprite.json) from
# an SVG icon set. Prebuilt static musl binary - no Rust toolchain needed.
RUN curl -L -o /tmp/spreet.tar.gz \
        https://github.com/flother/spreet/releases/download/v0.13.1/spreet-x86_64-unknown-linux-musl.tar.gz \
    && tar -xzf /tmp/spreet.tar.gz -C /usr/local/bin spreet \
    && chmod +x /usr/local/bin/spreet \
    && rm /tmp/spreet.tar.gz

# MapLibre static assets (~35MB), identical for every job so baked in here.
# Glyphs are prebuilt OFL PBF ranges - no glyph generator needed in the image.
RUN curl -L -o /tmp/noto-sans.zip \
        https://github.com/openmaptiles/fonts/releases/download/v2.0/noto-sans.zip \
    && mkdir -p "/opt/mapgen/maplibre-static/glyphs/Noto Sans Regular" \
    && unzip -j /tmp/noto-sans.zip 'Noto Sans Regular/*' \
        -d "/opt/mapgen/maplibre-static/glyphs/Noto Sans Regular" \
    && rm /tmp/noto-sans.zip

# Sprites: CC0 Maki icon set packed with spreet (installed above)
RUN curl -L -o /tmp/maki.zip \
        https://github.com/mapbox/maki/archive/refs/heads/main.zip \
    && unzip -j /tmp/maki.zip 'maki-main/icons/*.svg' -d /tmp/maki_icons \
    && mkdir -p /opt/mapgen/maplibre-static/sprites \
    && spreet /tmp/maki_icons /opt/mapgen/maplibre-static/sprites/sprite \
    && spreet --ratio 2 /tmp/maki_icons /opt/mapgen/maplibre-static/sprites/sprite@2x \
    && rm -rf /tmp/maki.zip /tmp/maki_icons

# Copy python scripts of mapgen to target
COPY lib/ /opt/mapgen/lib/
COPY bin/ /opt/mapgen/bin/

# Last layer: editing the style then rebuilds only this ~30kB layer
COPY xcsoar-maplibre-poc/style/style.json.tmpl /opt/mapgen/maplibre-static/style.json.tmpl

# Volume for data caching and jobs
VOLUME /opt/mapgen/data
VOLUME /opt/mapgen/jobs

# worker process
ENTRYPOINT ["/opt/mapgen/bin/mapgen-worker"]

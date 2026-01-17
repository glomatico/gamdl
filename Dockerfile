FROM python:3.11-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir --user gamdl

# Build Bento4 from source
FROM python:3.11-slim AS bento4-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    cmake \
    make \
    g++ \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/axiomatic-systems/Bento4.git /tmp/bento4 && \
    cd /tmp/bento4 && \
    mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    make mp4decrypt && \
    cp mp4decrypt /usr/local/bin/

# Build GPAC (MP4Box) from source
FROM python:3.11-slim AS gpac-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    pkg-config \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/gpac/gpac.git /tmp/gpac && \
    cd /tmp/gpac && \
    ./configure --static-bin && \
    make -j$(nproc) && \
    cp bin/gcc/MP4Box /usr/local/bin/

FROM python:3.11-slim

# Install FFmpeg, libicu (for N_m3u8DL-RE), and other dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    libicu76 \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Copy mp4decrypt from builder
COPY --from=bento4-builder /usr/local/bin/mp4decrypt /usr/local/bin/

# Copy MP4Box from builder
COPY --from=gpac-builder /usr/local/bin/MP4Box /usr/local/bin/

# Download N_m3u8DL-RE - architecture aware, fetches latest release
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        N_M3U8_ARCH="linux-x64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        N_M3U8_ARCH="linux-arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    DOWNLOAD_URL=$(curl -s https://api.github.com/repos/nilaoda/N_m3u8DL-RE/releases/latest | jq -r ".assets[] | select(.name | contains(\"${N_M3U8_ARCH}\") and (contains(\"musl\") | not)) | .browser_download_url") && \
    curl -L "$DOWNLOAD_URL" -o /tmp/nm3u8.tar.gz && \
    tar -xzf /tmp/nm3u8.tar.gz -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/N_m3u8DL-RE && \
    rm /tmp/nm3u8.tar.gz

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

WORKDIR /app
ENTRYPOINT ["gamdl"]

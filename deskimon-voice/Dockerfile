ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

LABEL \
    io.hass.version="0.1.0" \
    io.hass.type="addon" \
    io.hass.arch="aarch64|amd64"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-numpy \
        python3-venv \
        ffmpeg \
        git \
        avahi-utils \
        dbus \
        pulseaudio-utils \
        alsa-utils \
        libmpv-dev \
        libasound2-plugins \
        ca-certificates \
        build-essential \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ARG LVA_VERSION=v1.1.9
RUN git clone --depth 1 --branch "${LVA_VERSION}" \
        https://github.com/OHF-Voice/linux-voice-assistant.git /opt/lva \
    && cd /opt/lva \
    && touch linux_voice_assistant/player/__init__.py \
    && sed -i 's/include = \["linux_voice_assistant"\]/include = ["linux_voice_assistant*"]/' pyproject.toml \
    && pip3 install --break-system-packages --no-cache-dir . \
    && rm -rf /opt/lva/.git

COPY deskimon_bridge/ /opt/deskimon_bridge/

COPY rootfs/ /

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

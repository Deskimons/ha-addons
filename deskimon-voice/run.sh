#!/usr/bin/with-contenv bashio
set -euo pipefail

DEVICE_NAME="$(bashio::config 'device_name')"
RELAY_PORT="$(bashio::config 'relay_port')"
ESPHOME_PORT="$(bashio::config 'esphome_port')"
DEBUG="$(bashio::config 'debug')"

HA_URL="http://homeassistant:8123"

if command -v avahi-daemon &>/dev/null; then
    if [ ! -S /var/run/dbus/system_bus_socket ]; then
        mkdir -p /var/run/dbus
        dbus-daemon --system --nofork &
        sleep 1
    fi

    avahi-daemon --daemonize --no-chroot 2>/dev/null || true

    avahi-publish-service \
        "Deskimon Voice Relay" \
        "_deskimon-relay._tcp" \
        "${RELAY_PORT}" \
        "version=0.1.0" &
    bashio::log.info "mDNS: advertising _deskimon-relay._tcp on port ${RELAY_PORT}"
fi

CMD_ARGS=(
    python3 -m deskimon_bridge
    --name "${DEVICE_NAME}"
    --port "${ESPHOME_PORT}"
    --host "0.0.0.0"
    --relay-port "${RELAY_PORT}"
    --ha-url "${HA_URL}"
)

if bashio::var.true "${DEBUG}"; then
    CMD_ARGS+=( --debug )
fi

bashio::log.info "Starting Deskimon Voice Assistant..."
bashio::log.info "  Device name : ${DEVICE_NAME}"
bashio::log.info "  Relay port  : ${RELAY_PORT}"
bashio::log.info "  ESPHome port: ${ESPHOME_PORT}"

export PYTHONPATH="/opt:${PYTHONPATH:-}"

exec "${CMD_ARGS[@]}"

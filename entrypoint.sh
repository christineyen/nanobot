#!/bin/sh
# Write config.json from Fly secret if provided
if [ -n "$NANOBOT_CONFIG_JSON" ]; then
  mkdir -p /root/.nanobot
  echo "$NANOBOT_CONFIG_JSON" > /root/.nanobot/config.json
fi

exec nanobot "$@"

#!/bin/sh
# Start the Yeeps bot: shows the menu, then type commands (rain, fingergun, mimic NAME, ...)
cd "$(dirname "$0")" && exec python3 bot.py "$@"

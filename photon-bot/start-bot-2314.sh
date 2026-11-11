#!/bin/sh
# Start the Yeeps bot for 2.31.4-blaze (biglive) players.
# Uses the biglive env (Photon app 07a899cb, AppVersion 2.31.0_2.45, AUTH=userid,
# DB Yeeps240) and its own talk port so it can run alongside the 1.31 bot.
#   ./start-bot-2314.sh listen --target <name>     (observe a player)
#   ./start-bot-2314.sh run --target <name>        (interactive command menu)
#   ./start-bot-2314.sh                            (menu, no target)
cd "$(dirname "$0")" && exec env \
  YEEPS_BOT_ENV="$HOME/.config/yeeps/bot-2314.env" \
  YEEPS_TALK_PORT="${YEEPS_TALK_PORT:-8792}" \
  python3 bot.py "$@"

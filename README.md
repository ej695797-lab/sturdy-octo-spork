# Yeeps 1.31.0-blaze (self-host kit)

Run your own Yeeps 1.31.0-blaze server on your own PC, with your own Photon apps.
Your players install an APK made for your server. Nothing here talks to anyone
else's game server.

Join the Discord: https://discord.gg/VhFmdAyXGk

Thanks to @Mixid for the aws api!

> **Status (kit1):** tested end to end, including a host running their own server and
> playing on a headset. Problems or questions: ask in the Discord.

## Made for Claude

This kit is made to be set up with **[Claude Code](https://claude.com/claude-code)**, an
AI assistant that works on your PC. You don't need to know Python or Android tools:
the repo has a guide written for Claude (`CLAUDE.md`), and Claude follows it to do the
setup with you.

1. Install Claude Code from https://claude.com/claude-code.
2. Download this repo (green **Code** button > **Download ZIP**, then unzip it, or
   `git clone` it) and open the folder in Claude Code.
3. Ask it what you want, for example:
   - "Set up my server and make my APK"
   - "Help me make my Photon apps"
   - "Let players on other networks join my server"
   - "Put my song on a record"
   - "Add a picture block of my dog"
   - "I want maps"

Claude runs the steps on your own PC and tells you when it needs something from you
(your Photon App IDs, a headset plugged in). The rest of this README is the same steps
by hand, if you'd rather do it yourself.

## What you get

- **The APK** (in [Releases](../../releases)): the original Yeeps 1.31.0 game with
  GameAnalytics tracking blocked, the version shown as "1.31.0-blaze", the BLAZE
  block, and a news-board Discord button. No other added content.
- **A server** that runs in a console window on your PC (`start.bat` on Windows,
  `start.sh` on Linux/macOS): logins, the world, private worlds, moderation, and a
  web editor at `/editor`, including button controls for the Photon bot.
- **The 1.31 world layout**: Starter House, Playground, Blue House, the Wilds, the
  tutorial, the Build-A-Map rooms and the rest, with **no maps built in them**: every
  room starts empty. See [Maps](#maps).
- **Tools** to make your APK, check your server, and add your own blocks and music.

Not included: community worlds or live player lookups from real Yeeps. An optional Discord
profile bot can link accounts and edit usernames and colors.

## What you need

- A PC that stays on while people play (Windows, Linux or macOS).
- **Python 3.11+**: on Windows get it from https://www.python.org/downloads/ and tick
  "Add python.exe to PATH" (the "python" that opens the Microsoft Store doesn't work).
- **A database**: [MongoDB Community Server](https://www.mongodb.com/try/download/community)
  on the same PC (default settings are fine), or a free
  [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) cluster.
- **A free [Photon](https://dashboard.photonengine.com) account.**
- **To make your APK: Java and Android build-tools.**
  - Java: [Eclipse Temurin JDK](https://adoptium.net) (Windows: tick "Add to PATH").
  - Android build-tools (`zipalign`, `apksigner`): install
    [Android Studio](https://developer.android.com/studio) and add "Android SDK
    Build-Tools" in its SDK Manager, or the command-line tools and run
    `sdkmanager "build-tools;34.0.0"`. Then add the build-tools folder to PATH, or set
    `ANDROID_HOME` (Windows: usually `%LOCALAPPDATA%\Android\Sdk`).
- **Quest headsets** with developer mode, and [SideQuest](https://sidequestvr.com) or `adb`.

## Setup

### 1. Make two Photon apps

At https://dashboard.photonengine.com create a **PUN** app (Multiplayer Game, Photon PUN)
and a **Voice** app (Photon Voice). Copy both App IDs. Game clients do not need custom
authentication; the optional editor bot setup below does.

### 2. Start the server

Windows: double-click `start.bat`. Linux/macOS: `./start.sh`.

The first run installs the Python packages and creates `server/.env` with a random
editor password (shown in the window). Then edit `server/.env`:

- `MONGO_URI`: leave the default for a local MongoDB, or paste your Atlas connection string.
- `PUBLIC_BASE_URL`: the address players' headsets will use (see step 3).

Close the window and start it again after editing `.env`. Keep it open while people play.

Check it: `python tools/smoke_test.py http://127.0.0.1:8000 --editor-password <password>`
should end with `ALL PASS`.

The editor's Photon bot is optional. Set `BOT_KEY` to a private random value and
`PHOTON_APP_ID` to the PUN App ID used in the host's APK, then configure the PUN app's
Photon custom-auth URL to the server's `PUBLIC_BASE_URL`. To authenticate game clients
on Photon Voice too, set the Voice app's custom-auth URL to the same `PUBLIC_BASE_URL`;
the server applies its account, headset, and build checks to both apps. Keep the Voice
App ID in the APK as usual. The Bot tab can then hook the
bot to an online player. It provides game, arcade, cleanup, power and effect buttons,
including Simon Says, Memory Match, Flappy Bird, Minesweeper, two-player Tic-Tac-Toe and
Connect Four, plus follow, adjustable orbit presets and figure-eight bot movement. It has
no build, live-talk or view controls.
Upload an MP4 in the Bot tab to show video frames, or an MP3 for an audio waveform; playback
uses pillow blocks for the screen or the first sign placed by the tracked player. Media is
stored in MongoDB, not in the repository.
The Bot tab also accepts an image for a pillow pixel-art mosaic and audio for SoundKey
playback. Set `YEEPS_VOICE_APP` to the Photon Voice App ID from the Photon dashboard for
bot SoundKey and Bad Apple audio; without a valid Voice App ID, visual animations still
play without sound. `YEEPS_VOICE_AUTH` is optional custom-auth query data for the bot's
Voice connection only; player Voice authentication uses the Voice app's custom-auth URL
and does not require that bot setting.

The optional Discord profile bot is disabled unless `DISCORD_BOT_TOKEN` is configured.
Create a Discord application and bot, invite it with the `bot` and `applications.commands`
scopes, and set the token as a private environment variable. `DISCORD_GUILD_ID` is optional
and makes slash commands appear in that server immediately; without it, global commands may
take time to propagate. In the editor's Bot tab, generate a one-use code for the exact Yeeps
username and give it privately to that player. The player runs `/link` with their username
and code in Discord, then `/profile` to choose username, skin color, or eye color. Codes
expire after 10 minutes; colors accept six-digit hex such as `#7A42C8`. Keep the bot token
out of source control and use Render's environment settings when hosted there.

### 3. Pick your server's address

The headsets need to reach your PC. Pick one:

- **Same Wi-Fi only:** `http://<your PC's local IP>:8000`, e.g. `http://192.168.1.20:8000`
  (Windows: `ipconfig` shows it). Your firewall must allow the port: Windows asks the
  first time the server starts (allow "Private networks"); on Linux with ufw run
  `sudo ufw allow 8000/tcp`.
- **Over the internet, fixed address (best):** a Cloudflare named tunnel with your own
  domain, e.g. `https://yeeps.your-domain.com`, pointed at `http://127.0.0.1:8000`
  (see Cloudflare's "Create a tunnel" guide).
- **Quick internet test:** `start.bat --tunnel` / `./start.sh --tunnel` opens a free
  `https://....trycloudflare.com` address (Windows: `winget install --id Cloudflare.cloudflared`
  first). It changes every time, so use it only for testing (see "Override file").

Put the same address in `PUBLIC_BASE_URL` in `server/.env`.

### 4. Make your APK

Download `Yeeps-1.31.0-blaze-kit1.apk` from Releases, then:

```
python tools/configure_apk.py --apk Yeeps-1.31.0-blaze-kit1.apk --server http://192.168.1.20:8000 --photon-app-id <PUN app id> --photon-voice-app-id <Voice app id>
```

It writes your settings into the APK and signs it with your own key (created in
`tools/.keys/` the first time: keep that folder, or later APKs won't install over
earlier ones). The output is `Yeeps-1.31.0-blaze-<your server>.apk`.

### 5. Install it on each headset

With SideQuest, or `adb install -r <your apk>`. If another Yeeps build is already
installed, uninstall it first: a different signing key can't update it.

### 6. Use the editor

Open `<your server>/editor` and sign in with `EDITOR_PASSWORD` from `server/.env`:
players, bans, kicks, the news board, rooms, maps, disks, and more. Set a separate,
long, unique `MODERATION_PASSWORD` in the server environment. The editor asks for it
again for every ban, unban, kick, or account deletion; the server rejects these
requests and direct edits to ban fields until the variable is configured. On Render,
add it as a private service environment variable and redeploy; locally, put it in
`server/.env`.

## Maps

The kit has no map builds. To get maps, use
[YeepsMapLoader](https://github.com/Blazeisodd/YeepsMapLoader) (a Unity tool that loads
real Yeeps rooms), then paste the room's block list (JSON) into **Maps > Import a map
into a room** in the editor. Blocks that don't exist in 1.31 are dropped on import.
You can also build in game: set `UNLOCK_ALL_MODES=1` in `.env` and restart while you
build. Then anyone can switch rooms to creative and their builds are saved, so turn
it off again when your maps are done (rooms are editor-only otherwise).

## Your own music (disks)

- **Easiest, no new APK:** the editor's **Disks** tab. Pick a record, pick an audio
  file (mp3, ogg, wav, ...) and Upload: every player hears it on that record the next
  time they start the game. Your browser converts it to WAV (OGG is sent as is); about
  4 minutes fit.
- **Inside the APK (permanent):**
  ```
  python tools/data/extract_data.py Yeeps-1.31.0-blaze-kit1.apk data
  python tools/data/bake_disk.py data --list
  python tools/data/bake_disk.py data --clip SuburbTheme --audio song.mp3 --art recordArtTexture_suburbTheme --label cover.png
  python tools/configure_apk.py ... --data data
  ```

## Your own blocks (prefabs)

Add a picture block (a cube with your picture on every side):

```
python tools/data/extract_data.py Yeeps-1.31.0-blaze-kit1.apk data
python tools/data/add_picture_block.py data/data.unity3d data/data.unity3d --key 1x1_stuffedMyDog --name "My Dog" --image dog.png
python tools/configure_apk.py ... --data data
```

Then give players the block: in the editor add the key (here `1x1_stuffedMyDog`) to
`ownedPatterns` (Default player data for new players, or each player).

**The BLAZE block** (`1x5x2_stuffedTextBlaze`) is only for players with the `blaze`
role: give it to someone in the editor (Players tab, roleKeys).

## Override file (testing)

A headset can use a different server without a new APK. Put a file named
`blaze-config.txt` at `/sdcard/Android/data/com.TrassGames.Yeeps/files/` with:

```
server=https://whatever.trycloudflare.com
photon_app_id=<PUN app id>
photon_voice_app_id=<Voice app id>
```

`python tools/configure_apk.py ... --settings-only` prints this file for you.

## Troubleshooting

- **"address already in use" when the server starts:** another program uses port 8000.
  Set `PORT=8001` (or any free port) in `server/.env`, and use that port in
  `PUBLIC_BASE_URL` and in the APK's `--server`.
- **"Install Python 3.11 or newer"** on Windows: install it from python.org with
  "Add python.exe to PATH" ticked, then open a new window.
- **The game can't reach the server:** check `PUBLIC_BASE_URL`, the `--server` you built
  the APK with, and your firewall. From the headset's network,
  `<server>/health` should answer.

## Disclaimer

A fan-made private server project. Not affiliated with or endorsed by Trass Games or
Yeeps. Don't use it to cheat in, or harm, the real game.

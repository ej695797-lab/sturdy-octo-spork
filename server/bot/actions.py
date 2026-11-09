"""Allowlisted actions exposed by the editor's Photon bot controls."""

ACTION_COMMANDS = {
    "disco-start": "disco",
    "koth-start": "koth",
    "koth-stop": "koth off",
    "potato-start": "potato",
    "potato-stop": "potato off",
    "boss-start": "boss",
    "boss-pause": "boss pause",
    "boss-resume": "boss resume",
    "boss-stop": "boss off",
    "map-start": "map",
    "map-stop": "map off",
    "tetris": "tetris",
    "snake": "snake",
    "pong": "pong",
    "pong-2p": "pong 2p",
    "canvas": "canvas",
    "canvas-stop": "canvas off",
    "game-stop": "game off",
    "apple": "apple",
    "apple-small": "apple small",
    "apple-mute": "apple mute",
    "apple-stop": "apple off",
    "applesign": "applesign",
    "applesign-stop": "applesign off",
    "item-gun": "itemgun",
    "item-gun-off": "itemgun off",
    "cleanup": "cleanup",
    "pellet-gun": "pelletfx",
    "pellet-gun-off": "pelletfx off",
    "lightning-hand": "lightning",
    "lightning-off": "lightning off",
    "freeze-hand": "freeze",
    "freeze-hand-off": "freeze off",
    "finger-gun": "fingergun",
    "powers-off": "fingergun off",
    "smite": "smite",
    "freeze-player": "ice",
    "thaw": "thaw",
    "scare-hand": "scare",
    "jumpscare": "jumpscare",
    "scare-off": "scare off",
    "godhand": "godhand",
    "godhand-both": "godhand both",
    "godhand-off": "godhand off",
    "tracked-fly": "fly",
    "tracked-fly-off": "fly off",
    "blackhole": "blackhole",
    "meteors": "meteors",
    "rain": "rain",
    "rain-off": "rain off",
    "rain-clean": "rain clean",
    "boom": "boom",
    "coins-planet": "coins planet",
    "coins-off": "coins off",
    "mimic": "mimic",
    "mimic-stop": "stop",
    "players": "players",
}

TARGET_ACTIONS = {"smite", "freeze-player", "jumpscare", "mimic"}
MEDIA_ACTIONS = {"apple", "apple-small", "apple-mute", "apple-stop",
                 "applesign", "applesign-stop"}
ITEM_ACTIONS = {"item-gun"}
ITEM_CATALOG = {
    "buttcoin": "Buttcoin",
    "freezeGrenade": "Freeze grenade",
    "throwingBasicBomb": "Basic bomb",
    "throwingImpulseBomb": "Impulse bomb",
    "throwingPumpkinBomb": "Pumpkin bomb",
    "throwingRadiationBomb": "Radiation bomb",
}


def command_for(action: str, target_name: str = "", item_id: str = "") -> str | None:
    """Return a command only for a known action; never pass arbitrary UI text through."""
    command = ACTION_COMMANDS.get(action)
    if command is None:
        return None
    if action in TARGET_ACTIONS:
        target_name = " ".join(str(target_name).split())[:64]
        if not target_name:
            return None
        command = f"{command} {target_name}"
    if action in ITEM_ACTIONS:
        if item_id not in ITEM_CATALOG:
            return None
        command = f"{command} {item_id}"
    return command

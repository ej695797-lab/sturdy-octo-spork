"""Optional Discord commands for linking and editing Yeeps profiles."""

from __future__ import annotations

import hashlib
import os
import re
import time

import discord
from discord import app_commands
from pymongo.errors import DuplicateKeyError

from services.db import accounts, db
from services.names import is_visible_name

_links = db["discord_profile_links"]
_codes = db["discord_link_codes"]
_client: discord.Client | None = None


def discord_profile_bot_status() -> dict:
    return {
        "configured": bool(os.getenv("DISCORD_BOT_TOKEN", "").strip()),
        "connected": bool(_client and _client.is_ready()),
    }


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("ascii")).hexdigest()


async def _accounts_named(name: str) -> list[dict]:
    cursor = accounts.find(
        {"displayName": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}},
        {"_id": 0, "accountID": 1, "displayName": 1},
    ).limit(2)
    return await cursor.to_list(length=2)


class NameModal(discord.ui.Modal, title="Change Yeeps username"):
    name = discord.ui.TextInput(label="New username", min_length=1, max_length=24)

    def __init__(self, account_id: str):
        super().__init__()
        self.account_id = account_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.name.value).strip()
        if not is_visible_name(value) or any(c in value for c in "<>\r\n"):
            await interaction.response.send_message(
                "Choose a visible username up to 24 characters without markup.", ephemeral=True)
            return
        result = await accounts.update_one(
            {"accountID": self.account_id},
            {"$set": {"displayName": value, "displayName_searchable": value.lower()}},
        )
        if not result.matched_count:
            await interaction.response.send_message("That Yeeps account no longer exists.", ephemeral=True)
            return
        await interaction.response.send_message("Your Yeeps username was updated.", ephemeral=True)


class ColorModal(discord.ui.Modal):
    color = discord.ui.TextInput(
        label="Color as hex, for example #7A42C8",
        min_length=6,
        max_length=7,
        placeholder="#7A42C8",
    )

    def __init__(self, account_id: str, field: str):
        super().__init__(title=f"Change {field.replace('Color', '').lower()} color")
        self.account_id = account_id
        self.field = field

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.color.value).strip()
        if value.startswith("#"):
            value = value[1:]
        if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
            await interaction.response.send_message(
                "Enter exactly six hexadecimal digits, such as #7A42C8.", ephemeral=True)
            return
        packed = int(value + "FF", 16)
        if packed >= 0x80000000:
            packed -= 0x100000000
        result = await accounts.update_one(
            {"accountID": self.account_id}, {"$set": {self.field: packed}})
        if not result.matched_count:
            await interaction.response.send_message("That Yeeps account no longer exists.", ephemeral=True)
            return
        label = "skin" if self.field == "skinColor" else "eye"
        await interaction.response.send_message(f"Your Yeeps {label} color was updated.", ephemeral=True)


class ColorChoice(discord.ui.Select):
    def __init__(self, account_id: str):
        super().__init__(
            placeholder="Choose skin or eye color",
            options=[
                discord.SelectOption(label="Skin color", value="skinColor"),
                discord.SelectOption(label="Eye color", value="eyeColor"),
            ],
        )
        self.account_id = account_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ColorModal(self.account_id, self.values[0]))


class ColorChoiceView(discord.ui.View):
    def __init__(self, account_id: str):
        super().__init__(timeout=180)
        self.add_item(ColorChoice(account_id))


class ProfileChoice(discord.ui.Select):
    def __init__(self, account_id: str):
        super().__init__(
            placeholder="Choose what to edit",
            options=[
                discord.SelectOption(label="Username", value="username"),
                discord.SelectOption(label="Color", value="color"),
            ],
        )
        self.account_id = account_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "username":
            await interaction.response.send_modal(NameModal(self.account_id))
        else:
            await interaction.response.send_message(
                "Choose which color to change:", view=ColorChoiceView(self.account_id), ephemeral=True)


class ProfileChoiceView(discord.ui.View):
    def __init__(self, account_id: str):
        super().__init__(timeout=180)
        self.add_item(ProfileChoice(account_id))


def _make_client() -> discord.Client:
    class ProfileClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(intents=discord.Intents.none())
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self) -> None:
            await _links.create_index("discordID", unique=True)
            await _links.create_index("accountID", unique=True)
            await _codes.create_index("expiresAt")
            guild_value = os.getenv("DISCORD_GUILD_ID", "").strip()
            if guild_value:
                guild = discord.Object(id=int(guild_value))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()

    client = ProfileClient()
    tree = client.tree

    @client.event
    async def on_ready() -> None:
        print(f"[discord-profile] logged in as {client.user}")

    @tree.command(name="link", description="Link your Yeeps account with a host-issued pairing code")
    @app_commands.describe(username="The exact username shown in Yeeps", code="The one-time code from the server host")
    async def link(interaction: discord.Interaction, username: str, code: str) -> None:
        if not re.fullmatch(r"[A-HJ-NP-Z2-9]{10}", code.strip().upper()):
            await interaction.response.send_message("That pairing code is invalid.", ephemeral=True)
            return
        matches = await _accounts_named(username)
        if len(matches) != 1:
            await interaction.response.send_message(
                "I could not identify exactly one Yeeps account with that username. Ask the host to issue a code for your account.",
                ephemeral=True,
            )
            return
        account_id = matches[0]["accountID"]
        discord_id = str(interaction.user.id)
        existing = await _links.find_one({"$or": [{"discordID": discord_id}, {"accountID": account_id}]})
        if existing:
            await interaction.response.send_message(
                "That Discord account or Yeeps account is already linked.", ephemeral=True)
            return
        pair = await _codes.find_one_and_delete({
            "accountID": account_id,
            "codeHash": _code_hash(code),
            "expiresAt": {"$gt": time.time()},
        })
        if not pair:
            await interaction.response.send_message(
                "That pairing code is invalid, expired, or for a different Yeeps account.", ephemeral=True)
            return
        try:
            await _links.insert_one({
                "discordID": discord_id,
                "accountID": account_id,
                "linkedAt": time.time(),
            })
        except DuplicateKeyError:
            await interaction.response.send_message(
                "That Discord account or Yeeps account is already linked.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Linked to Yeeps account {matches[0].get('displayName') or account_id}. Use /profile to edit it.",
            ephemeral=True,
        )

    @tree.command(name="profile", description="Edit the username or colors on your linked Yeeps account")
    async def profile(interaction: discord.Interaction) -> None:
        record = await _links.find_one({"discordID": str(interaction.user.id)}, {"_id": 0, "accountID": 1})
        if not record:
            await interaction.response.send_message("Link your Yeeps account first with /link.", ephemeral=True)
            return
        account = await accounts.find_one({"accountID": record["accountID"]}, {"_id": 0, "displayName": 1})
        if not account:
            await interaction.response.send_message("Your linked Yeeps account no longer exists.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Editing Yeeps account {account.get('displayName') or record['accountID']}:",
            view=ProfileChoiceView(record["accountID"]),
            ephemeral=True,
        )

    return client


async def run_discord_profile_bot(token: str) -> None:
    global _client
    _client = _make_client()
    await _client.start(token)


async def stop_discord_profile_bot() -> None:
    if _client and not _client.is_closed():
        await _client.close()

from collections import defaultdict
import re
import asyncio
import io
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands

from app import (
    ITEMS_CATALOG,
    RECENT_RAW_ADS,
    SNIPED_DEALS,
    _resolve_item_inputs,
    catalog_client,
    db,
    poll_rolimons_ads_once,
    refresh_items_catalog,
    roblox,
    sales_client,
    seed_reference_calibration_data,
    sync_db_from_github_if_configured,
    trade_engine,
)
from db import OWNER_DISCORD_ID
from oplookup import compute_op_lookup, resolve_model_label
from proofs import find_item_by_query, format_k, format_relative_time, get_item_proofs_formatted
from render import render_trade_card_png, render_user_profile_graph
from sniper import AdSniper
from suggest import generate_trade_suggestions
from value_requests import parse_value_request_text, save_parsed_value_request_proof

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.toml"
FOOTER_TEXT = "totally not a market tool starting with V"

MODEL_CHOICES = [
    app_commands.Choice(name="Carbon Star 1.1 (Experimental Fully Dynamic)", value="star"),
    app_commands.Choice(name="Carbon Max 5.1 (Strict High-Precision)", value="max"),
    app_commands.Choice(name="Carbon Hume 4.2 (Expert Dynamic)", value="hume"),
    app_commands.Choice(name="Carbon Legacy 2.1 (Classic Proof + Demand)", value="legacy"),
]


def _apply_footer(embed: discord.Embed, extra: str = "") -> discord.Embed:
    text = f"{extra} • {FOOTER_TEXT}" if extra else FOOTER_TEXT
    embed.set_footer(text=text)
    return embed


def _load_bot_token() -> str:
    env_tok = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if env_tok:
        return env_tok
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
        return str(cfg.get("discord", {}).get("bot_token", "")).strip()
    return ""


class CarbonDiscordApp(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await asyncio.to_thread(refresh_items_catalog, True)
        await asyncio.to_thread(seed_reference_calibration_data, db, ITEMS_CATALOG)
        await asyncio.to_thread(sync_db_from_github_if_configured)
        await asyncio.to_thread(poll_rolimons_ads_once)
        synced = await self.tree.sync()
        print(f"[Carbon Discord App] Synced {len(synced)} User-Installable App commands: {[c.name for c in synced]}")
        self.loop.create_task(self._periodic_sync_loop())

    async def on_ready(self) -> None:
        print(f"[Carbon Discord App] Logged in as {self.user} (App ID: {self.application_id})")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.content and not message.attachments:
            return
        low = (message.content or "").lower()
        if any(k in low for k in ("sender:", "receiver:", "values:", "s:", "r:")):
            img_bytes = None
            if message.attachments:
                try:
                    img_bytes = await message.attachments[0].read()
                except Exception:
                    img_bytes = None
            parsed = await asyncio.to_thread(
                parse_value_request_text, message.content, ITEMS_CATALOG, img_bytes
            )
            if parsed:
                await asyncio.to_thread(save_parsed_value_request_proof, db, parsed)

    async def _periodic_sync_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.to_thread(refresh_items_catalog, False)
                await asyncio.to_thread(sync_db_from_github_if_configured)
                await asyncio.to_thread(poll_rolimons_ads_once)
            except Exception:
                pass
            await asyncio.sleep(30)


client = CarbonDiscordApp()


def _check_whitelisted(interaction: discord.Interaction) -> bool:
    return db.is_whitelisted(interaction.user.id)


def _check_owner(interaction: discord.Interaction) -> bool:
    return int(interaction.user.id) == OWNER_DISCORD_ID


async def _item_autocomplete(
    interaction: discord.Interaction, current: str
) -> List[app_commands.Choice[str]]:
    q = current.strip().lower()
    if not ITEMS_CATALOG:
        refresh_items_catalog(force=False)
    matches = []
    for it in ITEMS_CATALOG.values():
        acr = it.get("acronym", "")
        name = it["name"]
        if not q or q in name.lower() or (acr and q in acr.lower()) or q == str(it["item_id"]):
            label = f"{name} [{acr}] ({it['value']:,} Val)" if acr else f"{name} ({it['value']:,} Val)"
            matches.append((int(it.get("is_valued", False)), it["value"], label[:100], str(it["item_id"])))
    matches.sort(key=lambda x: (-x[0], -x[1]))
    return [app_commands.Choice(name=m[2], value=m[3]) for m in matches[:25]]


async def _ensure_thumbs_and_render(
    items_a: List[Dict[str, Any]], items_b: List[Dict[str, Any]]
) -> bytes:
    all_ids = [int(x["item_id"]) for x in items_a + items_b if int(x.get("item_id", 0)) > 0]
    thumbs = await asyncio.to_thread(roblox.fetch_thumbnails, all_ids)
    if thumbs:
        db.update_thumbnails(thumbs)
        for iid, url in thumbs.items():
            if iid in ITEMS_CATALOG:
                ITEMS_CATALOG[iid]["thumbnail_url"] = url
        for x in items_a + items_b:
            if int(x.get("item_id", 0)) in thumbs:
                x["thumbnail_url"] = thumbs[int(x["item_id"])]
    return await asyncio.to_thread(render_trade_card_png, items_a, items_b)


class PaginatedProofsView(discord.ui.View):
    def __init__(
        self,
        pages: List[Dict[str, Any]],
        header_title: str,
    ) -> None:
        super().__init__(timeout=300)
        self.pages = pages
        self.header_title = header_title
        self.idx = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.idx <= 0
        self.next_btn.disabled = self.idx >= len(self.pages) - 1

    async def build_page_payload(self) -> tuple[discord.Embed, discord.File]:
        for idx_p, p in enumerate( self.pages ):
            for side_k, v_k, r_k in (("items_a", "value_a", "rap_a"), ("items_b", "value_b", "rap_b")):
                for x in p.get(side_k, []):
                    iid_x = int(x.get("item_id", 0))
                    cat_it = ITEMS_CATALOG.get(iid_x)
                    if cat_it:
                        if int(x.get("value", 0)) <= 0:
                            x["value"] = int(cat_it.get("value", 0))
                        if int(x.get("rap", 0)) <= 0:
                            x["rap"] = int(cat_it.get("rap", 0))
                        if not x.get("name") or str(x["name"]).startswith("Item #"):
                            x["name"] = cat_it["name"]
                if int(p.get(v_k, 0)) <= 0:
                    p[v_k] = sum(int(x.get("value", 0)) for x in p.get(side_k, []))
                if int(p.get(r_k, 0)) <= 0:
                    p[r_k] = sum(int(x.get("rap", 0)) for x in p.get(side_k, []))

        p = self.pages[self.idx]
        uid_a = int(p.get("user_a_id", 0))
        uid_b = int(p.get("user_b_id", 0))
        uname_a = str(p.get("user_a_name", ""))
        uname_b = str(p.get("user_b_name", ""))
        if (not uname_a or uname_a.startswith("User_")) or (not uname_b or uname_b.startswith("User_")):
            resolved = await asyncio.to_thread(db.resolve_missing_trader_names, [uid_a, uid_b])
            if uid_a in resolved:
                uname_a = resolved[uid_a]
                p["user_a_name"] = uname_a
            if uid_b in resolved:
                uname_b = resolved[uid_b]
                p["user_b_name"] = uname_b

        sa = "\n".join(
            f"- **{x['name']}** ({x.get('value', 0):,})" for x in p["items_a"]
        )
        sb = "\n".join(
            f"- **{x['name']}** ({x.get('value', 0):,})" for x in p["items_b"]
        )
        op_diff = int(p.get("value_b", 0)) - int(p.get("value_a", 0))
        op_abs = int(p.get("op_value", abs(op_diff)))
        age_str = p.get("age") or format_relative_time(int(p.get("detected_at", time.time())))
        t_type = p.get("trade_type", "SWAP")

        link_a = f"[{uname_a}](https://www.rolimons.com/player/{uid_a})" if uid_a > 1000 and uid_a not in (888002, 888003, 999001, 999002) else f"**{uname_a}**"
        link_b = f"[{uname_b}](https://www.rolimons.com/player/{uid_b})" if uid_b > 1000 and uid_b not in (888002, 888003, 999001, 999002) else f"**{uname_b}**"

        desc_lines = [
            f"🕒 **Recorded**: `{age_str}`  •  **Type**: `{t_type}`  •  **Overpay**: `{op_abs:,}` (`{format_k(op_abs)} OP`)",
            f"👤 **Traders**: {link_a} ⇄ {link_b}",
            "",
            f"📤 **Side A (`{p.get('value_a', 0):,}` Val / `{p.get('rap_a', 0):,}` RAP)**\n{sa}",
            "",
            f"📥 **Side B (`{p.get('value_b', 0):,}` Val / `{p.get('rap_b', 0):,}` RAP)**\n{sb}",
        ]

        embed = discord.Embed(
            title=f"{self.header_title} — Proof {self.idx + 1}/{len(self.pages)}",
            description="\n".join(desc_lines),
            color=0x0EA5E9,
        )
        _apply_footer(embed, f"Proof {self.idx + 1} of {len(self.pages)}")

        png_bytes = await _ensure_thumbs_and_render(p["items_a"], p["items_b"])
        fname = f"proof_{self.idx + 1}.png"
        file = discord.File(io.BytesIO(png_bytes), filename=fname)
        embed.set_image(url=f"attachment://{fname}")
        return embed, file

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.idx > 0:
            self.idx -= 1
        self._update_buttons()
        await interaction.response.defer()
        embed, file = await self.build_page_payload()
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.idx < len(self.pages) - 1:
            self.idx += 1
        self._update_buttons()
        await interaction.response.defer()
        embed, file = await self.build_page_payload()
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


class SuggestOptionsView(discord.ui.View):
    def __init__(self, username: str, options: List[Dict[str, Any]], model_label: str) -> None:
        super().__init__(timeout=300)
        self.username = username
        self.options = options
        self.model_label = model_label
        self.idx = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.idx <= 0
        self.next_btn.disabled = self.idx >= len(self.options) - 1

    async def build_option_payload(self) -> tuple[discord.Embed, discord.File]:
        opt = self.options[self.idx]
        res = opt["analysis"]
        is_good = res["color"] == "green"
        title_emoji = "🟢" if is_good else "🔴"
        color_hex = 0x22C55E if is_good else 0xF43F5E

        give_str = ", ".join(f"**{x['name']}** (`{x['value']:,}`)" for x in res["give_items"])
        recv_str = ", ".join(f"**{x['name']}** (`{x['value']:,}`)" for x in res["receive_items"])

        factor_lines = []
        for f in res.get("factors", []):
            em = "🟢" if f.get("good") else "🔴"
            delta = int(f.get("delta", 0))
            d_str = f" (`{delta:+,}`)" if delta != 0 else ""
            factor_lines.append(f"{em} {f['text']}{d_str}")

        desc = [
            f"🎯 **Counterparty**: **{opt['poster_name']}** ({opt['source']}) — [Send Trade]({opt['send_trade_url']}) | [Rolimons Profile]({opt['rolimons_url']})",
            f"📊 **Trade Type**: `{opt['trade_kind']}`  •  **Net Retrade Room**: `{res['retrade_room']:+,}`  •  **Raw Val Diff**: `{res['op_value']:+,}`",
            "",
            f"📤 **Your Offer (`{res['give_value']:,}` Val)**: {give_str}",
            f"📥 **Their Items (`{res['receive_value']:,}` Val)**: {recv_str}",
            "",
            "**Why This Trade Is Recommended:**",
            "\n\n".join(factor_lines) if factor_lines else "🟢 Strong value & demand edge.",
        ]

        embed = discord.Embed(
            title=f"{title_emoji} Suggestion #{self.idx + 1}/{len(self.options)} for {self.username}: {res['verdict']}",
            description="\n".join(desc)[:4000],
            color=color_hex,
        )
        _apply_footer(embed, f"{self.model_label} • Option {self.idx + 1}/{len(self.options)}")

        png_bytes = await _ensure_thumbs_and_render(res["give_items"], res["receive_items"])
        fname = f"suggest_{self.idx + 1}.png"
        file = discord.File(io.BytesIO(png_bytes), filename=fname)
        embed.set_image(url=f"attachment://{fname}")
        return embed, file

    @discord.ui.button(label="◀ Previous Option", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.idx > 0:
            self.idx -= 1
        self._update_buttons()
        await interaction.response.defer()
        embed, file = await self.build_option_payload()
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)

    @discord.ui.button(label="Next Option ▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.idx < len(self.options) - 1:
            self.idx += 1
        self._update_buttons()
        await interaction.response.defer()
        embed, file = await self.build_option_payload()
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


@client.tree.command(name="whitelist", description="Owner only: Add a user to the Carbon whitelist")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="Discord user to whitelist")
async def cmd_whitelist(interaction: discord.Interaction, user: discord.User) -> None:
    if not _check_owner(interaction):
        await interaction.response.send_message(
            "Access denied: Only the Carbon owner (`1388335689229275220`) can manage the whitelist.",
            ephemeral=True,
        )
        return
    db.add_whitelist(user.id, username=str(user), added_by=interaction.user.id)
    embed = discord.Embed(
        title="🟢 Carbon Whitelist Updated",
        description=f"Whitelisted **{user}** (`{user.id}`). They can now use all Carbon commands.",
        color=0x22C55E,
    )
    _apply_footer(embed)
    await interaction.response.send_message(embed=embed, ephemeral=False)


@client.tree.command(name="blacklist", description="Owner only: Remove a user from the Carbon whitelist")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="Discord user to remove from whitelist")
async def cmd_blacklist(interaction: discord.Interaction, user: discord.User) -> None:
    if not _check_owner(interaction):
        await interaction.response.send_message(
            "Access denied: Only the Carbon owner (`1388335689229275220`) can manage the whitelist.",
            ephemeral=True,
        )
        return
    if int(user.id) == OWNER_DISCORD_ID:
        await interaction.response.send_message(
            "Cannot blacklist the primary Carbon owner.",
            ephemeral=True,
        )
        return
    removed = db.remove_whitelist(user.id)
    msg = (
        f"Removed **{user}** (`{user.id}`) from the Carbon whitelist."
        if removed
        else f"**{user}** (`{user.id}`) was not on the whitelist."
    )
    embed = discord.Embed(title="🔴 Carbon Blacklist Updated", description=msg, color=0xF43F5E)
    _apply_footer(embed)
    await interaction.response.send_message(embed=embed, ephemeral=False)


@client.tree.command(name="users", description="Owner only: View all whitelisted Carbon users")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def cmd_users(interaction: discord.Interaction) -> None:
    if not _check_owner(interaction):
        await interaction.response.send_message(
            "Access denied: Only the Carbon owner (`1388335689229275220`) can view the whitelist.",
            ephemeral=True,
        )
        return
    users = db.get_whitelisted_users()
    lines = []
    for idx, u in enumerate(users, start=1):
        role = "OWNER" if u.get("is_owner") else "USER"
        uname = u.get("username") or f"ID {u['discord_user_id']}"
        lines.append(f"`{idx}.` <@{u['discord_user_id']}> (`{u['discord_user_id']}`) — **{uname}** [{role}]")
    embed = discord.Embed(
        title=f"🟢 Carbon Whitelisted Users ({len(users)})",
        description="\n".join(lines[:40]) if lines else "No users whitelisted.",
        color=0x0EA5E9,
    )
    _apply_footer(embed)
    await interaction.response.send_message(embed=embed, ephemeral=False)


@client.tree.command(name="tradecheck", description="Analyze a Roblox limited trade with Carbon Star / Max / Hume / Legacy")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    give="Comma-separated item names, acronyms, or IDs you give (e.g. IUIS, President Doge)",
    receive="Comma-separated item names, acronyms, or IDs you receive (e.g. Griffin Warrior, Dupa)",
    give_robux="Robux you add (30% tax applied automatically)",
    receive_robux="Robux you receive (30% tax applied automatically)",
    model="Select Carbon valuation model (default: Carbon Star 1.0)",
)
@app_commands.choices(model=MODEL_CHOICES)
async def cmd_tradecheck(
    interaction: discord.Interaction,
    give: str,
    receive: str,
    give_robux: int = 0,
    receive_robux: int = 0,
    model: str = "star",
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    give_raw = [x.strip() for x in give.split(",") if x.strip()]
    recv_raw = [x.strip() for x in receive.split(",") if x.strip()]
    give_ids = await asyncio.to_thread(_resolve_item_inputs, give_raw)
    recv_ids = await asyncio.to_thread(_resolve_item_inputs, recv_raw)

    if (not give_ids and give_robux <= 0) or (not recv_ids and receive_robux <= 0):
        await interaction.followup.send(
            "Could not resolve items on both sides. Check item names/acronyms or IDs.",
            ephemeral=True,
        )
        return

    res = await asyncio.to_thread(
        trade_engine.analyze_trade,
        give_ids,
        recv_ids,
        ITEMS_CATALOG,
        model,
        RECENT_RAW_ADS,
        give_robux,
        receive_robux,
    )
    png_bytes = await _ensure_thumbs_and_render(res["give_items"], res["receive_items"])

    is_good = res["color"] == "green"
    title_emoji = "🟢" if is_good else "🔴"
    color_hex = 0x22C55E if is_good else (0xF59E0B if res["color"] == "yellow" else 0xF43F5E)

    net_emoji = "🟢" if res["retrade_room"] >= 0 else "🔴"

    header_block = (
        f"**OP (Value / RAP)**: `{res['op_value']:+,}` / `{res['op_rap']:+,}`\n"
        f"**Model**: `{res['engine']}` (*{res['engine_desc']}*)\n"
        f"{net_emoji} **Likely Retrade Room**: **`{res['retrade_room']:+,}`**"
    )

    factor_blocks = []
    for f in res.get("factors", []):
        f_emoji = "🟢" if f.get("good") else "🔴"
        delta = int(f.get("delta", 0))
        d_tag = f" — **`{delta:+,}`**" if delta != 0 else ""
        factor_blocks.append(f"{f_emoji} {f['text']}{d_tag}")

    body_text = header_block + "\n\n" + ("\n\n".join(factor_blocks) if factor_blocks else "")

    embed = discord.Embed(
        title=f"{title_emoji} {res['verdict']} ({res['retrade_room']:+,} Edge)",
        description=body_text[:4000],
        color=color_hex,
    )
    _apply_footer(embed, f"{res['engine']} ({res['engine_desc']})")

    file = discord.File(io.BytesIO(png_bytes), filename="carbon_trade.png")
    embed.set_image(url="attachment://carbon_trade.png")
    await interaction.followup.send(embed=embed, file=file)


@client.tree.command(name="proofget", description="Get the last 5 verified trade proofs for a specific item with Next/Previous buttons")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.autocomplete(item=_item_autocomplete)
@app_commands.describe(
    item="Item name, acronym, or ID (e.g. Dupa, IV, FF, CS)",
    trade_type="Optional filter: All proofs, Upgrade proofs only, or Downgrade proofs only",
)
@app_commands.choices(
    trade_type=[
        app_commands.Choice(name="All Proofs (Any)", value="any"),
        app_commands.Choice(name="Downgrade Proofs Only", value="downgrade"),
        app_commands.Choice(name="Upgrade Proofs Only", value="upgrade"),
    ]
)
async def cmd_proofget(
    interaction: discord.Interaction,
    item: str,
    trade_type: str = "any",
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    found = find_item_by_query(item, ITEMS_CATALOG)
    if not found:
        await interaction.followup.send(f"Item `{item}` not found.", ephemeral=True)
        return

    res = await asyncio.to_thread(
        get_item_proofs_formatted,
        db,
        found["item_id"],
        ITEMS_CATALOG,
        5,
        trade_type,
        sales_client,
    )
    it = res["item"]
    proofs_list = res.get("proofs") or []

    if not proofs_list:
        embed = discord.Embed(
            title=f"🔴 No {trade_type.upper() if trade_type != 'any' else ''} Trade Proofs Yet: {it['name']}",
            description=(
                f"No recorded `{trade_type}` trade proofs in database yet for **{it['name']}** (`{it['value']:,}` Val).\n\n"
                f"💡 Use `/proofs` to browse all recent recorded trades across all items, or `/scanproof` to log a Rolimons `#value-requests` proof."
            ),
            color=0xF43F5E,
        )
        _apply_footer(embed)
        await interaction.followup.send(embed=embed)
        return

    header = f"📜 Trade Proofs: {it['name']} [{trade_type.upper()}]"
    view = PaginatedProofsView(proofs_list, header)
    embed, file = await view.build_page_payload()
    await interaction.followup.send(embed=embed, file=file, view=view)


@client.tree.command(name="proofs", description="Browse the most recent recorded trade proofs across all items")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    view_mode="Show the 5 most recent proofs or browse all recorded proofs",
)
@app_commands.choices(
    view_mode=[
        app_commands.Choice(name="Last 5 Recent Proofs", value="recent5"),
        app_commands.Choice(name="All Recorded Proofs (Interactive Browser)", value="all"),
    ]
)
async def cmd_proofs(
    interaction: discord.Interaction,
    view_mode: str = "recent5",
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    fetch_limit = 5 if view_mode == "recent5" else 150
    rows = await asyncio.to_thread(db.get_recent_trades, fetch_limit, 0)
    total_db_count = len(await asyncio.to_thread(db.get_recent_trades, 2000, 0))

    if not rows:
        embed = discord.Embed(
            title="🔴 No Trade Proofs Recorded Yet",
            description="The database currently has 0 trade proofs.",
            color=0xF43F5E,
        )
        _apply_footer(embed)
        await interaction.followup.send(embed=embed)
        return

    header = (
        f"🕒 5 Most Recent Trade Proofs (Total Verified: {total_db_count})"
        if view_mode == "recent5"
        else f"📚 All Verified Trade Proofs ({len(rows)} of {total_db_count})"
    )
    view = PaginatedProofsView(rows, header)
    embed, file = await view.build_page_payload()
    await interaction.followup.send(embed=embed, file=file, view=view)


@client.tree.command(name="op", description="See exactly how much to GIVE to acquire an item and how much to GET when trading it away")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.autocomplete(item=_item_autocomplete)
@app_commands.describe(
    item="Item name, acronym, or ID",
    model="Select Carbon valuation model (default: Carbon Star 1.0)",
)
@app_commands.choices(model=MODEL_CHOICES)
async def cmd_op(
    interaction: discord.Interaction,
    item: str,
    model: str = "star",
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    found = find_item_by_query(item, ITEMS_CATALOG)
    if not found:
        await interaction.followup.send(f"Item `{item}` not found.", ephemeral=True)
        return

    d = await asyncio.to_thread(
        compute_op_lookup,
        db,
        found["item_id"],
        ITEMS_CATALOG,
        RECENT_RAW_ADS,
        sales_client,
        30,
        model,
    )
    it = d["item"]
    if not it.get("thumbnail_url"):
        t_map = await asyncio.to_thread(roblox.fetch_thumbnails, [it["item_id"]])
        if it["item_id"] in t_map:
            it["thumbnail_url"] = t_map[it["item_id"]]
            db.update_thumbnails(t_map)

    base_val = int(it.get("value", 0))
    rap = int(it.get("rap", 0))
    max_give_total = d.get("max_total_to_give")
    max_give_op = d.get("max_op_to_give")
    min_get_total = d.get("min_total_to_expect")
    min_get_op = d.get("min_op_to_expect")
    avg_op = d.get("avg_op")
    op_range = d.get("op_range") or [0, 0]
    hoard = d.get("hoard_stats") or {}

    is_pos = (avg_op or 0) >= 0
    title_emoji = "🟢" if is_pos else "🔴"
    color_hex = 0x22C55E if is_pos else 0xF43F5E

    give_box = (
        f"Give up to **`{max_give_total:,}`** total value (**`{max_give_op:+,}`** OP vs `{base_val:,}`)"
        if max_give_total is not None
        else "Insufficient proof data"
    )
    get_box = (
        f"Expect at least **`{min_get_total:,}`** total value (**`{min_get_op:+,}`** OP vs `{base_val:,}`)"
        if min_get_total is not None
        else "Insufficient proof data"
    )
    range_box = (
        f"OP Range: **`{op_range[0]:+,}`** to **`{op_range[1]:+,}`**  •  Median OP: **`{avg_op:+,}`**"
        if avg_op is not None
        else "N/A"
    )

    desc_lines = [
        f"💎 **Rolimons Value**: `{base_val:,}`  •  **RAP**: `{rap:,}`  •  **Demand**: `{it.get('demand_label', 'Unassigned')}`",
        f"🧠 **Model**: `{d.get('model_name')}` (*{d.get('model_desc')}*) — {d.get('explanation', '')}",
        "",
        f"📥 **HOW MUCH YOU SHOULD GIVE (When Trading FOR {it['name']})**\n> {give_box}",
        "",
        f"📤 **HOW MUCH YOU SHOULD GET (When Trading {it['name']} AWAY)**\n> {get_box}",
        "",
        f"🎯 **OVERPAY SUMMARY**\n> {range_box}",
        "",
        f"📦 **Hoards**: `{hoard.get('hoarder_count', 0)}` hoarders hold `{hoard.get('hoarded_copies', 0)}` copies (`{hoard.get('hoard_percent', 0.0)}%` of tracked supply)  •  **Proofs**: `{d.get('proof_count', 0)}`",
    ]

    acr_tag = f" [{it['acronym']}]" if it.get("acronym") else ""
    embed = discord.Embed(
        title=f"{title_emoji} OP Target Guide: {it['name']}{acr_tag}",
        description="\n".join(desc_lines),
        color=color_hex,
    )
    if it.get("thumbnail_url"):
        embed.set_thumbnail(url=it["thumbnail_url"])
    _apply_footer(embed, d.get("confidence_detail", "Carbon OP Engine"))
    await interaction.followup.send(embed=embed)


@client.tree.command(name="suggest", description="Generate top 3 trade suggestions for a Roblox user with visual cards")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    username="Roblox username or User ID to suggest trades for",
    exclude_items="Optional comma-separated items in your inventory to exclude (e.g. Classic Fedora, SSHF)",
    include_items="Optional comma-separated items in your inventory that MUST be included",
    trade_type="Optional filter: Upgrade, Downgrade, or Any",
    target="Optional target Roblox username/ID to pull trade offers/inventory from",
    model="Select Carbon valuation model (default: Carbon Star 1.0)",
)
@app_commands.choices(
    trade_type=[
        app_commands.Choice(name="Any (Best Profit Edge)", value="any"),
        app_commands.Choice(name="Upgrade Only (Consolidate Items)", value="upgrade"),
        app_commands.Choice(name="Downgrade Only (Split for Overpay)", value="downgrade"),
    ],
    model=MODEL_CHOICES,
)
async def cmd_suggest(
    interaction: discord.Interaction,
    username: str,
    exclude_items: str = "",
    include_items: str = "",
    trade_type: str = "any",
    target: str = "",
    model: str = "star",
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    await asyncio.to_thread(poll_rolimons_ads_once)
    model_name, model_desc = resolve_model_label(model)

    res = await asyncio.to_thread(
        generate_trade_suggestions,
        username,
        roblox,
        db,
        trade_engine,
        ITEMS_CATALOG,
        RECENT_RAW_ADS,
        exclude_items,
        include_items,
        trade_type,
        target,
        model,
        3,
    )

    if res.get("error"):
        embed = discord.Embed(
            title="🔴 Could Not Generate Trade Suggestions",
            description=res["error"],
            color=0xF43F5E,
        )
        _apply_footer(embed, f"{model_name} ({model_desc})")
        await interaction.followup.send(embed=embed)
        return

    options = res.get("options") or []
    if not options:
        embed = discord.Embed(
            title=f"🔴 No Profitable Trades Found for {res['username']}",
            description=(
                f"Scanned `{res['inventory_count']}` tradable items in **{res['username']}**'s inventory against live market offers using **{model_name}**, "
                "but none passed the profit filter with your current constraints. Try relaxing `include_items` or `trade_type`."
            ),
            color=0xF43F5E,
        )
        _apply_footer(embed, f"{model_name} ({model_desc})")
        await interaction.followup.send(embed=embed)
        return

    view = SuggestOptionsView(res["username"], options, f"{model_name} ({model_desc})")
    embed, file = await view.build_option_payload()
    await interaction.followup.send(embed=embed, file=file, view=view)


@client.tree.command(name="scanproof", description="Scan a Rolimons #value-requests text & screenshot (OCR + Text) into the proof database")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    proof_text="Paste the #value-requests text (optional if screenshot is attached)",
    screenshot="Attach the trade screenshot from #value-requests for automatic OCR scanning",
)
async def cmd_scanproof(
    interaction: discord.Interaction,
    proof_text: str = "",
    screenshot: Optional[discord.Attachment] = None,
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    img_bytes: Optional[bytes] = None
    if screenshot is not None:
        try:
            img_bytes = await screenshot.read()
        except Exception:
            img_bytes = None

    parsed = await asyncio.to_thread(parse_value_request_text, proof_text, ITEMS_CATALOG, img_bytes)
    if not parsed:
        embed = discord.Embed(
            title="🔴 Could Not Parse Complete 2-Sided Proof",
            description="Both sides of the trade must have real recognizable limiteds (no 1-sided trades). Provide the `#value-requests` text and/or clear trade screenshot.",
            color=0xF43F5E,
        )
        _apply_footer(embed)
        await interaction.followup.send(embed=embed)
        return

    tid = await asyncio.to_thread(save_parsed_value_request_proof, db, parsed)
    sa = ", ".join(f"**{x['name']}** (`{x['value']:,}`)" for x in parsed["items_a"])
    sb = ", ".join(f"**{x['name']}** (`{x['value']:,}`)" for x in parsed["items_b"])

    embed = discord.Embed(
        title=f"🟢 Value Request Proof Scanned & Saved (Trade #{tid})",
        description=(
            f"👤 **Sender**: `{parsed['sender']}`  ⇄  **Receiver**: `{parsed['receiver']}`\n"
            f"📊 **Values**: `{parsed['value_a']:,}` vs `{parsed['value_b']:,}` (`{parsed['op_value']:,}` OP • `{parsed['trade_type']}`)\n\n"
            f"📤 **Side A**: {sa}\n"
            f"📥 **Side B**: {sb}"
        ),
        color=0x22C55E,
    )
    _apply_footer(embed, "Rolimons #value-requests OCR + Text Scanner")

    png_bytes = await _ensure_thumbs_and_render(parsed["items_a"], parsed["items_b"])
    file = discord.File(io.BytesIO(png_bytes), filename="scanned_proof.png")
    embed.set_image(url="attachment://scanned_proof.png")
    await interaction.followup.send(embed=embed, file=file)


@client.tree.command(name="sniper", description="Filter live Rolimons trade ads by deal %, User Sends, or User Wants")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    sends="Filter by item the poster is sending (e.g. BPTH)",
    wants="Filter by item the poster is requesting (e.g. BPTH)",
    min_deal="Minimum deal percentage (default 5%)",
)
async def cmd_sniper(
    interaction: discord.Interaction,
    sends: str = "",
    wants: str = "",
    min_deal: float = 5.0,
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message(
            "Access denied: You are not whitelisted on Carbon.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=False)
    await asyncio.to_thread(poll_rolimons_ads_once)
    deals = AdSniper.filter_deals(
        SNIPED_DEALS,
        bucket="ALL",
        min_deal=min_deal,
        sends_query=sends,
        wants_query=wants,
    )[:8]

    if not deals:
        await interaction.followup.send(
            "No matching trade ads found right now. Try lowering `min_deal` or changing filters.",
        )
        return

    embed = discord.Embed(
        title=f"🟢 Carbon Ad Sniper ({len(deals)} Deals Shown)",
        color=0x22C55E,
    )
    for d in deals:
        off_str = ", ".join(f"{x['name']} ({x['value']:,})" for x in d["offer_items"])
        req_str = ", ".join(f"{x['name']} ({x['value']:,})" for x in d["request_items"])
        embed.add_field(
            name=f"🟢 +{d['deal_percent']}% (+{d['value_overpay']:,} Val) — {d['username']}",
            value=(
                f"**Sends ({d['offer_value']:,})**: {off_str}\n"
                f"**Wants ({d['request_value']:,})**: {req_str}\n"
                f"[Send Trade]({d['links']['send_trade']}) | [Rolimons]({d['links']['rolimons_profile']})"
            ),
            inline=False,
        )
    _apply_footer(embed)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="user", description="Inspect a Roblox trader profile, limiteds valuation, rank, and history graph")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="Roblox username or numeric User ID")
async def cmd_user(interaction: discord.Interaction, user: str) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message("Access denied: You are not whitelisted on Carbon.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    resolved = await asyncio.to_thread(roblox.resolve_user, user)
    if not resolved:
        await interaction.followup.send(f"Could not find Roblox user `{user}`.", ephemeral=True)
        return

    uid = resolved["user_id"]
    uname = resolved["username"]

    created_date = "Unknown"
    try:
        req = urllib.request.Request(f"https://users.roblox.com/v1/users/{uid}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            udata = json.loads(r.read().decode())
            created_raw = udata.get("created", "")
            if created_raw:
                created_date = created_raw.split("T")[0]
    except Exception:
        pass

    avatar_url = ""
    try:
        req = urllib.request.Request(f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={uid}&size=150x150&format=Png&isCircular=false", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            tdata = json.loads(r.read().decode())
            avatar_url = tdata.get("data", [{}])[0].get("imageUrl", "")
    except Exception:
        pass

    rap_map: Dict[int, int] = {}
    uaids = await asyncio.to_thread(roblox.fetch_collectibles, uid, rap_map) or {}
    owned_items = [ITEMS_CATALOG[aid] for aid in uaids.values() if aid in ITEMS_CATALOG]
    owned_items.sort(key=lambda x: (int(x.get("value", 0)), int(x.get("rap", 0))), reverse=True)

    tot_val = sum(int(x.get("value", 0)) for x in owned_items)
    tot_rap = sum(int(x.get("rap", 0)) for x in owned_items)

    await asyncio.to_thread(poll_rolimons_ads_once)
    ads_count = sum(1 for a in RECENT_RAW_ADS if int(a.get("user_id", a.get("player_id", 0)) if isinstance(a, dict) else a[2]) == uid)

    top5_lines = []
    for it in owned_items[:5]:
        v = int(it.get("value", 0))
        top5_lines.append(f"• **{it['name']}** - `{format_k(v)} R$`")

    top5_str = "\n".join(top5_lines) if top5_lines else "*No public limiteds found.*"

    desc = (
        f"**Value**\n`{format_k(tot_val)} R$`\n\n"
        f"**RAP**\n`{format_k(tot_rap)} R$`\n\n"
        f"**Limiteds**\n`{len(owned_items)}`\n\n"
        f"**Trade Ads**\n`{ads_count}`\n\n"
        f"**ID**\n`{uid}`\n\n"
        f"**Joined**\n`{created_date}`\n\n"
        f"**Top 5 Items**\n{top5_str}"
    )

    embed = discord.Embed(
        title=f"{uname}'s Profile",
        description=desc,
        color=0x3B82F6,
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    _apply_footer(embed)

    graph_png = await asyncio.to_thread(render_user_profile_graph, uname, tot_val, tot_rap)
    fname = f"profile_{uid}.png"
    file = discord.File(io.BytesIO(graph_png), filename=fname)
    embed.set_image(url=f"attachment://{fname}")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Profile", url=f"https://www.roblox.com/users/{uid}/profile", style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Rolimons", url=f"https://www.rolimons.com/player/{uid}", style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Trade Ads", url=f"https://www.rolimons.com/tradeads?player={uid}", style=discord.ButtonStyle.link))
    view.add_item(discord.ui.Button(label="Send Trade", url=f"https://www.roblox.com/trades/send?userId={uid}", style=discord.ButtonStyle.link))

    await interaction.followup.send(embed=embed, file=file, view=view)


@client.tree.command(name="predict", description="Predict whether an item is close to raising, lowering, or stable with Carbon Models")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    item="Item name, acronym, or ID",
    target="Optional expectation or target: close to raise, close to decrease, or stable",
    model="Select Carbon valuation model",
)
@app_commands.choices(
    target=[
        app_commands.Choice(name="Close to Raise", value="raise"),
        app_commands.Choice(name="Close to Decrease", value="decrease"),
        app_commands.Choice(name="Stable", value="stable"),
    ],
    model=MODEL_CHOICES,
)
async def cmd_predict(
    interaction: discord.Interaction,
    item: str,
    target: Optional[str] = None,
    model: str = "star",
) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message("Access denied: You are not whitelisted on Carbon.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    found = find_item_by_query(item, ITEMS_CATALOG)
    if not found:
        await interaction.followup.send(f"Item `{item}` not found.", ephemeral=True)
        return

    item_id = found["item_id"]
    d = await asyncio.to_thread(
        compute_op_lookup,
        db,
        item_id,
        ITEMS_CATALOG,
        RECENT_RAW_ADS,
        sales_client,
        30,
        model,
    )
    it = d["item"]
    base_val = int(it.get("value", 0))
    rap = int(it.get("rap", 0))
    avg_op = int(d.get("avg_op", 0))
    demand = int(it.get("demand", -1))
    trend = int(it.get("trend", -1))

    sales_summary = sales_client.fetch_item_sales_summary(db, item_id, it) if sales_client else {}
    avg_price_7d = sales_summary.get("avg_price_7d", 0)
    lowest_resale = sales_summary.get("lowest_resale", 0)
    vol = sales_summary.get("daily_volume", 0.0)

    score = 0
    factors = []

    if demand >= 3:
        score += 2
        factors.append("🟢 **High/Amazing Demand**: Accelerates trade circulation and buyer competition.")
    elif demand in (0, 1):
        score -= 2
        factors.append("🔴 **Low/Terrible Demand**: Slower buyer interest increases drop probability.")

    if avg_op >= 4000:
        score += 3
        factors.append(f"🟢 **Strong Proof Overpay (`+{avg_op:,}` OP)**: Traders consistently paying above Rolimons value.")
    elif avg_op <= -4000:
        score -= 3
        factors.append(f"🔴 **Negative Overpay / Underpay (`{avg_op:,}` OP)**: Item being discounted in recent proofs.")

    if lowest_resale and base_val > 0:
        ratio = lowest_resale / float(base_val)
        if ratio > 1.15:
            score += 2
            factors.append(f"🟢 **Elevated Resale Floor**: Lowest copy listed at `{lowest_resale:,}` R$ (above base value).")
        elif ratio < 0.82:
            score -= 2
            factors.append(f"🔴 **Depressed Resale Floor**: Lowest copy listed at `{lowest_resale:,}` R$ (below base value).")

    if trend == 3:
        score += 2
        factors.append("🟢 **Rolimons Trend: Raising**.")
    elif trend == 0:
        score -= 2
        factors.append("🔴 **Rolimons Trend: Lowering**.")

    if score >= 3:
        prediction = "CLOSE TO RAISE 📈"
        p_color = 0x22C55E
        confidence = "High" if score >= 5 else "Moderate"
    elif score <= -3:
        prediction = "CLOSE TO DECREASE 📉"
        p_color = 0xF43F5E
        confidence = "High" if score <= -5 else "Moderate"
    else:
        prediction = "STABLE ⚖️"
        p_color = 0x0EA5E9
        confidence = "Solid"

    align_text = ""
    if target:
        t_key = "raise" if "raise" in prediction.lower() else ("decrease" if "decrease" in prediction.lower() else "stable")
        match_icon = "✅" if target == t_key else "❌"
        align_text = f"\n**Target Check ({target.upper()})**: {match_icon} Model verdict aligns: **{target == t_key}**\n"

    desc = (
        f"💎 **Item**: **{it['name']}** (`{base_val:,}` Val / `{rap:,}` RAP)\n"
        f"🧠 **Model**: `{d.get('model_name')}`\n"
        f"🔮 **Prediction**: **`{prediction}`** (Confidence: **{confidence}**)\n"
        f"{align_text}\n"
        f"📊 **Market Indicators & Driving Factors**:\n"
        + "\n".join(f"- {f}" for f in factors)
        + f"\n\n**Key Metrics**:\n"
        f"- Recent Median OP: **`{avg_op:+,}`**\n"
        f"- Daily Sales Volume: **`{vol:.1f}/day`**\n"
        f"- 7-Day Average Price: **`{avg_price_7d:,}` R$**\n"
        f"- Lowest Resale: **`{lowest_resale:,}` R$**"
    )

    embed = discord.Embed(
        title=f"🔮 Carbon Market Predictor: {it['name']}",
        description=desc,
        color=p_color,
    )
    if it.get("thumbnail_url"):
        embed.set_thumbnail(url=it["thumbnail_url"])
    _apply_footer(embed)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="matchingowners", description="Find traders owning exact item criteria (e.g. 'fafa 2, legit 3+, sta 2-')")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(query="Comma-separated rules (e.g. 'fafa 2, legit 3+, sta 2-')")
async def cmd_matchingowners(interaction: discord.Interaction, query: str) -> None:
    if not _check_whitelisted(interaction):
        await interaction.response.send_message("Access denied: You are not whitelisted on Carbon.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    parts = [p.strip() for p in query.split(",") if p.strip()]
    rules = []
    for p in parts:
        m = re.match(r"^(.*?)\s*(\d+)\s*([+-])?$", p)
        if not m:
            continue
        name_part = m.group(1).strip()
        cnt = int(m.group(2))
        op = m.group(3) or ""
        it = find_item_by_query(name_part, ITEMS_CATALOG)
        if it:
            rules.append({"item": it, "count": cnt, "op": op})

    if not rules:
        await interaction.followup.send(
            "Could not parse query. Example format: `/matchingowners fafa 2, legit 3+, sta 2-`",
            ephemeral=True,
        )
        return

    def find_owners():
        conn = db._connect()
        user_counts = defaultdict(lambda: defaultdict(int))
        for r in rules:
            iid = r["item"]["item_id"]
            rows = conn.execute("SELECT user_id FROM owners WHERE item_id = ?", (iid,)).fetchall()
            for row in rows:
                user_counts[row["user_id"]][iid] += 1

        matched_users = []
        for uid, counts in user_counts.items():
            if uid <= 1000:
                continue
            fits = True
            for r in rules:
                iid = r["item"]["item_id"]
                c = counts.get(iid, 0)
                if r["op"] == "+":
                    if c < r["count"]:
                        fits = False
                        break
                elif r["op"] == "-":
                    if c > r["count"]:
                        fits = False
                        break
                else:
                    if c != r["count"]:
                        fits = False
                        break
            if fits:
                matched_users.append(uid)
        return matched_users

    matched_uids = await asyncio.to_thread(find_owners)

    if not matched_uids:
        await interaction.followup.send(
            f"No matching traders found in database matching: `{query}`.",
            ephemeral=True,
        )
        return

    await asyncio.to_thread(poll_rolimons_ads_once)
    resolved_names = await asyncio.to_thread(db.resolve_missing_trader_names, matched_uids[:5])

    rule_str = "\n".join(f"- **{r['item']['name']}** (Owns {r['count']}{r['op']})" for r in rules)
    embed = discord.Embed(
        title=f"🎯 Matching Owners Found ({len(matched_uids)} Traders)",
        description=f"**Criteria**:\n{rule_str}\n\n**Showing Top Matching Traders**:",
        color=0x22C55E,
    )

    for uid in matched_uids[:5]:
        uname = resolved_names.get(uid) or f"User_{uid}"
        user_ads = [a for a in RECENT_RAW_ADS if int(a.get("user_id", a.get("player_id", 0)) if isinstance(a, dict) else a[2]) == uid][:3]
        ad_lines = []
        for ad in user_ads:
            s_items = ad.get("offer_items", []) if isinstance(ad, dict) else ad[4].get("items", [])
            sends = [ITEMS_CATALOG[x]["name"] for x in s_items if x in ITEMS_CATALOG]
            w_items = ad.get("request_items", []) if isinstance(ad, dict) else ad[5].get("items", [])
            wants = [ITEMS_CATALOG[x]["name"] for x in w_items if x in ITEMS_CATALOG]
            ad_lines.append(f"• Sends `{', '.join(sends) or 'Any'}` ⇄ Wants `{', '.join(wants) or 'Any'}`")

        ad_text = "\n".join(ad_lines) if ad_lines else "*No active trade ads posted recently.*"
        embed.add_field(
            name=f"👤 {uname} (`{uid}`)",
            value=(
                f"[Rolimons Profile](https://www.rolimons.com/player/{uid}) | [Send Trade](https://www.roblox.com/trades/send?userId={uid})\n"
                f"**Last Trade Ads**:\n{ad_text}"
            ),
            inline=False,
        )

    _apply_footer(embed)
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    token = _load_bot_token()
    if not token:
        print("Set DISCORD_BOT_TOKEN in environment or [discord] bot_token in config.toml")
    elif "--sync-only" in sys.argv:
        async def _sync_once():
            await client.login(token)
            synced = await client.tree.sync()
            print(f"[Carbon Discord App] Synced {len(synced)} commands: {[c.name for c in synced]}")
            await client.close()
        asyncio.run(_sync_once())
    else:
        client.run(token)

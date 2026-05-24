import os
import asyncio
import base64
import io
import discord
from discord.ext import tasks
from supabase import create_client

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_TOKEN")
SUPABASE_URL      = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
GUILD_ID          = int(os.environ.get("GUILD_ID", "1507853042135994528"))
OWNER_ID          = int(os.environ.get("OWNER_ID", "1350282344988409940"))

# How often the bot checks for new purchases (seconds)
POLL_INTERVAL = 15

# ── Supabase ──────────────────────────────────────────────────────────────────
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# ── Discord client ────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_pending_purchases():
    """Fetch all purchases that haven't been delivered yet."""
    res = (
        supabase.table("purchases")
        .select("*")
        .eq("delivery_status", "pending")
        .execute()
    )
    return res.data or []

def get_asset(asset_id):
    """Fetch a single asset by ID."""
    res = (
        supabase.table("assets")
        .select("*")
        .eq("id", asset_id)
        .single()
        .execute()
    )
    return res.data

def mark_delivered(purchase_id):
    """Mark a purchase as delivered."""
    supabase.table("purchases").update(
        {"delivery_status": "delivered"}
    ).eq("id", purchase_id).execute()

def mark_failed(purchase_id, reason):
    """Mark a purchase as failed with a reason."""
    supabase.table("purchases").update(
        {"delivery_status": f"failed: {reason}"}
    ).eq("id", purchase_id).execute()

def decode_file(base64_data, file_name):
    """Convert base64 file data to a Discord-uploadable file object."""
    # Strip the data URL prefix if present (data:application/...;base64,)
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]
    raw = base64.b64decode(base64_data)
    return discord.File(io.BytesIO(raw), filename=file_name)

async def find_member_by_discord_id(discord_id: str):
    """Look up a guild member by their Discord user ID."""
    guild = client.get_guild(GUILD_ID)
    if not guild:
        guild = await client.fetch_guild(GUILD_ID)
    try:
        member = await guild.fetch_member(int(discord_id))
        return member
    except discord.NotFound:
        return None

async def notify_owner(message: str):
    """DM the owner with a status update."""
    try:
        owner = await client.fetch_user(OWNER_ID)
        await owner.send(message)
    except Exception as e:
        print(f"Could not DM owner: {e}")

# ── Delivery loop ─────────────────────────────────────────────────────────────
@tasks.loop(seconds=POLL_INTERVAL)
async def check_purchases():
    pending = get_pending_purchases()
    if not pending:
        return

    print(f"[bot] Found {len(pending)} pending purchase(s)")

    for purchase in pending:
        purchase_id      = purchase["id"]
        discord_id       = purchase.get("discord_id")
        discord_username = purchase.get("discord_username", "unknown")
        asset_id         = purchase.get("asset_id")
        asset_name       = purchase.get("asset_name", "Unknown Asset")
        file_name        = purchase.get("asset_file_name", "asset.rbxl")

        print(f"[bot] Processing purchase {purchase_id} for {discord_username} ({discord_id})")

        # ── 1. Validate discord ID ────────────────────────────────────────────
        if not discord_id:
            print(f"[bot] No discord_id for purchase {purchase_id}, skipping")
            mark_failed(purchase_id, "no discord_id")
            await notify_owner(f"⚠️ Purchase `{purchase_id}` has no Discord ID — could not deliver `{asset_name}`.")
            continue

        # ── 2. Fetch asset ────────────────────────────────────────────────────
        try:
            asset = get_asset(asset_id)
        except Exception as e:
            print(f"[bot] Could not fetch asset {asset_id}: {e}")
            mark_failed(purchase_id, "asset not found")
            await notify_owner(f"⚠️ Could not find asset `{asset_id}` for purchase `{purchase_id}`.")
            continue

        if not asset or not asset.get("file"):
            print(f"[bot] Asset {asset_id} has no file data")
            mark_failed(purchase_id, "asset file missing")
            await notify_owner(f"⚠️ Asset `{asset_name}` has no RBXL file attached. Purchase `{purchase_id}` could not be delivered.")
            continue

        # ── 3. Find the buyer on Discord ──────────────────────────────────────
        member = await find_member_by_discord_id(discord_id)
        if not member:
            print(f"[bot] Could not find Discord user {discord_id}")
            mark_failed(purchase_id, "user not in server")
            await notify_owner(
                f"⚠️ Could not find `{discord_username}` (ID: `{discord_id}`) in the server.\n"
                f"They need to join the server before the bot can DM them.\n"
                f"Purchase: `{purchase_id}` — Asset: `{asset_name}`"
            )
            continue

        # ── 4. Send the file ──────────────────────────────────────────────────
        try:
            rbxl_file = decode_file(asset["file"], file_name)
            await member.send(
                f"✅ **Thanks for your purchase!**\n\n"
                f"Here's your file for **{asset_name}**.\n"
                f"Drop the `.rbxl` file into Roblox Studio to use it.\n\n"
                f"— KY Assets",
                file=rbxl_file
            )
            mark_delivered(purchase_id)
            print(f"[bot] Delivered {asset_name} to {discord_username}")
            await notify_owner(f"✅ Delivered `{asset_name}` to `{discord_username}` successfully.")
        except discord.Forbidden:
            print(f"[bot] DMs are closed for {discord_username}")
            mark_failed(purchase_id, "DMs closed")
            await notify_owner(
                f"⚠️ Could not DM `{discord_username}` — their DMs are closed.\n"
                f"Ask them to open DMs from server members and re-request delivery.\n"
                f"Purchase: `{purchase_id}`"
            )
        except Exception as e:
            print(f"[bot] Failed to send file to {discord_username}: {e}")
            mark_failed(purchase_id, str(e))
            await notify_owner(f"⚠️ Error delivering `{asset_name}` to `{discord_username}`: {e}")

# ── Bot events ────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"[bot] Logged in as {client.user} — polling every {POLL_INTERVAL}s")
    await notify_owner(f"✅ KY Assets bot is online and watching for purchases.")
    check_purchases.start()

# ── Run ───────────────────────────────────────────────────────────────────────
client.run(DISCORD_TOKEN)

#!/usr/bin/env python3
import sys
import os
import asyncio

# Change to script directory
script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)
sys.path.insert(0, script_dir)

print(f"Current directory: {os.getcwd()}")
print(f".env exists: {os.path.exists('.env')}")

import discord
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
print(f"TOKEN loaded: {TOKEN is not None}")

from src.services.order_manager import refresh_order_message
from src.commands.handlers import get_channel_by_id
from src.http_utils import ensure_http_client_ready

async def test_refresh_order_message():
    """Test if refresh_order_message works with the HTTP client fix"""

    if not TOKEN:
        print("❌ BOT_TOKEN not found in .env file")
        return

    # Create a mock bot instance
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = discord.Client(intents=intents)

    try:
        # Login with token
        await bot.login(TOKEN)
        print("✅ Bot logged in")

        # Start the connection
        await bot.connect(reconnect=False)
        print("✅ Bot connected to Discord")

        # Initialize HTTP client
        await ensure_http_client_ready(bot)

        # Warm up HTTP client
        await bot.fetch_user(bot.user.id)
        print("✅ HTTP client warmup successful")

        # Test channel fetch
        channel = await get_channel_by_id("1493058457232740382", bot)  # INVOICE_CHANNEL_ID
        if channel:
            print(f"✅ Channel fetch successful: {channel.name}")
        else:
            print("❌ Channel fetch failed")

        # Test refresh_order_message with a pending order
        order_id = "10f701a2-4bda-42bd-8477-a6684220261d"  # First pending order
        print(f"Testing refresh_order_message for order {order_id}")

        try:
            await refresh_order_message(order_id, bot)
            print("✅ refresh_order_message completed without error")
        except Exception as e:
            print(f"❌ refresh_order_message failed: {e}")

    except Exception as e:
        print(f"❌ Test failed: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(test_refresh_order_message())
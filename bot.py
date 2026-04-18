#!/usr/bin/env python3
"""
Discord Shop Bot Runner
========================
Run script for the organized Discord shop bot.
"""

import sys
import os

# Add src directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import and run the bot
from bot import bot

if __name__ == "__main__":
    print("[*] Starting Discord Shop Bot...")
    bot.run(os.getenv("BOT_TOKEN"))
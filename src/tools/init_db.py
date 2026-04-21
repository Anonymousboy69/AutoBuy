#!/usr/bin/env python3
import os
from shopbot.database import init_db

DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'shop_data.db'))

def init_database():
    print("Initializing database...")
    init_db(DB_FILE)
    print("Database initialized successfully!")

if __name__ == "__main__":
    init_database()
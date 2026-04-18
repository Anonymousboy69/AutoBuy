#!/usr/bin/env python3
from shopbot.database import init_db

def init_database():
    print("Initializing database...")
    init_db('shop.db')
    print("Database initialized successfully!")

if __name__ == "__main__":
    init_database()
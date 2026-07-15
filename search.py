import sqlite3
import os
import sys
from pathlib import Path

DB_FILE = Path(__file__).parent.resolve() / "screendata.db"

def search_screenshots(query_text):
    if not DB_FILE.exists():
        print("No database found! Please run organizer.py first to process some screenshots.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Query using SQLite's lightning-fast full-text search index (FTS5)
    cursor.execute('''
        SELECT filename, category FROM screenshot_search 
        WHERE screenshot_search MATCH ? 
        LIMIT 10
    ''', (query_text,))
    
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        print(f"\n❌ No screenshots found containing: '{query_text}'")
        return

    print(f"\n🔍 Found {len(results)} screenshot(s) matching '{query_text}':")
    print("-" * 60)
    for idx, (filename, category) in enumerate(results, 1):
        print(f"[{idx}] {filename}  -->  Folder: {category}")
    print("-" * 60)
    
    # Ask the user if they want to open any of the matches
    try:
        choice = input("Enter a number to open that screenshot (or press Enter to exit): ")
        if choice.strip().isdigit():
            index = int(choice) - 1
            if 0 <= index < len(results):
                target_file, target_cat = results[index]
                file_path = DB_FILE.parent / target_cat / target_file
                
                # Automatically open the file with Windows default photo viewer
                if file_path.exists():
                    os.startfile(str(file_path))
                    print(f"Opened: {target_file}")
                else:
                    print("Could not find the file on disk. It may have been moved or deleted.")
    except Exception as e:
        print(f"Error opening file: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # If user runs: python search.py flask
        search_screenshots(" ".join(sys.argv[1:]))
    else:
        # If user just runs: python search.py
        query = input("What text are you looking for? ")
        if query.strip():
            search_screenshots(query)
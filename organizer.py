import os
import time
import shutil
import hashlib
import sqlite3
import json
import re
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import pytesseract
from PIL import Image
import google.generativeai as genai

# ==========================================
# 1. PATHS & CONFIGURATION
# ==========================================
BASE_DIR = Path(__file__).parent.resolve()
WATCH_DIR = BASE_DIR  # Watches the folder where the script sits
DB_FILE = BASE_DIR / "screendata.db"
HISTORY_FILE = BASE_DIR / "history.json"  # Legacy file for migration

# Configure Gemini API
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"  # Replace with your actual key
genai.configure(api_key=GEMINI_API_KEY)

CATEGORIES = {
    "Coding": ["def ", "import ", "python", "javascript", "html", "css", "class ", "const ", "git ", "npm"],
    "Finance": ["portfolio", "stock", "nse", "bse", "market", "inr", "usd", "price", "dividend", "investment"],
    "Study": ["ncert", "cbse", "exercise", "chapter", "theorem", "proof", "biology", "physics", "chemistry", "notes"],
    "Anime": ["crunchyroll", "subtitles", "manga", "anime", "episode"],
    "Personal": ["whatsapp", "instagram", "chat", "message", "discord"],
    "Private": []  # Handled exclusively by the local Privacy Shield
}

# ==========================================
# 2. HARDENED DATABASE INITIALIZATION
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Core screenshot metadata table (FTS5 virtual table for lightning search)
    cursor.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS screenshots USING fts5(
            filepath,
            category,
            ocr_text,
            timestamp
        )
    ''')
    
    # New table to store SHA-256 hashes for instant duplicate prevention
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_hashes (
            sha256 TEXT PRIMARY KEY,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    
    # --- ONE-TIME LEGACY MIGRATION ---
    if HISTORY_FILE.exists():
        print("💾 Found legacy history.json! Migrating hashes to SQLite...")
        try:
            with open(HISTORY_FILE, "r") as f:
                old_hashes = json.load(f)
                if isinstance(old_hashes, list):
                    # Insert all old hashes, ignoring duplicates
                    cursor.executemany(
                        "INSERT OR IGNORE INTO processed_hashes (sha256) VALUES (?)",
                        [(h,) for h in old_hashes]
                    )
                    conn.commit()
            # Safely delete the old JSON file now that it's in the DB
            HISTORY_FILE.unlink()
            print("🗑️ Successfully migrated and deleted legacy history.json!")
        except Exception as e:
            print(f"⚠️ Error during migration: {e}")
            
    conn.close()

# Initialize DB and migration on script startup
init_db()

# ==========================================
# 3. CRYPTOGRAPHIC UTILITIES
# ==========================================
def calculate_sha256(file_path):
    """Calculate SHA-256 hash of a file to check for exact duplicates."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def is_duplicate_and_register(file_hash):
    """
    Checks the local SQLite database for the hash.
    If it exists, returns True.
    If it doesn't, registers it atomically and returns False.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT 1 FROM processed_hashes WHERE sha256 = ?", (file_hash,))
    exists = cursor.fetchone()
    
    if exists:
        conn.close()
        return True
    
    # Atomically log the new hash
    cursor.execute("INSERT INTO processed_hashes (sha256) VALUES (?)", (file_hash,))
    conn.commit()
    conn.close()
    return False

# ==========================================
# 4. PRIVACY SHIELD & ENGINE LOGIC
# ==========================================
def local_privacy_shield(text):
    """Scans text locally for sensitive patterns (API keys, cards, OTPs) before AI triggers."""
    patterns = {
        "API Key / Token": r"(sk_[a-zA-Z0-9]{24,}|AIzaSy[a-zA-Z0-9-_]{33})",
        "Credit/Debit Card": r"\b(?:\d[ -]*?){13,19}\b",
        "OTP / Verification": r"\b\d{4,6}\b.*\b(?:otp|one time|verification|code|pin)\b|\b(?:otp|one time|verification|code|pin)\b.*\b\d{4,6}\b"
    }
    for label, pattern in patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return True, label
    return False, None

def classify_with_gemini(text):
    """Fallback: Uses Gemini 2.5 Flash to classify difficult text."""
    prompt = f"""
    You are an automated file sorter. Classify this extracted OCR text into exactly ONE of these categories:
    Coding, Finance, Study, Anime, Personal, Other.

    Extracted Text:
    \"\"\"{text}\"\"\"

    Respond with ONLY the category name. No explanations, no markdown formatting.
    """
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        category = response.text.strip()
        if category in ["Coding", "Finance", "Study", "Anime", "Personal", "Other"]:
            return category
    except Exception as e:
        print(f"⚠️ Gemini API failed: {e}")
    return "Other"

# ==========================================
# 5. THE WATCHER PIPELINE
# ==========================================
class ScreenshotHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        if file_path.suffix.lower() not in ['.png', '.jpg', '.jpeg']:
            return
        
        # Give Windows a split second to finish writing the file to disk
        time.sleep(1)
        if not file_path.exists():
            return

        print(f"\n🔍 New screenshot detected: {file_path.name}")
        
        # 1. Check for Duplicate (Hardened SQLite check)
        try:
            file_hash = calculate_sha256(file_path)
        except Exception as e:
            print(f"⚠️ Could not calculate hash (file might be locked): {e}")
            return

        if is_duplicate_and_register(file_hash):
            print(f"🛡️ Duplicate detected (SHA-256 matched). Deleting clutter: {file_path.name}")
            try:
                file_path.unlink()
            except Exception as e:
                print(f"⚠️ Could not delete duplicate: {e}")
            return

        # 2. Local OCR Extraction
        ocr_text = ""
        try:
            ocr_text = pytesseract.image_to_string(Image.open(file_path))
        except Exception as e:
            print(f"⚠️ OCR failed: {e}")

        # 3. Privacy Shield Check
        is_sensitive, violation_type = local_privacy_shield(ocr_text)
        if is_sensitive:
            print(f"🚨 PRIVACY TRIGGERED: Detected {violation_type}. Isolating file immediately.")
            category = "Private"
            ocr_text = "[REDACTED BY PRIVACY SHIELD]"
        else:
            # 4. Classify: Local Keyword Match -> Gemini Fallback
            category = None
            clean_text = ocr_text.lower()
            for cat_name, keywords in CATEGORIES.items():
                if any(kw in clean_text for kw in keywords):
                    category = cat_name
                    print(f"🏷️ Local match found: {category}")
                    break
            
            if not category:
                print("🧠 Local keywords missed. Consulting Gemini...")
                category = classify_with_gemini(ocr_text)
                print(f"🏷️ Gemini assigned: {category}")

        # 5. File Movement & DB Logging
        target_folder = BASE_DIR / category
        target_folder.mkdir(exist_ok=True)
        dest_path = target_folder / file_path.name

        try:
            shutil.move(str(file_path), str(dest_path))
            print(f"📁 Moved to: {category}/{file_path.name}")
            
            # Save to SQLite FTS5 database
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                "INSERT INTO screenshots (filepath, category, ocr_text, timestamp) VALUES (?, ?, ?, ?)",
                (str(dest_path), category, ocr_text, timestamp)
            )
            conn.commit()
            conn.close()
            print("💾 Metadata indexed successfully!")
        except Exception as e:
            print(f"⚠️ Error moving file or logging to database: {e}")

# (Watchdog startup code continues below as normal)
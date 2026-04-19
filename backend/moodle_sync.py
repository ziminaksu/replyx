# moodle_sync.py autonomous agent that checks for new TUM lectures

import schedule
import time
import os
import json
import requests
from pathlib import Path
from slides import process_all_pdfs

MOODLE_URL = "https://www.moodle.tum.de"
DATA_DIR = "data/slides"

def check_new_lectures():
    """
    Agent checks for new PDFs automatically.
    In production: connects to Moodle API with student credentials.
    For demo: processes any new PDFs dropped in data/slides/
    """
    print("🤖 ReplyX Agent: checking for new lectures...")
    
    # Check if any new PDFs appeared
    pdf_files = list(Path(DATA_DIR).glob("*.pdf"))
    
    # Load what we already processed
    processed_path = "data/descriptions/slides.json"
    if os.path.exists(processed_path):
        with open(processed_path) as f:
            done = json.load(f)
        done_pdfs = {d["pdf"] for d in done}
    else:
        done_pdfs = set()
    
    new_pdfs = [p for p in pdf_files if p.name not in done_pdfs]
    
    if new_pdfs:
        print(f"📚 Found {len(new_pdfs)} new lectures! Processing...")
        process_all_pdfs()
        print("✅ New lectures added to knowledge base!")
    else:
        print("✅ Knowledge base is up to date!")

def start_agent():
    """Start the autonomous sync agent"""
    print("🚀 ReplyX Autonomous Agent started!")
    print("📡 Watching for new TUM lectures...")
    
    # Check immediately on start
    check_new_lectures()
    
    # Then check every day at 3am
    schedule.every().day.at("03:00").do(check_new_lectures)
    # Also check every hour for demo purposes
    schedule.every(1).hours.do(check_new_lectures)
    
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    start_agent()
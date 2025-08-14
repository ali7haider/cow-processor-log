import time
import random
from datetime import datetime
import ctypes
import sys
import os
import csv

# Paths
LOG_PATH = r'C:\Program Files\Afimilk\Logs\RTC\MILKINGPARLOR\RTC_MILKINGPARLOR.log'
CSV_PATH = r'D:\Study\Freelancing\Gary\cow-processor-log\map.csv'

def is_admin():
    """Check if script is running as administrator"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    """Rerun the script with admin rights"""
    print("🔐 Elevating to admin...")
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, " ".join(sys.argv), None, 1
    )

def load_csv_animals(csv_path):
    """Load Animal_ID/EART values from CSV, ignoring headers"""
    animals = []
    with open(csv_path, 'r', newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        headers = [h.strip() for h in reader.fieldnames]
        
        # Determine ID field
        id_field = None
        for possible in ('Animal_ID', 'EART'):
            if possible in headers:
                id_field = possible
                break
        if not id_field:
            id_field = headers[0]  # fallback

        for row in reader:
            animal_id = row.get(id_field, '').strip()
            if animal_id:
                animals.append(animal_id)
    return animals

def append_log_entry(animals):
    stall = f"ST{random.randint(1, 100):03d}"
    animal_tag = random.choice(animals)  # pick from CSV
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    entry = (
        f"{timestamp},......Thread,1002,Information,"
        f"\"RotaryAuto:Forward rotation processing starts. "
        f"Stall: {stall}, Stall Tag: 0, Animal {animal_tag}, "
        f"Stall time: 00:00:04.6000000 sec\"\n"
    )

    with open(LOG_PATH, 'a') as f:
        f.write(entry)

    print(f"✅ Written to real log: {stall} - {animal_tag}")

if __name__ == "__main__":
    if not is_admin():
        run_as_admin()
        sys.exit()

    animals = load_csv_animals(CSV_PATH)
    if not animals:
        print("❌ No animal IDs loaded from CSV")
        sys.exit(1)

    print(f"📄 Writing directly to: {LOG_PATH}")
    while True:
        try:
            append_log_entry(animals)
        except PermissionError as e:
            print(f"❌ Still no permission: {e}")
            break
        time.sleep(5)

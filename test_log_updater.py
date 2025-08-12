import time
import random
from datetime import datetime
import ctypes
import sys
import os

# Path to Afimilk log file
LOG_PATH = r'C:\Program Files\Afimilk\Logs\RTC\MILKINGPARLOR\RTC_MILKINGPARLOR.log'

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

def append_log_entry():
    stall = f"ST{random.randint(1, 100):03d}"
    animal_tag = random.randint(1000, 9999)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    entry = (
        f"{timestamp},......Thread,1002,Information,"
        f"\"RotaryAuto:Forward rotation processing starts. "
        f"Stall: {stall}, Stall Tag: 0, Animal Tag {animal_tag}, "
        f"Stall time: 00:00:04.6000000 sec\"\n"
    )

    with open(LOG_PATH, 'a') as f:
        f.write(entry)

    print(f"✅ Written to real log: {stall} - {animal_tag}")

if __name__ == "__main__":
    if not is_admin():
        run_as_admin()
        sys.exit()

    print(f"📄 Writing directly to: {LOG_PATH}")
    while True:
        try:
            append_log_entry()
        except PermissionError as e:
            print(f"❌ Still no permission: {e}")
            break
        time.sleep(5)

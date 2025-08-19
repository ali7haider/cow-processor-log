#!/usr/bin/env python3
"""
Milking Parlor Animal ID Processing System
PC to RevPi Data Bridge

Monitors milking parlor log files, processes cow data through circular queue,
cross-references IDs to EIDs, and sends to RevPi for Bluetooth transmission.

Author: Industrial Data Systems
Version: 1.0.0
"""

import os
import sys
import time
import csv
import json
import socket
import logging
import threading
import re
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List, Tuple
from pathlib import Path
import configparser
import signal

@dataclass
class CowDataEntry:
    """Data structure for cow processing entry"""
    stall: str
    animal_tag: str
    eid: Optional[str]
    position: int
    timestamp: datetime
    processed: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            'stall': self.stall,
            'animal_tag': self.animal_tag,
            'eid': self.eid,
            'position': self.position,
            'timestamp': self.timestamp.isoformat().replace(',', '.'),
            'processed': self.processed
        }

class CircularQueue:
    """Thread-safe circular queue implementation"""
    
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self.queue = deque(maxlen=max_size)
        self.lock = threading.Lock()
        self.position_counter = 0
    
    def add(self, entry: CowDataEntry) -> None:
        """Add entry to queue"""
        with self.lock:
            entry.position = self.position_counter
            self.position_counter += 1
            self.queue.append(entry)
    
    def get_by_offset(self, offset: int) -> Optional[CowDataEntry]:
        """Get entry by position offset (newest - offset)"""
        with self.lock:
            if len(self.queue) <= offset:
                return None
            
            # Get entry from offset positions back
            target_position = self.position_counter - offset - 1
            for entry in reversed(self.queue):
                if entry.position == target_position:
                    return entry
            return None
    
    def mark_processed(self, position: int) -> bool:
        """Mark entry as processed"""
        with self.lock:
            for entry in self.queue:
                if entry.position == position:
                    entry.processed = True
                    return True
            return False
    
    def get_status(self) -> dict:
        """Get queue status"""
        with self.lock:
            return {
                'size': len(self.queue),
                'max_size': self.max_size,
                'current_position': self.position_counter,
                'processed_count': sum(1 for entry in self.queue if entry.processed)
            }

class IDMapping:
    """Manages ID to EID mapping from CSV files"""
    
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.mapping: Dict[str, str] = {}
        self.last_modified = 0
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__)
    
    def load_mapping(self) -> bool:
        """Load ID to EID mapping from CSV file, auto-detecting header and delimiter"""
        try:
            if not os.path.exists(self.csv_path):
                self.logger.error(f"EID Map CSV file not found: {self.csv_path}")
                return False

            current_modified = os.path.getmtime(self.csv_path)
            if current_modified <= self.last_modified:
                return True  # No changes

            # Detect delimiter automatically
            with open(self.csv_path, 'r', newline='') as csvfile:
                sample = csvfile.read(2048)
                csvfile.seek(0)
                sniffer = csv.Sniffer()
                try:
                    dialect = sniffer.sniff(sample, delimiters=',\t;')
                except csv.Error:
                    dialect = csv.get_dialect('excel')

                reader = csv.DictReader(csvfile, dialect=dialect)
                headers = [h.strip() for h in reader.fieldnames] if reader.fieldnames else []

                # Determine which columns to use
                id_field = None
                eid_field = None

                # Priority 1: Expected names
                for possible in ('Animal_ID', 'EART'):
                    if possible in headers:
                        id_field = possible
                        break
                if 'EID' in headers:
                    eid_field = 'EID'

                # Priority 2: Fallback to first/second columns
                if not id_field and len(headers) >= 1:
                    id_field = headers[0]
                if not eid_field and len(headers) >= 2:
                    eid_field = headers[1]

                if not id_field or not eid_field:
                    self.logger.error("EID Map CSV does not contain enough columns to map EIDs")
                    return False

                # Read and populate mapping
                new_mapping = {}
                for row in reader:
                    animal_id = (row.get(id_field) or '').strip()
                    eid = (row.get(eid_field) or '').strip()
                    if animal_id and eid:
                        new_mapping[animal_id] = eid

            with self.lock:
                self.mapping = new_mapping
                self.last_modified = current_modified

            self.logger.info(f"Mapped {len(new_mapping)} EIDs from {self.csv_path}")
            return True

        except Exception as e:
            self.logger.error(f"Error mapping EIDs: {e}")
            return False

    
    def get_eid(self, animal_id: str) -> Optional[str]:
        """Get EID for given animal ID"""
        with self.lock:
            return self.mapping.get(animal_id)
    
    def get_mapping_count(self) -> int:
        """Get total number of mappings"""
        with self.lock:
            return len(self.mapping)

class LogFileMonitor:
    """Monitors and parses milking parlor log files"""
    
    def __init__(self, log_path: str):
        self.log_path = log_path
        self.last_position = 0
        self.last_modified = 0
        self.logger = logging.getLogger(__name__)
        self.prev_entries = set()

        self.rotation_pattern = re.compile(
    r'"RotaryAuto:(Forward|Reverse) rotation processing complete\. '
    r'Stall: (ST\d+), Stall Tag: \d+, Animal (\d+),'
)


    
    def check_for_updates(self) -> List[CowDataEntry]:
        entries = []

        try:
            if not os.path.exists(self.log_path):
                self.logger.warning(f"Log file not found: {self.log_path}")
                return entries

            current_modified = os.path.getmtime(self.log_path)
            file_size = os.path.getsize(self.log_path)

            # Detect rotation
            if current_modified != self.last_modified or file_size < self.last_position:
                self.last_position = 0
                self.last_modified = current_modified
                self.logger.info("Parlor position updated")

            # Read new data
            with open(self.log_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self.last_position)
                new_content = f.read()
                self.last_position = f.tell()

            if new_content:
                parsed_entries = self._parse_log_content(new_content)
                
                # Filter out entries already seen in last batch
                new_unique_entries = []
                for e in parsed_entries:
                    key = (e.stall, e.animal_tag, e.timestamp)
                    if key not in self.prev_entries:
                        new_unique_entries.append(e)

                # Update the memory for next call
                self.prev_entries = {(e.stall, e.animal_tag, e.timestamp) for e in parsed_entries}

                if new_unique_entries:
                    self.logger.info(f"Parsed {len(new_unique_entries)} animals from log file")
                
                entries = new_unique_entries

        except Exception as e:
            self.logger.error(f"Error reading log file: {e}")

        return entries
    
    def _parse_log_content(self, content: str) -> List[CowDataEntry]:
        """Parse log content for rotation events"""
        entries = []
        lines = content.split('\n')
        
        for line in lines:
            if "RotaryAuto:Forward rotation processing complete" in line or \
           "RotaryAuto:Reverse rotation processing complete" in line:
                entry = self._parse_rotation_line(line)
                if entry:
                    entries.append(entry)
        
        return entries
    
    def _parse_rotation_line(self, line: str) -> Optional[CowDataEntry]:
        """Parse individual rotation log line"""
        try:
            # Extract timestamp
            timestamp_str = line.split(',')[0]
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
            
            # Extract stall and animal tag using regex
            match = self.rotation_pattern.search(line)
            if match:
                stall = match.group(2)
                animal_tag = match.group(3)
                
                return CowDataEntry(
                    stall=stall,
                    animal_tag=animal_tag,
                    eid=None,  # Will be filled later via mapping
                    position=0,  # Will be set by queue
                    timestamp=timestamp
                )
        
        except Exception as e:
            self.logger.debug(f"Error parsing log line: {e}")
        
        return None
class CSVOutputHandler:
    """Handles CSV file output as alternative to RevPi communication"""
    
    def __init__(self, output_file: str):
        self.output_file = output_file
        self.logger = logging.getLogger(__name__)
        self.lock = threading.Lock()
        self._initialize_csv_file()
    
    def _initialize_csv_file(self):
        """Initialize CSV file with headers if it doesn't exist"""
        try:
            if not os.path.exists(self.output_file):
                with open(self.output_file, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(['timestamp', 'stall', 'eid', 'status'])
                self.logger.info(f"Created new CSV output file: {self.output_file}")
        except Exception as e:
            self.logger.error(f"Error initializing Output CSV file: {e}")

    def send_data(self, stall: str, eid: str, animal_tag: str) -> bool:
        """Write stall and EID data to CSV file"""
        try:
            with self.lock:
                with open(self.output_file, 'a', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow([
                        datetime.now().isoformat(),
                        stall,
                        eid,
                        'processed'
                    ])
                
                self.logger.info(f"Transmitted: {stall}-{animal_tag}-{eid}")
                return True
                
        except Exception as e:
            self.logger.error(f"Error writing data to Output CSV: {e}")
            return False
    
    def test_connection(self) -> bool:
        """Test if CSV file can be written to"""
        try:
            # Test write access
            with open(self.output_file, 'a', encoding='utf-8') as f:
                pass
            return True
        except Exception as e:
            self.logger.error(f"Output CSV file test failed: {e}")
            return False

class RevPiCommunicator:
    """Handles network communication with RevPi"""
    
    def __init__(self, host: str, port: int, timeout: int = 5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
    
    def send_data(self, stall: str, eid: str,animal_tag: str) -> bool:
        """Send stall and EID data to RevPi"""
        try:
            data = {
                'stall': stall,
                'eid': eid,
                'timestamp': datetime.now().isoformat()
            }
            
            message = json.dumps(data) + '\n'
            
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                sock.sendall(message.encode('utf-8'))
                
                # Wait for acknowledgment
                response = sock.recv(1024).decode('utf-8').strip()
                if response == 'OK':
                    self.logger.info(f"Successfully sent data to RevPi: {stall} -> {eid} with Animal Tag {animal_tag}")
                    return True
                else:
                    self.logger.error(f"Unexpected response from RevPi: {response}")
                    return False
                    
        except Exception as e:
            self.logger.error(f"Error sending data to RevPi: {e}")
            return False
    
    def test_connection(self) -> bool:
        """Test connection to RevPi"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                result = sock.connect_ex((self.host, self.port))
                return result == 0
        except Exception:
            return False

class CowDataProcessor:
    """Main application class"""
    
    def __init__(self, config_path: str = 'config.ini'):
        self.config = self._load_config(config_path)
        self.running = False
        self.setup_logging()
        self.logger = logging.getLogger(__name__)

        # Initialize components
        self.queue = CircularQueue(self.config.getint('processing', 'parlor_stalls', fallback=100))
        self.id_mapping = IDMapping(self.config.get('files', 'eid_map_csv'))
        self.log_monitor = LogFileMonitor(self.config.get('files', 'parlor_log'))
        self.output_mode = self.config.get('output', 'mode', fallback='revpi').lower()
        
        if self.output_mode == 'csv':
            self.output_handler = CSVOutputHandler(
                self.config.get('output', 'csv_file', fallback='cow_data_output.csv')
            )
            self.logger.info("Using CSV output mode")
        else:
            self.output_handler = RevPiCommunicator(
                self.config.get('network', 'revpi_host'),
                self.config.getint('network', 'revpi_port')
            )
            self.logger.info("Using RevPi output mode")


        self.stats = {
            'processed_today': 0,
            'errors_today': 0,
            'last_transmission': None,
            'start_time': datetime.now()
        }
    
    def _load_config(self, config_path: str) -> configparser.ConfigParser:
        """Load configuration from file"""
        config = configparser.ConfigParser()
        
        # Default configuration
        config.read_dict({
            'files': {
                'parlor_log': r'C:\Program Files\Afimilk\Logs\RTC\MILKINGPARLOR\RTC_MILKINGPARLOR.log',
                'eid_map_csv': 'id_mapping.csv'
            },
            'processing': {
                'parlor_interval': '3',
                'parlor_stalls': '100',
                'transmission_offset': '60'
            },
            'network': {
                'revpi_host': '192.168.1.100',
                'revpi_port': '8080'
            },
            'logging': {
                'level': 'INFO',
                'file': 'cow_processor.log',
                'max_size_mb': '10',
                'backup_count': '5'
            }
        })
        
        # Load from file if exists
        if os.path.exists(config_path):
            config.read(config_path)
        else:
            # Create default config file
            with open(config_path, 'w') as f:
                config.write(f)
        
        return config
    
    def setup_logging(self):
        """Setup logging configuration with dot-separated milliseconds"""

        import logging
        from logging.handlers import RotatingFileHandler
        from datetime import datetime

        class DotMillisFormatter(logging.Formatter):
            def formatTime(self, record, datefmt=None):
                ct = datetime.fromtimestamp(record.created)
                if datefmt:
                    # Format without microseconds, then append milliseconds
                    s = ct.strftime(datefmt)
                    return s + f".{int(record.msecs):03d}"
                else:
                    t = ct.strftime("%Y-%m-%d %H:%M:%S")
                    return f"{t}.{int(record.msecs):03d}"

        log_level = getattr(logging, self.config.get('logging', 'level', fallback='INFO'))
        log_file = self.config.get('logging', 'file', fallback='cow_processor.log')

        # Formatter with only milliseconds
        formatter = DotMillisFormatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # File handler with rotation
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=self.config.getint('logging', 'max_size_mb', fallback=10) * 1024 * 1024,
            backupCount=self.config.getint('logging', 'backup_count', fallback=5)
        )
        file_handler.setFormatter(formatter)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        logging.basicConfig(
            level=log_level,
            handlers=[file_handler, console_handler]
        )

    def start(self):
        """Start the data processing system"""
        self.logger.info("Starting Animal ID Processing System")
        self.running = True
        
        if self.output_handler.test_connection():
            self.logger.info(f"{self.output_mode.upper()} connection test successful")
        else:
            self.logger.warning(f"{self.output_mode.upper()} connection test failed - will retry during operation")
        # Load initial ID mapping
        self.id_mapping.load_mapping()
        
        # Start main processing loop
        try:
            self._main_loop()
        except KeyboardInterrupt:
            self.logger.info("Received shutdown signal")
        except Exception as e:
            self.logger.error(f"Unexpected error in main loop: {e}")
        finally:
            self.stop()
    
    def stop(self):
        """Stop the data processing system"""
        self.logger.info("Stopping Cow Data Processing System")
        self.running = False
    
    def _main_loop(self):
        """Main processing loop"""
        parlor_interval = self.config.getfloat('processing', 'parlor_interval', fallback=3.0)
        transmission_offset = self.config.getint('processing', 'transmission_offset', fallback=60)
        
        last_mapping_check = 0
        mapping_parlor_interval = 60*60  # Check mapping file every hour

        while self.running:
            try:
                # Check for ID mapping updates
                current_time = time.time()
                if current_time - last_mapping_check > mapping_parlor_interval:
                    self.id_mapping.load_mapping()
                    last_mapping_check = current_time
                    self.logger.info(f"Checking for EID Map CSV file updates every {mapping_parlor_interval / 60:.0f} minutes...")

                
                # Check for new log entries
                new_entries = self.log_monitor.check_for_updates()
                
                # Process new entries
                for entry in new_entries:
                    # Get EID for animal ID
                    entry.eid = self.id_mapping.get_eid(entry.animal_tag)
                    
                    # Add to queue
                    self.queue.add(entry)

                    if entry.eid:
                        self.logger.info(
                            f"New Animal: "
                            f"{entry.stall}-{entry.animal_tag}-{entry.eid}"
                        )
                    else:
                        self.logger.info(
                            f"New Animal: "
                            f"{entry.stall}-{entry.animal_tag}-NO_EID"
        )
                
                # Check for entries ready for transmission
                entry_to_transmit = self.queue.get_by_offset(transmission_offset)
                if entry_to_transmit and not entry_to_transmit.processed:
                    eid_to_send = entry_to_transmit.eid if entry_to_transmit.eid else "0"

                    success = self.output_handler.send_data(entry_to_transmit.stall, eid_to_send,entry_to_transmit.animal_tag)
                    if success:
                        self.queue.mark_processed(entry_to_transmit.position)
                        self.stats['processed_today'] += 1
                        self.stats['last_transmission'] = datetime.now()
                    else:
                        self.stats['errors_today'] += 1
                
                # Log periodic status
                if int(current_time) % 300 == 0:  # Every 5 minutes
                    self._log_status()
                
                time.sleep(parlor_interval)
                
            except Exception as e:
                self.logger.error(f"Error in main loop iteration: {e}")
                self.stats['errors_today'] += 1
                time.sleep(parlor_interval)
    
    def _log_status(self):
        """Log current system status"""
        queue_status = self.queue.get_status()
        mapping_count = self.id_mapping.get_mapping_count()
        uptime = datetime.now() - self.stats['start_time']
        
        self.logger.info(
            f"STATUS - Queue: {queue_status['size']}/{queue_status['max_size']}, "
            f"Mappings: {mapping_count}, Processed today: {self.stats['processed_today']}, "
            f"Errors today: {self.stats['errors_today']}, Uptime: {uptime}"
        )
    
    def get_status(self) -> dict:
        """Get current system status"""
        return {
            'running': self.running,
            'queue': self.queue.get_status(),
            'mapping_count': self.id_mapping.get_mapping_count(),
            'stats': self.stats,
            'uptime': str(datetime.now() - self.stats['start_time'])
        }

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print("\nReceived shutdown signal, stopping gracefully...")
    global processor
    if 'processor' in globals():
        processor.stop()

def main():
    """Main entry point"""
    global processor
    
    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("Cow Data Processing System v1.0.0")
    print("PC to RevPi Data Bridge")
    print("-" * 40)
    
    # Create and start processor
    processor = CowDataProcessor()
    processor.start()
    
if __name__ == "__main__":
    main()#!/usr/bin/env python3

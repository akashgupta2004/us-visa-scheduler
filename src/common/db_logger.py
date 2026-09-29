import logging
import queue
import threading
import os
import atexit
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING

class MongoDBLogger:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MongoDBLogger, cls).__new__(cls)
                cls._instance._init_logger()
            return cls._instance

    def _init_logger(self):
        # MongoDB logging disabled.
        # Local terminal/orchestrator/extension logs remain unaffected.
        self.queue = queue.Queue()
        self.flush_interval = 1800
        self.batch_size = 500
        self.running = False
        self.logging_enabled = False
        self.client = None
        self.db = None
        self.collection = None

    def _flush_thread(self):
        while self.running:
            batch = []
            try:
                # Wait for the first item up to flush_interval
                item = self.queue.get(timeout=self.flush_interval)
                batch.append(item)
                
                # Try to get more items immediately up to batch_size
                while len(batch) < self.batch_size:
                    try:
                        item = self.queue.get_nowait()
                        batch.append(item)
                    except queue.Empty:
                        break
            except queue.Empty:
                pass # Timeout reached, flush if anything
                
            if batch:
                self._insert_batch(batch)

    def _insert_batch(self, batch):
        if self.collection is None:
            return
        try:
            self.collection.insert_many(batch, ordered=False)
        except Exception as e:
            print(f"Failed to insert logs to MongoDB: {e}")

    def log(self, record):
        if not getattr(self, 'logging_enabled', True):
            return
        doc = {
            "createdAt": datetime.now(timezone.utc),
            "level": record.levelname,
            "message": record.getMessage(),
            "name": record.name,
            "module": record.module,
            "line": record.lineno,
        }
        self.queue.put(doc)
        
    def log_extension_console(self, timestamp_str, level, customer, message):
        if not getattr(self, 'logging_enabled', True):
            return
        # Specific method for handling browser console logs directly
        try:
            # Parse timestamp if possible, otherwise use now
            dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
            
        doc = {
            "createdAt": dt,
            "level": level,
            "message": message,
            "name": "extension_console",
            "customer": customer
        }
        self.queue.put(doc)

    def toggle_logging(self, enable: bool):
        self.logging_enabled = enable

    def flush(self):
        self.running = False
        batch = []
        while not self.queue.empty():
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._insert_batch(batch)


class MongoDBHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.db_logger = MongoDBLogger()

    def emit(self, record):
        try:
            self.db_logger.log(record)
        except Exception:
            self.handleError(record)

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
        self.queue = queue.Queue()
        self.flush_interval = 1800  # 30 minutes
        self.batch_size = 500       # Max items before forced flush
        self.running = True
        
        # Load from env or use defaults provided
        mongo_uri = os.getenv("MONGO_URI", "mongodb+srv://lakshya:eiFAT3ppk5N7Fde@cluster0.0nsi2e7.mongodb.net/?retryWrites=true&w=majority")
        db_name = os.getenv("MONGODB_DATABASE_NAME", "LeSo")
        
        try:
            self.client = MongoClient(mongo_uri)
            self.db = self.client[db_name]
            self.collection = self.db["logs"]
            
            # Setup TTL index for 48-hour retention (172800 seconds)
            self.collection.create_index([("createdAt", ASCENDING)], expireAfterSeconds=172800)
        except Exception as e:
            print(f"Failed to initialize MongoDB connection: {e}")
            self.collection = None

        self.thread = threading.Thread(target=self._flush_thread, daemon=True)
        self.thread.start()
        atexit.register(self.flush)

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
        if not self.collection:
            return
        try:
            self.collection.insert_many(batch, ordered=False)
        except Exception as e:
            print(f"Failed to insert logs to MongoDB: {e}")

    def log(self, record):
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

from collections import deque
from datetime import datetime

# Global log buffer (In-memory)
# Stores the last 100 log entries
LOG_BUFFER = deque(maxlen=100)


def log_activity(message, level="INFO"):
    """
    Adds a message to the global activity log buffer.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {
        "timestamp": timestamp,
        "message": message,
        "level": level
    }
    LOG_BUFFER.append(entry)
    # Print to console for server debugging
    print(f"[{timestamp}] {level}: {message}")


def get_logs():
    """Returns list of log entries."""
    return list(LOG_BUFFER)


def get_icon(level):
    """Get icon for log level."""
    icons = {
        "ERROR": "❌",
        "WARNING": "⚠️",
        "SUCCESS": "✅",
        "INFO": "🔹"
    }
    return icons.get(level, "🔹")

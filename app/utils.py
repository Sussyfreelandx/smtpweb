from collections import deque
from datetime import datetime

LOG_BUFFER = deque(maxlen=100)


def log_activity(message, level="INFO"):
    timestamp = datetime. now().strftime("%H:%M:%S")
    entry = {
        "timestamp": timestamp,
        "message": message,
        "level":  level
    }
    LOG_BUFFER.append(entry)
    print(f"[{timestamp}] {level}:  {message}")


def get_logs():
    return list(LOG_BUFFER)


def get_icon(level):
    if level == "ERROR":
        return "X"
    if level == "WARNING":
        return "!"
    if level == "SUCCESS":
        return "OK"
    return "i"

import logging
import sys
import time

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


class _MsecFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        t = time.strftime(LOG_DATE_FORMAT, ct)
        return f"{t}.{int(record.msecs):03d}"


def setup_logging(level=logging.INFO):
    global _initialized
    if _initialized:
        return
    _initialized = True
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_MsecFormatter(LOG_FORMAT))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

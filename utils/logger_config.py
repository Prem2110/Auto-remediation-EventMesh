"""
Simple logger configuration for the MCP project.
Creates a logger that writes to 'logs/mcp.log' with a specific format.
"""
# import logging
# import os

# def setup_logger(name: str):
#     logger = logging.getLogger(name)
#     logger.setLevel(logging.INFO)

#     if logger.hasHandlers():
#         logger.handlers.clear()

#     # Ensure logs directory exists
#     log_dir = os.path.join(os.getcwd(), "logs")
#     os.makedirs(log_dir, exist_ok=True)

#     log_file = os.path.join(log_dir, "mcp.log")

#     file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
#     formatter = logging.Formatter(
#         "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
#     )
#     file_handler.setFormatter(formatter)

#     logger.addHandler(file_handler)

#     logger.propagate = False

#     return logger

"""
Production-grade logging configuration for the MCP project.

All application logs are routed through a single rotating file handler at
logs/mcp.log so they do not clutter the terminal.
"""
from logging.handlers import RotatingFileHandler
import logging
import os
import warnings


LOG_FILE_NAME = "mcp.log"
LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(name)s | "
    "%(module)s:%(funcName)s:%(lineno)d | "
    "pid=%(process)d tid=%(thread)d | %(message)s"
)
MAX_LOG_BYTES = 3 * 1024 * 1024
BACKUP_COUNT = 5
RESET_LOG_ON_START = False
ENABLE_CONSOLE_LOGS = os.getenv("ENABLE_CONSOLE_LOGS", "true").lower() == "true"



def configure_logging(level: int = logging.INFO):
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    log_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, LOG_FILE_NAME)

    file_handler = None
    for handler in list(root_logger.handlers):
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", None) == log_file:
            file_handler = handler
            continue
        root_logger.removeHandler(handler)

    if file_handler is None:
        if RESET_LOG_ON_START and os.path.exists(log_file):
            try:
                os.remove(log_file)
            except PermissionError:
                # Windows reload mode can leave another process holding the file.
                # Keep the existing log instead of failing application startup.
                pass
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        root_logger.addHandler(file_handler)

    console_handler = None
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, RotatingFileHandler):
            console_handler = handler
            break

    if ENABLE_CONSOLE_LOGS and console_handler is None:
        console_handler = logging.StreamHandler()
        root_logger.addHandler(console_handler)
    if not ENABLE_CONSOLE_LOGS and console_handler is not None:
        root_logger.removeHandler(console_handler)
        console_handler = None

    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    if console_handler is not None:
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    _uvicorn_console = logging.StreamHandler()
    _uvicorn_console.setLevel(level)
    _uvicorn_console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        named_logger = logging.getLogger(logger_name)
        named_logger.handlers.clear()
        named_logger.addHandler(_uvicorn_console)
        named_logger.setLevel(level)
        named_logger.propagate = False

    # Reduce auto-reload file watcher noise in logs.
    for logger_name in ("watchfiles", "watchfiles.main"):
        watch_logger = logging.getLogger(logger_name)
        watch_logger.handlers.clear()
        watch_logger.setLevel(logging.WARNING)
        watch_logger.propagate = True

    logging.captureWarnings(True)
    warnings_logger = logging.getLogger("py.warnings")
    warnings_logger.handlers.clear()
    warnings_logger.setLevel(level)
    warnings_logger.propagate = True

    return root_logger


def setup_logger(name: str):
    configure_logging()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    return logger

"""
Utility functions for the application
"""
from datetime import datetime
from utils.logger_config import setup_logger

logger = setup_logger("utils")

def get_hana_timestamp() -> str:
    """
    Generate a HANA-compatible timestamp string
    
    Returns:
        Formatted timestamp string
    """
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond:06d}" + "000"


def format_mcp_response(result: str) -> str:
    """
    Format MCP response, handling None or empty results

    Args:
        result: Raw result from MCP processing

    Returns:
        Formatted result string
    """
    if not result:
        logger.warning("Maximum iteration reached by AGENT")
        return "Maximum iteration reached by AGENT."
    return result.strip()


def clean_error_message(raw_error: str) -> str:
    """
    Strip SAP CPI noise from error messages.
    Keeps the core error text, removes stack traces,
    MPL IDs, and Java class name prefixes.
    """
    import re
    if not raw_error:
        return ""

    cleaned = raw_error

    # Remove MPL ID line
    cleaned = re.sub(
        r'\nThe MPL ID for the failed message is\s*:\s*\S+',
        '', cleaned
    )

    # Remove Java stack trace lines
    cleaned = re.sub(r'\n\tat .*', '', cleaned)
    cleaned = re.sub(r'\n\t\.\.\..*', '', cleaned)

    # Shorten Java class name prefixes — keep just the exception class
    # e.g. com.sap.it.rt.adapter.http.api.exception.HttpResponseException:
    # → HttpResponseException:
    cleaned = re.sub(
        r'com\.sap\.[a-z.]+\.([A-Z][a-zA-Z]+Exception)',
        r'\1', cleaned
    )
    cleaned = re.sub(
        r'com\.sap\.[a-z.]+\.([A-Z][a-zA-Z]+Error)',
        r'\1', cleaned
    )

    # Remove duplicate whitespace and leading/trailing space
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = cleaned.strip()

    # Truncate to 600 chars of meaningful content
    return cleaned[:600]
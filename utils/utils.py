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
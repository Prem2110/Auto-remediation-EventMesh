"""
Configuration management using environment variables
"""
import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Runtime config file path
RUNTIME_CONFIG_PATH = Path(__file__).parent / "runtime_config.json"


class Settings:
    """Application configuration settings"""
    
    # HANA Database Configuration
    HANA_HOST: str = os.getenv("HANA_HOST")
    HANA_PORT: int = int(os.getenv("HANA_PORT", "443"))
    HANA_USER: str = os.getenv("HANA_USER")
    HANA_PASSWORD: str = os.getenv("HANA_PASSWORD")
    HANA_SCHEMA: str = os.getenv("HANA_SCHEMA")
    HANA_TABLE_QUERY_HISTORY: str = os.getenv("HANA_TABLE_QUERY_HISTORY", "MCP_QUERY_HISTORY")
    HANA_TABLE_USER_FILES: str = os.getenv("HANA_TABLE_USER_FILES", "USER_FILES_METADATA")
    HANA_TABLE_XSD_FILES: str = os.getenv("HANA_TABLE_XSD_FILES", "SAP_IS_XSD_FILES")
    
    # MCP Server Configuration
    MCP_SERVER_PATH: str = os.path.join("./dist", "index.js")
    
    # Upload Configuration
    UPLOAD_ROOT: str = os.getenv("UPLOAD_ROOT")
    
    # # API Configuration
    # API_HOST: str = os.getenv("API_HOST")
    # API_PORT: int = int(os.getenv("API_PORT"))


# Global settings instance
settings = Settings()


class Config:
    """Configuration class with runtime override support"""
    
    @staticmethod
    def get_auto_fix_enabled():
        """Get auto-fix setting from runtime config or fall back to .env"""
        try:
            if RUNTIME_CONFIG_PATH.exists():
                with open(RUNTIME_CONFIG_PATH, 'r') as f:
                    runtime_config = json.load(f)
                    # If runtime config has a value (not null), use it
                    if runtime_config.get("auto_fix_enabled") is not None:
                        return runtime_config["auto_fix_enabled"]
        except Exception as e:
            print(f"Error reading runtime config: {e}")
        
        # Fall back to .env setting
        return os.getenv("AUTO_FIX_ENABLED", "false").lower() == "true"
    
    @staticmethod
    def set_auto_fix_enabled(enabled: bool):
        """Set auto-fix setting in runtime config"""
        try:
            runtime_config = {"auto_fix_enabled": enabled}
            with open(RUNTIME_CONFIG_PATH, 'w') as f:
                json.dump(runtime_config, f, indent=2)
            return True
        except Exception as e:
            print(f"Error writing runtime config: {e}")
            return False
    
    @staticmethod
    def reset_auto_fix_to_env():
        """Reset auto-fix to use .env value"""
        try:
            runtime_config = {"auto_fix_enabled": None}
            with open(RUNTIME_CONFIG_PATH, 'w') as f:
                json.dump(runtime_config, f, indent=2)
            return True
        except Exception as e:
            print(f"Error resetting runtime config: {e}")
            return False

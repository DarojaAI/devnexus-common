"""
Standards Loader

Provides a simple interface for loading the standards compliance configuration.
Used by A2A skills to get configurable stage labels and check definitions.
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional

import yaml


def _find_config() -> Path:
    """
    Find the standards-config.yaml file.
    
    Searches in order:
    1. Environment variable STANDARDS_CONFIG_PATH
    2. Relative to this file's directory: compliance/standards-config.yaml
    3. Current working directory: compliance/standards-config.yaml
    
    Returns:
        Path to the config file
        
    Raises:
        FileNotFoundError: If no config file is found
    """
    # Check environment variable
    env_path = os.environ.get("STANDARDS_CONFIG_PATH")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    
    # Check relative to this file
    file_dir = Path(__file__).parent
    relative_path = file_dir / "standards-config.yaml"
    if relative_path.exists():
        return relative_path
    
    # Check current working directory
    cwd_path = Path("compliance/standards-config.yaml")
    if cwd_path.exists():
        return cwd_path
    
    raise FileNotFoundError(
        "standards-config.yaml not found. Set STANDARDS_CONFIG_PATH env var or "
        "ensure compliance/standards-config.yaml exists relative to working directory."
    )


def load_standards_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load the standards compliance configuration.
    
    Loads and parses the standards-config.yaml file, returning its contents
    as a dictionary. Used by readiness assessment skills to get configurable
    stage labels and check definitions.
    
    Args:
        config_path: Optional explicit path to config file. If None, searches
                    using _find_config().
    
    Returns:
        Dictionary containing the parsed YAML configuration.
        Returns empty dict if file not found (caller should handle gracefully).
    
    Example:
        >>> config = load_standards_config()
        >>> stages = config.get("readiness_stages", [])
        >>> for stage in stages:
        ...     print(f"Stage {stage['stage']}: {stage['label']}")
    """
    try:
        if config_path:
            path = Path(config_path)
        else:
            path = _find_config()
        
        with open(path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        return config
    
    except FileNotFoundError:
        # Return empty dict - callers handle gracefully with fallback defaults
        print(f"[WARN] Standards config not found, using empty config")
        return {}
    except yaml.YAMLError as e:
        print(f"[ERROR] Failed to parse standards config: {e}")
        return {}
    except Exception as e:
        print(f"[ERROR] Unexpected error loading standards config: {e}")
        return {}


def get_readiness_stages() -> list:
    """
    Get the readiness stages configuration.
    
    Convenience function that loads the full config and extracts
    the readiness_stages list.
    
    Returns:
        List of readiness stage definitions, or empty list if not found.
    """
    config = load_standards_config()
    return config.get("readiness_stages", [])


def get_readiness_checks() -> Dict[str, Any]:
    """
    Get the readiness checks configuration.
    
    Convenience function that loads the full config and extracts
    the readiness_checks dictionary.
    
    Returns:
        Dict of check_id -> check definition, or empty dict if not found.
    """
    config = load_standards_config()
    return config.get("readiness_checks", {})

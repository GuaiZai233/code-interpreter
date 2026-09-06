# worker/meta_config.py
"""
Configuration management for the worker service.
"""
import os


# --- Execution Configuration ---
EXECUTION_TIMEOUT: float = float(os.environ.get("EXECUTION_TIMEOUT", 120.0))  # 120 seconds default

# --- Supervisor Configuration ---
SUPERVISOR_RPC_URL: str = os.environ.get("SUPERVISOR_RPC_URL", "http://127.0.0.1:9001/RPC2")

#!/usr/bin/env python3
"""Start the Leak Hunt MCP server (see addon/server.py for environment variables)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from addon.server import main

if __name__ == "__main__":
    main()

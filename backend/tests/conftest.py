"""
Shared pytest configuration for the Second Brain test suite.
"""
import sys
import os

# Ensure `backend/` is on sys.path so `import retry`, `import health`, etc. work.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

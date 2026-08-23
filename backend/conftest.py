"""
conftest.py — pytest configuration for backend tests.

Adds the backend/ directory to sys.path so that `from data.schema import ...`
works without installing the package.
"""

import sys
import os

# Ensure backend/ is on sys.path regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(__file__))

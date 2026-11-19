"""Launcher used by PyInstaller and for running from source: python app/run.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smo.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

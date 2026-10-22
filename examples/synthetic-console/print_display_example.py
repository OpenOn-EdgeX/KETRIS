"""Compatibility entry point for the explicitly synthetic display example."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).with_name('print_synthetic_example.py')), run_name='__main__')

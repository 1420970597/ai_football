#!/usr/bin/env python3
"""Reuse the repository report renderer, including readable Mermaid fallback."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    renderer = Path(__file__).resolve().parents[2] / 'dashboard-data-stats-optimization/scripts/md_to_html.py'
    runpy.run_path(str(renderer), run_name='__main__')

#!/usr/bin/env python3
"""Reuse the repository renderer, with Mermaid and readable offline fallback."""
from pathlib import Path
import runpy

SHARED = Path(__file__).resolve().parents[2] / 'leyu-kaiyun-odds-bot-feasibility/scripts/md_to_html.py'

if __name__ == '__main__':
    runpy.run_path(str(SHARED), run_name='__main__')

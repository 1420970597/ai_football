#!/usr/bin/env python3
"""Reuse the existing Markdown/Mermaid renderer without a parallel copy."""
import runpy
from pathlib import Path

if __name__ == '__main__':
    renderer = Path(__file__).resolve().parents[2]/'leyu-kaiyun-odds-bot-feasibility/scripts/md_to_html.py'
    runpy.run_path(str(renderer),run_name='__main__')

#!/usr/bin/env python3
"""
Parse Hermes' reply from stdin and extract the JSON decision block.

Supports two output formats:
  1. Raw JSON immediately after "---DECISION---" delimiter
  2. YAML-style indented key: value blocks (older style)
Falls back to HOLD on any parse failure.
"""

import sys
import json
import re

def _parse_yaml_style(text: str) -> dict:
    """Parse lines like:  action: "ENTER"  or  size_btc: 0.01"""
    mapping = {
        'action': str, 'side': str, 'size': float, 'asset': str,
        'limit_offset_bps': int, 'leverage': int, 'reason': str,
        'sl_price': float, 'tp_price': float
    }
    result = {}
    for line in text.split('\n'):
        # Match "  key: value"  with optional quotes
        m = re.match(r'^\s*([a-z_]+)\s*:\s*("?[^"]*"|[-.0-9]+)\s*$', line)
        if m:
            key, raw_val = m.groups()
            if key not in mapping:
                continue
            try:
                # Strip quotes if present
                if raw_val.startswith('"') and raw_val.endswith('"'):
                    result[key] = raw_val[1:-1]
                elif '.' in raw_val or 'e' in raw_val.lower():
                    result[key] = float(raw_val)
                elif raw_val.lstrip('-').isdigit():
                    result[key] = int(raw_val)
                else:
                    result[key] = raw_val
            except Exception:
                result[key] = raw_val
    # Ensure required keys exist with sensible defaults
    defaults = {
        'action': result.get('action', 'HOLD'),
        'side': result.get('side'),
        'size': float(result.get('size', 0.0)),
        'asset': result.get('asset', 'BTC'),
        'limit_offset_bps': int(result.get('limit_offset_bps', 0)),
        'leverage': int(result.get('leverage', 1)),
        'sl_price': float(result.get('sl_price', 0.0)) or None,
        'tp_price': float(result.get('tp_price', 0.0)) or None,
        'reason': result.get('reason', 'yaml_parse')
    }
    return defaults

def main():
    text = sys.stdin.read()
    idx = text.find('---DECISION---')
    if idx < 0:
        print(json.dumps({
            'action': 'HOLD', 'side': None, 'size': 0.0, 'asset': 'BTC',
            'limit_offset_bps': 0, 'leverage': 1,
            'sl_price': None, 'tp_price': None, 'reason': 'no_delimiter'
        }))
        sys.exit(0)

    after = text[idx + 14:]  # len('---DECISION---') = 14

    # Strategy 1: look for raw JSON starting with {
    brace = after.find('{')
    if brace >= 0:
        candidate = after[brace:]
        try:
            obj, end = json.JSONDecoder().raw_decode(candidate)
            if end > 0:
                print(json.dumps(obj))
                sys.exit(0)
        except Exception:
            pass  # Fall through to YAML parser

    # Strategy 2: parse YAML-style indented key: value lines
    obj = _parse_yaml_style(after)
    print(json.dumps(obj))

if __name__ == '__main__':
    main()

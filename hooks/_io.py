"""Hook wire input and output shared by the harness adapters.

Bytes in and ASCII JSON out, so a non-ASCII title survives a cp1252 pipe on Windows."""
import json,sys

def read_payload():
    """The hook payload object from stdin (UTF-8, with or without a BOM); {} when stdin is empty."""
    raw=sys.stdin.buffer.read().decode('utf-8-sig')
    payload=json.loads(raw) if raw.strip() else {}
    if not isinstance(payload,dict):raise ValueError('Hook payload must be an object')
    return payload

def emit(obj):
    """One JSON reply line on stdout."""
    sys.stdout.write(json.dumps(obj,separators=(',',':'))+'\n');sys.stdout.flush()

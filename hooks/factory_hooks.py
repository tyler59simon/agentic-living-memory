"""Factory Droid native adapter; Factory hooks use the Claude-compatible wire shape."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hooks.claude_hooks import main
if __name__=='__main__':raise SystemExit(main('factory'))

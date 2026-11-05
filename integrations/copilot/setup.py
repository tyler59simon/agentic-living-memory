"""Compatibility entrypoint for governed Copilot setup."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from integrations.setup import main
if __name__=='__main__':raise SystemExit(main(['copilot']+sys.argv[1:]))

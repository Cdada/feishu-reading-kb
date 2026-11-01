"""Read-only, explicitly invoked inventory; never launches the service."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parent/'src'))
from kb_custody_v21.archive_retention import main
if __name__=='__main__':main()

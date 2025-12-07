"""Entry point for python -m urpm"""

import sys
from urpm.cli.main import main

if __name__ == '__main__':
    sys.exit(main())

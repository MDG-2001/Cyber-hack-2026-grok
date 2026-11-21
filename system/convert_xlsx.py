#!/usr/bin/env python3
"""python3 system/convert_xlsx.py sheet.xlsx data/"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screen.convert import main

if __name__ == "__main__":
    main()

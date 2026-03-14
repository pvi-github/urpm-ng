# Running tests from test_install.py
## Requirements
- genhslist2
- gendistrib
## Running
 cd urpm/tests
 PYTHONPATH=<location>/urpm-ng python3 -m pytest test_install.py
## Instructions
For now, not working tests are marked to be skipped. The reason of the fail is written within the decorator. Remove the decorator to have it included in the tests

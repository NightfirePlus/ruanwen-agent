"""运行全部单元测试:python run_tests.py"""
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path[:0] = [str(root), str(root / "tests")]
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
sys.exit(0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1)

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeLLM  # noqa: F401  (确保 sys.path 已设置)

from agent import envfile  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_basic_comments_quotes_export(self):
        d = envfile.parse('# 注释\n\nA=1\nexport B = two \nC="x y"\nD=\'z\'\nE=a=b\nbad line\n')
        self.assertEqual(d, {"A": "1", "B": "two", "C": "x y", "D": "z", "E": "a=b"})

    def test_value_with_base64_chars(self):
        self.assertEqual(envfile.parse("K=ab+/cd==")["K"], "ab+/cd==")


class LoadTest(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return Path(f.name)

    def test_loads_missing_vars(self):
        p = self.write("T_A=1\nT_B=2\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sorted(envfile.load_dotenv(p)), ["T_A", "T_B"])
            self.assertEqual(os.environ["T_A"], "1")

    def test_system_env_wins(self):
        p = self.write("T_A=from_file\n")
        with mock.patch.dict(os.environ, {"T_A": "from_system"}, clear=True):
            self.assertEqual(envfile.load_dotenv(p), [])
            self.assertEqual(os.environ["T_A"], "from_system")

    def test_empty_system_value_is_filled(self):
        p = self.write("T_A=from_file\n")
        with mock.patch.dict(os.environ, {"T_A": ""}, clear=True):
            envfile.load_dotenv(p)
            self.assertEqual(os.environ["T_A"], "from_file")

    def test_blank_entry_not_written(self):
        p = self.write("T_A=\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(envfile.load_dotenv(p), [])
            self.assertNotIn("T_A", os.environ)

    def test_missing_file_is_noop(self):
        self.assertEqual(envfile.load_dotenv(Path("/不存在/.env")), [])

    def test_bom_tolerated(self):
        p = self.write("\ufeffT_A=1\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            envfile.load_dotenv(p)
            self.assertEqual(os.environ["T_A"], "1")


if __name__ == "__main__":
    unittest.main()

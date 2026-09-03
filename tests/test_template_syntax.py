from pathlib import Path
import unittest

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]


class TemplateSyntaxTests(unittest.TestCase):
    def test_all_templates_parse_as_jinja(self):
        environment = Environment()
        templates = sorted((ROOT / "templates").glob("*.html"))
        self.assertTrue(templates)
        for path in templates:
            with self.subTest(template=path.name):
                environment.parse(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

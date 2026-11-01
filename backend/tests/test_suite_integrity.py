"""Guards against tests that silently stop running.

A duplicate class name in a test module is not an error in Python — the second definition simply
rebinds the name, and every test method on the first one disappears from the suite with no warning.
That happened here: a new `ConsentTierTests` shadowed an existing one and deleted 11 assertions,
while the run still reported OK. The count went DOWN in a commit that added tests, which is the only
reason it was noticed.

That is the same failure shape the rest of this codebase is written to avoid: not a wrong answer, a
missing one, reported as success. So it gets a test of its own.

No import stubs here — these read the test files as TEXT and never import `run`.
"""
import ast
import unittest
from collections import Counter
from pathlib import Path

_TESTS = Path(__file__).resolve().parent


def _test_modules():
    return sorted(p for p in _TESTS.glob("test_*.py") if p.name != Path(__file__).name)


class NoShadowedTestsTests(unittest.TestCase):
    def test_no_module_defines_the_same_class_twice(self):
        for path in _test_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
            duplicates = [n for n, count in Counter(names).items() if count > 1]
            self.assertEqual(duplicates, [],
                             f"{path.name} redefines {duplicates}; the earlier definition's tests "
                             f"are silently dropped from the suite")

    def test_no_class_defines_the_same_test_twice(self):
        # Same failure one level down: a repeated method name inside a class overwrites the first,
        # and the assertion it carried is gone.
        for path in _test_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                methods = [f.name for f in cls.body
                           if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and f.name.startswith("test")]
                duplicates = [m for m, count in Counter(methods).items() if count > 1]
                self.assertEqual(duplicates, [],
                                 f"{path.name}:{cls.name} redefines {duplicates}")


if __name__ == "__main__":
    unittest.main()

import pathlib
import unittest

try:
    import yaml
except ImportError:  # PyYAML isn't stdlib; GitHub's runners have it
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _no_duplicates(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return loader.construct_mapping(node, deep)


if yaml is not None:

    class StrictLoader(yaml.SafeLoader):
        """PyYAML keeps the last of two equal keys; GitHub rejects the file."""

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ActionYamlTest(unittest.TestCase):
    def test_no_duplicate_keys(self):
        # A merge once left two COVERAGE_FILE keys in one step's env: valid for
        # PyYAML, rejected by GitHub. Load every action and workflow strictly.
        files = ["action.yml", "trigger/action.yml"] + sorted(
            str(p.relative_to(ROOT)) for p in (ROOT / ".github" / "workflows").glob("*.yml")
        ) + sorted(str(p.relative_to(ROOT)) for p in (ROOT / "examples").glob("*.yml"))
        for name in files:
            with self.subTest(name=name):
                yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=StrictLoader)


if __name__ == "__main__":
    unittest.main()

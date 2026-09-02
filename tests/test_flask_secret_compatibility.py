import ast
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
DEFAULT_SECRET = "booklore-flibusta-local-v20"


def secret_selection_node():
    for node in TREE.body:
        if not isinstance(node, ast.If):
            continue
        test_source = ast.get_source_segment(SOURCE, node.test) or ""
        if "OPDS_DESK_SECRET" in test_source:
            return node
    raise AssertionError("Flask secret selection was not found")


def selected_secret(environ):
    namespace = {
        "app": types.SimpleNamespace(secret_key=None),
        "os": types.SimpleNamespace(environ=dict(environ)),
    }
    node = secret_selection_node()
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"),
        namespace,
    )
    return namespace["app"].secret_key


class FlaskSecretCompatibilityTests(unittest.TestCase):
    def test_a_no_environment_values_uses_exact_default(self):
        self.assertEqual(selected_secret({}), DEFAULT_SECRET)

    def test_b_legacy_environment_value_remains_supported(self):
        self.assertEqual(
            selected_secret({"FLIBUSTA_BRIDGE_SECRET": "legacy-secret"}),
            "legacy-secret",
        )

    def test_c_neutral_environment_value_is_supported(self):
        self.assertEqual(
            selected_secret({"OPDS_DESK_SECRET": "neutral-secret"}),
            "neutral-secret",
        )

    def test_d_neutral_environment_value_has_priority(self):
        self.assertEqual(
            selected_secret(
                {
                    "OPDS_DESK_SECRET": "neutral-secret",
                    "FLIBUSTA_BRIDGE_SECRET": "legacy-secret",
                }
            ),
            "neutral-secret",
        )

    def test_e_empty_legacy_value_is_preserved(self):
        self.assertEqual(selected_secret({"FLIBUSTA_BRIDGE_SECRET": ""}), "")

    def test_f_empty_neutral_value_has_priority(self):
        self.assertEqual(
            selected_secret(
                {
                    "OPDS_DESK_SECRET": "",
                    "FLIBUSTA_BRIDGE_SECRET": "legacy-secret",
                }
            ),
            "",
        )

    def test_g_whitespace_only_values_are_not_normalized(self):
        for environment_name in (
            "OPDS_DESK_SECRET",
            "FLIBUSTA_BRIDGE_SECRET",
        ):
            with self.subTest(environment_name=environment_name):
                self.assertEqual(selected_secret({environment_name: "   "}), "   ")

    def test_h_default_literal_remains_exact(self):
        node = secret_selection_node()
        constants = {
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        }

        self.assertIn(DEFAULT_SECRET, constants)


if __name__ == "__main__":
    unittest.main()

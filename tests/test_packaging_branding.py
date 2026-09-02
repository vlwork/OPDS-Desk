import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ONEDIR_SPEC = PROJECT_ROOT / "OPDS-Desk.spec"
ONEFILE_SPEC = PROJECT_ROOT / "OPDS-Desk-OneFile.spec"
VERSION_INFO = PROJECT_ROOT / "version_info.txt"
APP_SOURCE = PROJECT_ROOT / "app.py"


def parse_file(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def calls_named(tree, name):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def keyword_value(call, name):
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    raise AssertionError(f"Keyword not found: {name}")


def analysis_entry_points(tree):
    calls = calls_named(tree, "Analysis")
    if len(calls) != 1 or not calls[0].args:
        raise AssertionError("Expected one Analysis call with an entry-point list")
    value = calls[0].args[0]
    if not isinstance(value, (ast.List, ast.Tuple)):
        raise AssertionError("Analysis entry points must be a list or tuple")
    return [
        item.value
        for item in value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def string_struct_values(tree):
    values = {}
    for call in calls_named(tree, "StringStruct"):
        if (
            len(call.args) >= 2
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[1], ast.Constant)
        ):
            values[call.args[0].value] = call.args[1].value
    return values


def fixed_file_info_versions(tree):
    version_calls = calls_named(tree, "VSVersionInfo")
    if len(version_calls) != 1:
        raise AssertionError("Expected one VSVersionInfo call")

    fixed_file_info = None
    for keyword in version_calls[0].keywords:
        if keyword.arg == "ffi":
            fixed_file_info = keyword.value
            break
    if not (
        isinstance(fixed_file_info, ast.Call)
        and isinstance(fixed_file_info.func, ast.Name)
        and fixed_file_info.func.id == "FixedFileInfo"
    ):
        raise AssertionError("VSVersionInfo ffi must be a FixedFileInfo call")

    versions = {}
    for keyword in fixed_file_info.keywords:
        if keyword.arg in {"filevers", "prodvers"}:
            versions[keyword.arg] = ast.literal_eval(keyword.value)
    return versions


def assignment_value(tree, name):
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant):
                return node.value.value
            return node.value
    raise AssertionError(f"Assignment not found: {name}")


class PackagingBrandingTests(unittest.TestCase):
    def test_a_spec_files_use_new_names(self):
        self.assertTrue(ONEDIR_SPEC.is_file())
        self.assertTrue(ONEFILE_SPEC.is_file())
        self.assertFalse((PROJECT_ROOT / "FlibustaBridge.spec").exists())
        self.assertFalse((PROJECT_ROOT / "FlibustaBridge-OneFile.spec").exists())

    def test_b_onedir_identity_and_topology(self):
        tree = parse_file(ONEDIR_SPEC)
        exe_calls = calls_named(tree, "EXE")
        collect_calls = calls_named(tree, "COLLECT")

        self.assertEqual(len(exe_calls), 1)
        self.assertEqual(len(collect_calls), 1)
        self.assertEqual(keyword_value(exe_calls[0], "name"), "OPDS-Desk")
        self.assertEqual(keyword_value(collect_calls[0], "name"), "OPDS-Desk")

    def test_c_onefile_identity_and_topology(self):
        tree = parse_file(ONEFILE_SPEC)
        exe_calls = calls_named(tree, "EXE")

        self.assertEqual(len(exe_calls), 1)
        self.assertEqual(calls_named(tree, "COLLECT"), [])
        self.assertEqual(keyword_value(exe_calls[0], "name"), "OPDS-Desk")

    def test_d_specs_keep_entry_point_and_version_resource(self):
        for path in (ONEDIR_SPEC, ONEFILE_SPEC):
            with self.subTest(spec=path.name):
                tree = parse_file(path)
                self.assertEqual(analysis_entry_points(tree), ["app.py"])
                exe_calls = calls_named(tree, "EXE")
                self.assertEqual(len(exe_calls), 1)
                self.assertEqual(
                    keyword_value(exe_calls[0], "version"),
                    "version_info.txt",
                )

    def test_e_windows_version_metadata_uses_opds_desk(self):
        tree = parse_file(VERSION_INFO)
        values = string_struct_values(tree)
        fixed_versions = fixed_file_info_versions(tree)
        self.assertEqual(values["FileDescription"], "OPDS Desk")
        self.assertEqual(values["InternalName"], "OPDS-Desk")
        self.assertEqual(values["OriginalFilename"], "OPDS-Desk.exe")
        self.assertEqual(values["ProductName"], "OPDS Desk")
        self.assertEqual(values["FileVersion"], "1.0.0.0")
        self.assertEqual(values["ProductVersion"], "1.0.0")
        self.assertEqual(fixed_versions["filevers"], (1, 0, 0, 0))
        self.assertEqual(fixed_versions["prodvers"], (1, 0, 0, 0))

    def test_f_app_and_product_versions_are_consistent(self):
        app_version = assignment_value(parse_file(APP_SOURCE), "APP_VERSION")
        version_values = string_struct_values(parse_file(VERSION_INFO))

        self.assertEqual(app_version, "1.0.0")
        self.assertEqual(version_values["ProductVersion"], app_version)

    def test_g_persisted_app_data_directory_has_neutral_and_legacy_roots(self):
        tree = parse_file(APP_SOURCE)
        legacy_value = assignment_value(tree, "LEGACY_APP_DATA_DIR")
        neutral_value = assignment_value(tree, "NEUTRAL_APP_DATA_DIR")
        app_data_value = assignment_value(tree, "APP_DATA_DIR")
        legacy_strings = {
            node.value
            for node in ast.walk(legacy_value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        neutral_strings = {
            node.value
            for node in ast.walk(neutral_value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertIn("FlibustaBridge", legacy_strings)
        self.assertIn("OPDSDesk", neutral_strings)
        self.assertIsInstance(app_data_value, ast.Call)
        self.assertIsInstance(app_data_value.func, ast.Name)
        self.assertEqual(app_data_value.func.id, "resolve_app_data_dir")
        self.assertEqual(
            [argument.id for argument in app_data_value.args],
            ["NEUTRAL_APP_DATA_DIR", "LEGACY_APP_DATA_DIR"],
        )


if __name__ == "__main__":
    unittest.main()

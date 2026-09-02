import ast
import os
import tempfile
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
PATH_CONSTANTS = {
    "APP_DATA_BASE_DIR",
    "LEGACY_APP_DATA_DIR",
    "NEUTRAL_APP_DATA_DIR",
    "APP_DATA_DIR",
    "CONFIG_FILE",
    "JOB_STATE_FILE",
    "QUEUE_DB_FILE",
    "DEFAULT_DESTINATION",
}
PATH_HELPERS = {"app_data_dir_has_state", "resolve_app_data_dir"}


def assigned_names(node):
    if not isinstance(node, ast.Assign):
        return set()
    return {
        target.id
        for target in node.targets
        if isinstance(target, ast.Name)
    }


class PathFacade:
    def __init__(self, expanded_home):
        self.expanded_home = expanded_home

    def expanduser(self, value):
        if value != "~":
            raise AssertionError(f"Unexpected expanduser value: {value!r}")
        return self.expanded_home

    join = staticmethod(os.path.join)
    isfile = staticmethod(os.path.isfile)
    isdir = staticmethod(os.path.isdir)


def load_path_module(environ=None, expanded_home="C:\\Users\\fallback"):
    body = []
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name in PATH_HELPERS:
            body.append(node)
        elif assigned_names(node) & PATH_CONSTANTS:
            body.append(node)

    module = types.ModuleType("isolated_app_data_directory_test")
    module.os = types.SimpleNamespace(
        environ=dict(environ or {}),
        path=PathFacade(expanded_home),
    )
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "app.py", "exec"),
        module.__dict__,
    )
    return module


def create_state(root, artifact):
    root.mkdir(parents=True, exist_ok=True)
    path = root / artifact
    if artifact == "Library":
        path.mkdir()
    else:
        path.write_text(artifact, encoding="utf-8")


def top_level_assignment_index(name):
    return next(
        index
        for index, node in enumerate(TREE.body)
        if name in assigned_names(node)
    )


def top_level_call_index(name):
    for index, node in enumerate(TREE.body):
        value = None
        if isinstance(node, ast.Expr):
            value = node.value
        elif isinstance(node, ast.Assign):
            value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == name
        ):
            return index
    raise AssertionError(f"Top-level call not found: {name}")


class AppDataDirectoryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.neutral = base / "OPDSDesk"
        self.legacy = base / "FlibustaBridge"
        self.module = load_path_module()

    def resolve(self):
        return Path(
            self.module.resolve_app_data_dir(
                str(self.neutral),
                str(self.legacy),
            )
        )

    def test_a_no_persisted_state_selects_neutral(self):
        self.assertEqual(self.resolve(), self.neutral)

    def test_b_legacy_config_selects_legacy(self):
        create_state(self.legacy, "config.json")
        self.assertEqual(self.resolve(), self.legacy)

    def test_c_legacy_queue_database_selects_legacy(self):
        create_state(self.legacy, "queue.db")
        self.assertEqual(self.resolve(), self.legacy)

    def test_d_legacy_jobs_selects_legacy(self):
        create_state(self.legacy, "jobs.json")
        self.assertEqual(self.resolve(), self.legacy)

    def test_e_legacy_library_selects_legacy(self):
        create_state(self.legacy, "Library")
        self.assertEqual(self.resolve(), self.legacy)

    def test_f_neutral_state_selects_neutral(self):
        create_state(self.neutral, "config.json")
        self.assertEqual(self.resolve(), self.neutral)

    def test_g_neutral_has_priority_when_both_roots_have_state(self):
        create_state(self.neutral, "jobs.json")
        create_state(self.legacy, "queue.db")
        self.assertEqual(self.resolve(), self.neutral)

    def test_h_empty_neutral_directory_does_not_hide_legacy_state(self):
        self.neutral.mkdir()
        create_state(self.legacy, "config.json")
        self.assertEqual(self.resolve(), self.legacy)

    def test_i_config_temp_alone_is_not_meaningful_state(self):
        create_state(self.neutral, "config.json.tmp")
        create_state(self.legacy, "config.json")
        self.assertEqual(self.resolve(), self.legacy)

    def test_j_sqlite_sidecars_alone_are_not_meaningful_state(self):
        create_state(self.neutral, "queue.db-wal")
        create_state(self.neutral, "queue.db-shm")
        create_state(self.legacy, "jobs.json")
        self.assertEqual(self.resolve(), self.legacy)

    def test_k_legacy_transient_files_alone_select_neutral(self):
        for artifact in (
            "config.json.tmp",
            "jobs.json.tmp",
            "queue.db-wal",
            "queue.db-shm",
            "download.part",
            ".opds-desk-write-test-token.tmp",
        ):
            create_state(self.legacy, artifact)
        self.assertEqual(self.resolve(), self.neutral)

    def test_l_resolver_does_not_copy_move_or_delete_contents(self):
        create_state(self.neutral, "jobs.json")
        create_state(self.legacy, "config.json")
        before = {
            path.relative_to(Path(self.temp_dir.name)): path.read_bytes()
            for path in Path(self.temp_dir.name).rglob("*")
            if path.is_file()
        }

        self.assertEqual(self.resolve(), self.neutral)

        after = {
            path.relative_to(Path(self.temp_dir.name)): path.read_bytes()
            for path in Path(self.temp_dir.name).rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_m_localappdata_value_is_used_bit_for_bit(self):
        base = "  X:\\Custom App Data  "
        module = load_path_module(
            {"LOCALAPPDATA": base},
            expanded_home="C:\\Unused",
        )

        self.assertEqual(module.APP_DATA_BASE_DIR, base)
        self.assertEqual(
            module.NEUTRAL_APP_DATA_DIR,
            os.path.join(base, "OPDSDesk"),
        )

    def test_n_missing_localappdata_uses_expanduser_fallback(self):
        fallback = "C:\\Users\\fallback"
        module = load_path_module({}, expanded_home=fallback)

        self.assertEqual(module.APP_DATA_BASE_DIR, fallback)
        self.assertEqual(
            module.LEGACY_APP_DATA_DIR,
            os.path.join(fallback, "FlibustaBridge"),
        )

    def test_o_root_selection_precedes_derived_paths_and_persisted_reads(self):
        app_data_index = top_level_assignment_index("APP_DATA_DIR")

        for name in (
            "CONFIG_FILE",
            "JOB_STATE_FILE",
            "QUEUE_DB_FILE",
            "DEFAULT_DESTINATION",
        ):
            with self.subTest(assignment=name):
                self.assertLess(app_data_index, top_level_assignment_index(name))
        for name in ("load_app_config", "load_jobs", "init_queue_db"):
            with self.subTest(call=name):
                self.assertLess(app_data_index, top_level_call_index(name))


if __name__ == "__main__":
    unittest.main()

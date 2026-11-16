"""Light install regressions: temporary files and a Python/pip stub, no downloads/models."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PKG = Path(__file__).resolve().parents[1]


class LightManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tx light manifest ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("light_install_selftest", PKG / "selftest.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.__file__ = str(self.root / "selftest.py")
        self.exporter = self.root / "export_light_models.py"
        self.exporter.write_text("# fixture exporter\n")
        self.source_manifest = self.root / "models.tsv"
        self.source_manifest.write_text(
            "modelscope\tasr\tmodel.pt\t10\t" + "0" * 64 + "\thttps://fixture/asr\n" +
            "modelscope\tvad\tam.mvn\t8040\t" + "2" * 64 + "\thttps://fixture/vad-ms\n" +
            "huggingface\tvad\tam.mvn\t8033\t" + "3" * 64 + "\thttps://fixture/vad-hf\n")
        self.model_dir = self.root / "models-light"
        self.artifact = self.model_dir / "asr" / "model.onnx"
        self.artifact.parent.mkdir(parents=True)
        self.artifact.write_bytes(b"fixture model")
        self.local = self.model_dir / "models-light.manifest.json"
        self.trusted = self.root / "trusted.json"
        self.manifest = {
            "schema": 1, "pipeline_version": "light-cif-v1",
            "exporter_sha256": self.module.file_sha256(self.exporter),
            "source_manifest_sha256": self.module.file_sha256(self.source_manifest),
            "dependencies": {"fixture-runtime": "1.0"},
            "sources": [{"component": "asr", "path": "/old-machine/models/asr/model.pt",
                         "size": 10, "sha256": "0" * 64},
                        {"component": "vad", "path": "/old-machine/models/vad/am.mvn",
                         "size": 8040, "sha256": "2" * 64},
                        {"component": "punc", "path": "/old-machine/light-source/punc/model.pt",
                         "size": 20, "sha256": "4" * 64}],
            "files": [{"path": "asr/model.onnx", "size": self.artifact.stat().st_size,
                       "sha256": self.module.file_sha256(self.artifact)}],
        }
        self.write_manifests()

    def write_manifests(self, actual=None, trusted=None):
        self.local.write_text(json.dumps(actual or self.manifest), encoding="utf-8")
        self.trusted.write_text(json.dumps(trusted or self.manifest), encoding="utf-8")

    def verify(self, runtime_version="1.0"):
        with mock.patch.object(self.module.importlib.metadata, "version", return_value=runtime_version), \
                contextlib.redirect_stdout(io.StringIO()):
            return self.module.verify_light_models(self.model_dir, self.trusted)

    def assert_rejected(self):
        with self.assertRaises(SystemExit) as error:
            self.verify()
        self.assertEqual(error.exception.code, 1)

    def test_valid_export_accepts_different_source_root(self):
        actual = copy.deepcopy(self.manifest)
        actual["sources"][0]["path"] = "/another-machine/models/asr/model.pt"
        self.write_manifests(actual=actual)
        self.assertEqual(len(self.verify()), 64)

    def test_corrupt_artifact_rejected_even_with_ok_marker(self):
        (self.model_dir / ".ok").write_text("ok")
        self.artifact.write_bytes(b"corrupt model")
        self.assert_rejected()

    def test_changed_source_or_exporter_rejected(self):
        actual = copy.deepcopy(self.manifest)
        actual["sources"][0]["sha256"] = "1" * 64
        self.write_manifests(actual=actual)
        self.assert_rejected()
        self.write_manifests()
        self.exporter.write_text("# changed exporter\n")
        self.assert_rejected()

    def test_known_alternative_source_allowed_but_unlisted_hash_rejected(self):
        actual = copy.deepcopy(self.manifest)
        actual["sources"][1].update(size=8033, sha256="3" * 64)
        self.write_manifests(actual=actual)
        self.assertEqual(len(self.verify()), 64)
        actual["sources"][1]["sha256"] = "5" * 64
        self.write_manifests(actual=actual)
        self.assert_rejected()
        actual = copy.deepcopy(self.manifest)
        actual["sources"][2]["sha256"] = "3" * 64
        self.write_manifests(actual=actual)
        self.assert_rejected()

    def test_source_manifest_hash_matches_current_actual_and_trusted(self):
        changed = copy.deepcopy(self.manifest)
        changed["source_manifest_sha256"] = "9" * 64
        self.write_manifests(actual=changed)
        self.assert_rejected()
        self.write_manifests(trusted=changed)
        self.assert_rejected()
        self.write_manifests()
        self.source_manifest.write_text(self.source_manifest.read_text() + "# changed\n")
        self.assert_rejected()

    def test_source_keys_must_match_without_duplicates(self):
        for sources in (self.manifest["sources"][:-1], self.manifest["sources"] * 2):
            with self.subTest(sources=sources):
                actual = copy.deepcopy(self.manifest)
                actual["sources"] = sources
                self.write_manifests(actual=actual)
                self.assert_rejected()

    def test_changed_runtime_dependency_rejected(self):
        with self.assertRaises(SystemExit) as error:
            self.verify(runtime_version="2.0")
        self.assertEqual(error.exception.code, 1)

    def test_missing_and_duplicate_artifact_entries_rejected(self):
        for files in ([], self.manifest["files"] * 2):
            with self.subTest(files=files):
                actual = copy.deepcopy(self.manifest)
                actual["files"] = files
                self.write_manifests(actual=actual)
                self.assert_rejected()

    def test_paths_cannot_escape_model_directory(self):
        outside = self.root / "outside.onnx"
        outside.write_bytes(self.artifact.read_bytes())
        for path in ("../outside.onnx", str(outside)):
            with self.subTest(path=path):
                manifest = copy.deepcopy(self.manifest)
                manifest["files"][0]["path"] = path
                self.write_manifests(actual=manifest, trusted=manifest)
                self.assert_rejected()
        self.write_manifests()
        self.artifact.unlink()
        self.artifact.symlink_to(outside)
        self.assert_rejected()


class LightShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tx light shell ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TX_")}
        self.env.update(TX_APP_DIR=str(self.app), TX_USER_DIR=str(self.root / "user"))
        self.build_lock = self.root / "build.txt"
        self.light_lock = self.root / "light.txt"
        self.build_lock.write_text("fixture build lock\n")
        self.light_lock.write_text("fixture light lock\n")
        self.env.update(TX_BUILD_LOCK_FILE=str(self.build_lock), TX_LIGHT_LOCK_FILE=str(self.light_lock))
        self.calls = self.root / "python-calls.jsonl"
        self.env.update(FIXTURE_CALLS=str(self.calls), FIXTURE_BUILD_READY=str(self.root / "build-ready"))

    def run_shell(self, code, args=(), installer=False):
        if installer:
            definitions = (PKG / "install-light.sh").read_text().split('\nmain_light "$@"', 1)[0]
            definitions = definitions.replace(
                '. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"',
                '. ' + shlex.quote(str(PKG / "common.sh")))
        else:
            definitions = '. ' + shlex.quote(str(PKG / "common.sh"))
        return subprocess.run(["/bin/bash", "-c", definitions + "\n" + code, "fixture", *args],
                              capture_output=True, text=True, env=self.env, timeout=10)

    def make_python_stub(self):
        python = self.app / "python" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        helper = self.root / "python_stub.py"
        self.env["FIXTURE_PYTHON_STUB"] = str(python)
        python.write_text("#!/bin/bash\nexec " + shlex.quote(sys.executable) + " " +
                          shlex.quote(str(helper)) + ' "$@"\n')
        helper.write_text('''
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ["FIXTURE_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["-m", "venv"]:
    target = pathlib.Path(args[2]) / "bin" / "python"
    target.parent.mkdir(parents=True)
    shutil.copy2(os.environ["FIXTURE_PYTHON_STUB"], target)
elif args[:3] == ["-m", "pip", "install"]:
    assert "--require-hashes" in args and "--no-deps" in args
    lock = args[args.index("-r") + 1]
    ready = pathlib.Path(os.environ["FIXTURE_BUILD_READY"])
    if lock == os.environ["TX_BUILD_LOCK_FILE"]:
        ready.write_text("ready")
    elif lock == os.environ["TX_LIGHT_LOCK_FILE"]:
        if not ready.exists():
            sys.exit("build backend is missing in fresh Python 3.12 venv")
        assert "--no-build-isolation" in args
    else:
        sys.exit("unexpected lock")
elif args[0] != "-c":
    sys.exit("unexpected Python operation")
''')
        python.chmod(0o755)

    def test_engine_selection_uses_independent_paths(self):
        for args, engine, suffix in (((), "light", "-light"),
                                     (("--engine", "original"), "original", ""),
                                     (("--engine", "light"), "light", "-light"),
                                     (("files", "录音 文件.wav", "--engine=light"), "light", "-light")):
            with self.subTest(args=args):
                result = self.run_shell('printf "%s\\n" "$ENGINE" "$VENV" "$MODEL_DIR" "$OUT_DIR"', args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [engine, str(self.app / ("venv" + suffix)),
                                  str(self.app / ("models" + suffix)), str(self.root / "user" / "转写结果")])

    def test_light_program_copy_includes_batch_export_helper(self):
        package = self.root / "fixture package"
        package.mkdir()
        (package / "batch_export.py").write_text("# batch export fixture\n")
        result = self.run_shell('PKG_DIR=' + shlex.quote(str(package)) + '''
mkdir -p "$APP_DIR"
copy_light_program
''', installer=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.app / "app" / "batch_export.py").read_text(), "# batch export fixture\n")

    def test_fresh_light_env_installs_build_tools_first_and_keeps_original(self):
        self.make_python_stub()
        originals = [self.app / "venv" / "sentinel", self.app / "models" / "sentinel"]
        for sentinel in originals:
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("original remains intact")
        result = self.run_shell("install_light_deps", installer=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        installs = [call for call in calls if call[:3] == ["-m", "pip", "install"]]
        self.assertEqual([call[call.index("-r") + 1] for call in installs],
                         [str(self.build_lock), str(self.light_lock)])
        smoke = next(call[1] for call in calls if call[0] == "-c")
        for module in ("torch", "funasr", "funasr_onnx", "onnx", "onnxruntime"):
            self.assertIn(module, smoke)
        for sentinel in originals:
            self.assertEqual(sentinel.read_text(), "original remains intact")
        self.assertTrue((self.app / "venv-light" / ".ok").is_file())

    def test_build_lock_change_invalidates_light_install_marker(self):
        self.make_python_stub()
        result = self.run_shell("install_light_deps", installer=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.env["TX_LIGHT_MANIFEST"] = str(self.root / "trusted.json")
        Path(self.env["TX_LIGHT_MANIFEST"]).write_text("{}")
        setup = '''
mkdir -p "$MODEL_DIR"
printf '{}' > "$MODEL_DIR/models-light.manifest.json"
echo "$APP_VERSION" > "$INSTALLED_MARK"
printf '%s:%s\\n' "$APP_VERSION" "$(sha256_of "$MODEL_DIR/models-light.manifest.json")" > "$SELFTEST_MARK"
is_installed
'''
        self.assertEqual(self.run_shell(setup, ("--engine", "light")).returncode, 0)
        self.build_lock.write_text("updated build lock\n")
        self.assertEqual(self.run_shell("is_installed", ("--engine", "light")).returncode, 1)
        old = (self.app / "venv-light" / ".ok").read_text()
        result = self.run_shell("install_light_deps", installer=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(old, (self.app / "venv-light" / ".ok").read_text())

    def test_fresh_light_export_checks_space_before_work(self):
        result = self.run_shell('''
verify_light_install() { return 1; }
disk_available_mb() { echo 100; }
check_light_disk
''', installer=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("磁盘空间不够", result.stdout)
        self.assertIn("轻量安装和模型导出", result.stdout)
        self.assertFalse((self.app / "venv-light").exists())

    def test_verified_existing_light_install_only_needs_small_reserve(self):
        self.make_python_stub()
        result = self.run_shell('''
install_light_deps
verify_light_install() { return 0; }
disk_available_mb() { echo 600; }
check_light_disk
''', installer=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

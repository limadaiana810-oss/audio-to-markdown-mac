"""Shell entry-point regressions. Uses temporary directories; no models, downloads or UI."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

PKG = Path(__file__).resolve().parents[1]


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tx shell tests ")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, TX_APP_DIR=str(self.root / "app"),
                        TX_USER_DIR=str(self.root / "user"), TX_ENGINE="original")

    def run_functions(self, script, code, stdin=""):
        text = (PKG / script).read_text()
        marker = '\nif ! is_installed; then' if script == "launcher.sh" else '\nmain "$@"'
        definitions = text.split(marker, 1)[0]
        # Source common.sh at its real location; all mutable paths use TX_* overrides.
        definitions = definitions.replace(
            '. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"',
            '. ' + shlex.quote(str(PKG / "common.sh")))
        return subprocess.run(["/bin/bash", "-c", definitions + "\n" + code],
                              input=stdin, text=True, capture_output=True, env=self.env, timeout=10)

    def test_drag_prompt_is_visible_but_not_a_path(self):
        p = self.run_functions("launcher.sh", 'VPY=' + shlex.quote(sys.executable) + '\nask_drag',
                               shlex.quote('/tmp/录音 文件.m4a') + '\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, '/tmp/录音 文件.m4a\n')
        self.assertIn('请把录音文件', p.stderr)

    def test_fallback_passes_only_selected_paths(self):
        p = self.run_functions("launcher.sh", 'VPY=' + shlex.quote(sys.executable) + '''
choose() { return 2; }
run_tx() { printf 'ARG:%s\\n' "$@"; }
collect_and_run file
''', shlex.quote('/tmp/录音 文件.m4a') + '\n')
        self.assertEqual(p.returncode, 0, p.stderr)
        args = [s for s in p.stdout.splitlines() if s.startswith('ARG:')]
        self.assertEqual(args, ['ARG:run', 'ARG:/tmp/录音 文件.m4a'])

    def test_cancel_does_not_start_a_job(self):
        p = self.run_functions("launcher.sh", '''
choose() { return 1; }
run_tx() { echo UNEXPECTED_JOB; }
collect_and_run file
''')
        self.assertEqual(p.returncode, 0)
        self.assertNotIn('UNEXPECTED_JOB', p.stdout)
        self.assertIn('已取消', p.stdout)

    def test_redo_cancel_does_not_start_a_job(self):
        p = self.run_functions("launcher.sh", '''
choose() { return 1; }
run_tx() { echo UNEXPECTED_JOB; }
menu
''', '8\n0\n')
        self.assertEqual(p.returncode, 0)
        self.assertNotIn('UNEXPECTED_JOB', p.stdout)
        self.assertIn('已取消', p.stdout)

    def test_redo_selection_passes_force(self):
        p = self.run_functions("launcher.sh", '''
choose() { printf '%s\\n' '/tmp/录音 文件.m4a'; }
run_tx() { printf 'ARG:%s\\n' "$@"; }
menu
''', '8\n0\n')
        self.assertEqual(p.returncode, 0)
        args = [s for s in p.stdout.splitlines() if s.startswith('ARG:')]
        self.assertEqual(args, ['ARG:run', 'ARG:/tmp/录音 文件.m4a', 'ARG:--force'])

    def test_watch_prompt_keeps_path_before_chinese_punctuation(self):
        p = self.run_functions("launcher.sh", '''
open_dir() { :; }
run_tx() { :; }
menu
''', '3\n0\n')
        self.assertEqual(p.returncode, 0)
        self.assertIn(str(self.root / 'user' / '待转写录音') + '（每分钟', p.stdout)

    def test_interrupted_job_reports_stop_and_preserves_exit_status(self):
        p = self.run_functions("launcher.sh", '''
keep_awake() { return 130; }
run_tx run /tmp/test.wav
''')
        self.assertEqual(p.returncode, 130)
        self.assertIn('已停止', p.stdout)
        self.assertNotIn('有文件没转成功', p.stdout)

    def test_failed_job_keeps_exit_code_before_chinese_punctuation(self):
        p = self.run_functions("launcher.sh", '''
keep_awake() { return 7; }
run_tx run /tmp/test.wav
''')
        self.assertEqual(p.returncode, 7)
        self.assertIn('退出码 7。', p.stdout)

    def test_finished_batch_displays_its_actual_combined_markdown(self):
        merged = self.root / "user" / "转写结果" / "合并全文" / "本批 录音.md"
        merged.parent.mkdir(parents=True)
        merged.write_text("本批结果")
        p = self.run_functions("launcher.sh", 'FIXTURE_MERGED=' + shlex.quote(str(merged)) + '''
keep_awake() { printf '13:00:00 合并全文：%s\n' "$FIXTURE_MERGED"; return 0; }
run_tx run /tmp/a.wav /tmp/b.wav
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('本次合并全文：' + str(merged), p.stdout)

    def test_single_file_does_not_advertise_old_combined_markdown(self):
        old = self.root / "user" / "转写结果" / "合并全文.md"
        old.parent.mkdir(parents=True)
        old.write_text("上一批结果")
        p = self.run_functions("launcher.sh", '''
keep_awake() { return 0; }
run_tx run /tmp/single.wav
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn('本次合并全文：', p.stdout)

    def test_partial_batch_displays_partial_path_and_keeps_failure(self):
        partial = self.root / "部分结果.md"
        partial.write_text("部分结果")
        p = self.run_functions("launcher.sh", 'FIXTURE_PARTIAL=' + shlex.quote(str(partial)) + '''
keep_awake() { printf '13:00:00 部分结果：%s\n' "$FIXTURE_PARTIAL"; return 2; }
run_tx run /tmp/a.wav /tmp/missing.wav
''')
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn('本次部分结果（仍有录音未完成）：' + str(partial), p.stdout)
        self.assertNotIn('本次合并全文：', p.stdout)

    def test_same_second_runs_have_separate_logs_without_stale_merge_prompt(self):
        merged = self.root / "本批.md"
        merged.write_text("本批结果")
        p = self.run_functions("launcher.sh", 'FIXTURE_MERGED=' + shlex.quote(str(merged)) + '''
date() { printf '20261112-130000\n'; }
keep_awake() { printf '13:00:00 合并全文：%s\n' "$FIXTURE_MERGED"; return 0; }
run_tx run /tmp/a.wav /tmp/b.wav
keep_awake() { return 0; }
run_tx run /tmp/single.wav
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(1, p.stdout.count('本次合并全文：'))
        logs = list((self.root / "user" / "日志").glob('转写_original_*'))
        self.assertEqual(2, len(logs))

    def test_log_write_failure_is_not_reported_as_success(self):
        p = self.run_functions("launcher.sh", '''
keep_awake() { printf 'worker finished\\n'; return 0; }
tee() { cat; return 1; }
run_tx run /tmp/test.wav
''')
        self.assertEqual(p.returncode, 74, p.stdout + p.stderr)
        self.assertIn('日志保存失败', p.stdout)
        self.assertNotIn('✓ 完成', p.stdout)

    def test_log_failure_preserves_transcription_failure(self):
        p = self.run_functions("launcher.sh", '''
keep_awake() { return 7; }
tee() { cat; return 1; }
run_tx run /tmp/test.wav
''')
        self.assertEqual(p.returncode, 7, p.stdout + p.stderr)
        self.assertIn('日志保存失败', p.stdout)

    def test_unwritable_log_directory_does_not_start_transcription(self):
        blocked = self.root / "not a directory"
        blocked.write_text("fixture")
        p = self.run_functions("launcher.sh", 'LOG_DIR=' + shlex.quote(str(blocked)) + '''
keep_awake() { echo UNEXPECTED_JOB; return 0; }
run_tx run /tmp/test.wav
''')
        self.assertEqual(p.returncode, 74, p.stdout + p.stderr)
        self.assertNotIn('UNEXPECTED_JOB', p.stdout)

    def test_download_commit_failure_is_propagated_and_part_retained(self):
        part = self.root / "model.part"
        part.write_bytes(b"verified fixture")
        destination = self.root / "model"
        p = self.run_functions("common.sh", '''
curl() { return 0; }
mv() { return 1; }
download_verified https://fixture.invalid/model ''' + shlex.quote(str(destination)) +
                               ' "" "$(sha256_of ' + shlex.quote(str(part)) + ')"')
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(part.is_file())
        self.assertFalse(destination.exists())

    def test_install_space_check_uses_app_volume_and_blocks_low_space(self):
        app_parent = self.root / "external volume"
        app_parent.mkdir()
        self.env["TX_APP_DIR"] = str(app_parent / "app")
        capture = self.root / "df-path"
        self.env["FIXTURE_DF_CAPTURE"] = str(capture)
        p = self.run_functions("install.sh", '''
df() { printf '%s' "$2" > "$FIXTURE_DF_CAPTURE"; printf 'Filesystem blocks used available capacity mounted\\nfixture 1000 999 1 99%% /fixture\\n'; }
check_disk
''')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(capture.read_text(), str(app_parent))
        self.assertIn('磁盘空间不够', p.stdout)

    def test_light_bootstrap_installs_original_resources_then_light(self):
        package = self.root / "fixture package"
        package.mkdir()
        (package / "install.sh").write_text('#!/bin/bash\nprintf "bootstrap:%s\\n" "$*"\n')
        (package / "install-light.sh").write_text('#!/bin/bash\nprintf "light:%s\\n" "$*"\n')
        p = self.run_functions("install.sh", 'PKG_DIR=' + shlex.quote(str(package)) + '''
select_engine light
main --engine light
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('bootstrap:--engine original', p.stdout)
        self.assertIn('light:--engine light', p.stdout)
        self.assertLess(p.stdout.index('bootstrap:'), p.stdout.index('light:'))

    def test_failed_bootstrap_does_not_continue_or_silently_use_original(self):
        package = self.root / "fixture package"
        package.mkdir()
        (package / "install.sh").write_text('#!/bin/bash\nexit 17\n')
        (package / "install-light.sh").write_text('#!/bin/bash\necho UNEXPECTED_LIGHT_INSTALL\n')
        p = self.run_functions("install.sh", 'PKG_DIR=' + shlex.quote(str(package)) + '''
select_engine light
main --engine light
''')
        self.assertEqual(p.returncode, 17, p.stdout + p.stderr)
        self.assertNotIn('UNEXPECTED_LIGHT_INSTALL', p.stdout)

    def test_program_copy_includes_new_python_helpers(self):
        package = self.root / "fixture package"
        package.mkdir()
        (package / "new_helper.py").write_text("# helper fixture\n")
        p = self.run_functions("install.sh", 'PKG_DIR=' + shlex.quote(str(package)) + '''
mkdir -p "$APP_DIR"
copy_program
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.root / "app" / "app" / "new_helper.py").read_text(), "# helper fixture\n")

    def test_install_completion_preserves_all_output_paths(self):
        # Exercise main's final messages, replacing every installation operation.
        # Only the real lock and installed.ok marker are written in our temp directory.
        p = self.run_functions("install.sh", '''
start_log() { :; }
check_platform() { :; }
check_disk() { :; }
copy_program() { :; }
prepare_user_dir() { :; }
install_python() { :; }
install_deps() { :; }
install_models() { :; }
selftest() { :; }
main
''')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('安装完成', p.stdout)
        self.assertIn('放录音：' + str(self.root / 'user' / '待转写录音') + '（', p.stdout)
        self.assertIn('放声纹：' + str(self.root / 'user' / '声纹样本') + '（', p.stdout)

    def test_install_marker_failure_does_not_report_completion(self):
        (self.root / "app" / "installed.ok").mkdir(parents=True)
        p = self.run_functions("install.sh", '''
start_log() { :; }
check_platform() { :; }
check_disk() { :; }
copy_program() { :; }
prepare_user_dir() { :; }
install_python() { :; }
install_deps() { :; }
install_models() { :; }
selftest() { :; }
main
''')
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn('安装完成', p.stdout)
        self.assertIn('无法保存安装状态', p.stdout)

    def test_active_installer_lock_survives_rejected_second_installer(self):
        lock = self.root / 'app' / '.install.lock'
        lock.mkdir(parents=True)
        (lock / 'pid').write_text(str(os.getpid()))
        p = self.run_functions('install.sh', 'trap cleanup EXIT\nacquire_lock')
        self.assertEqual(p.returncode, 1)
        self.assertIn('另一个安装窗口正在运行', p.stdout)
        self.assertIn(f'进程 {os.getpid()}）', p.stdout)
        self.assertTrue(lock.is_dir(), 'rejected installer deleted the active lock')
        self.assertEqual((lock / 'pid').read_text(), str(os.getpid()))

    def test_cleanup_before_acquisition_does_not_touch_existing_lock(self):
        lock = self.root / 'app' / '.install.lock'
        lock.mkdir(parents=True)
        (lock / 'pid').write_text(str(os.getpid()))
        p = self.run_functions('install.sh', 'trap cleanup EXIT\nexit 1')
        self.assertEqual(p.returncode, 1)
        self.assertTrue(lock.is_dir())

    def test_owned_lock_is_cleaned_on_exit(self):
        p = self.run_functions('install.sh', 'trap cleanup EXIT\nacquire_lock')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((self.root / 'app' / '.install.lock').exists())

    def test_cleanup_does_not_remove_replaced_owner(self):
        p = self.run_functions('install.sh', 'trap cleanup EXIT\nacquire_lock\nprintf "%s" ' +
                               str(os.getpid()) + ' > "$LOCK_DIR/pid"')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.root / 'app' / '.install.lock').is_dir())


if __name__ == '__main__':
    unittest.main()

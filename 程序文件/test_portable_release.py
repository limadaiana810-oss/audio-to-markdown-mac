"""Real-model test of a relocated bundle with network and installed dependencies denied."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cancel', action='store_true', help='Stop after the first real worker is assigned')
    parser.add_argument('--metadata-change', action='store_true', help='Change only recording xattrs during inference')
    args = parser.parse_args()
    original = args.app.resolve()
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Use a fresh output directory for an empty-cache test')
    output.mkdir(parents=True)
    app = output / '同事安装 测试' / '录音转写.app'
    app.parent.mkdir()
    shutil.copytree(original, app, symlinks=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    resources = app / 'Contents/Resources'
    inputs = output / '中文录音'
    inputs.mkdir()
    first = inputs / '会议_20261112_100000.m4a'
    second = inputs / '会议_20261112_110000.wav'
    shutil.copy2(args.sample, first)
    subprocess.run([str(resources / 'runtime/bin/ffmpeg'), '-y', '-loglevel', 'error', '-i', str(first),
                    '-acodec', 'pcm_s16le', str(second)], check=True)
    app_dir, user_dir = output / '全新程序状态', output / '全新用户结果'
    denied = [str(Path.home() / 'Library/Application Support/录音转写工具'),
              str(Path.home() / 'Library/Python'), '/opt/homebrew', '/usr/local',
              str(original), str(Path(__file__).parent.resolve())]
    profile = '(version 1)\n(allow default)\n(deny network*)\n'
    profile += '\n'.join('(deny file-read* (subpath ' + json.dumps(p) + '))' for p in denied) + '\n'
    policy = output / 'portable.sb'
    policy.write_text(profile)
    environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'zh_CN.UTF-8',
                   'TX_APP_DIR': str(app_dir), 'TX_USER_DIR': str(user_dir),
                   'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    command = ['/usr/bin/sandbox-exec', '-f', str(policy), '/bin/bash',
               str(resources / '程序文件/gui_runner.sh'), str(second), str(first)]
    started = time.monotonic()
    metadata_changes = []
    if args.cancel or args.metadata_change:
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        stdout, stderr = [], []
        stopped = []
        def read_events():
            for line in process.stdout:
                stdout.append(line)
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                running = event.get('type') == 'file' and event.get('state') == 'running'
                if running and args.metadata_change and not metadata_changes:
                    for source in (first, second):
                        before = source.stat()
                        digest = hashlib.sha256(source.read_bytes()).hexdigest()
                        subprocess.run(['/usr/bin/xattr', '-w', 'com.transcriber.portable-test',
                                        'metadata-only', str(source)], check=True)
                        after = source.stat()
                        metadata_changes.append({'file': source.name,
                            'mtime_unchanged': before.st_mtime_ns == after.st_mtime_ns,
                            'ctime_changed': before.st_ctime_ns != after.st_ctime_ns,
                            'content_unchanged': digest == hashlib.sha256(source.read_bytes()).hexdigest()})
                if running and args.cancel and not stopped:
                    stopped.append(time.monotonic())
                    process.send_signal(signal.SIGTERM)
        def read_errors():
            stderr.extend(process.stderr.readlines())
        readers = [threading.Thread(target=read_events), threading.Thread(target=read_errors)]
        for reader in readers:
            reader.start()
        try:
            code = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        for reader in readers:
            reader.join(timeout=5)
        result = subprocess.CompletedProcess(command, code, ''.join(stdout), ''.join(stderr))
    else:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=240)
    (output / '运行.log').write_text(result.stdout + '\nSTDERR\n' + result.stderr, encoding='utf-8')
    if args.cancel:
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        assert result.returncode == 130, result.returncode
        assert any(event.get('type') == 'cancelled' for event in events)
        assert not any(event.get('type') == 'complete' for event in events)
        assert not (user_dir / '转写结果/合并全文.md').exists()
        # Inspect from the unsandboxed verifier: no child or orphan uses this unique run's paths.
        processes = subprocess.check_output(['/bin/ps', '-axo', 'pid=,command='], text=True)
        assert not any(str(resources) in line for line in processes.splitlines()), processes
        report = {'passed': True, 'cancelled': True, 'exit_code': result.returncode,
                  'network_denied': True, 'installed_dependencies_denied': denied,
                  'fresh_state_and_output': True, 'relocated_path_has_chinese_and_space': True,
                  'workers_started': bool(stopped), 'no_surviving_run_processes': True,
                  'elapsed_seconds': round(time.monotonic() - started, 3)}
        (output / '验收.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if result.returncode:
        raise RuntimeError('Portable run failed; see ' + str(output / '运行.log'))
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    complete = [e for e in events if e.get('type') == 'complete']
    assert len(complete) == 1 and complete[0]['file_count'] == 2
    document = Path(complete[0]['path'])
    text = document.read_text('utf-8')
    assert '说话人' in text and '[00:' in text
    assert text.index('2026-11-12 10:00:00') < text.index('2026-11-12 11:00:00')
    assert quote(first.name) in text and quote(second.name) in text
    assert text.count('这是录音转写工具的中文流程验收') == 2
    assert not (app_dir / 'python').exists() and not (app_dir / 'models-light').exists()
    batch = json.loads((user_dir / '转写结果/批次索引.json').read_text())
    assert batch['status'] == 'completed' and all(e['status'] == 'completed' for e in batch['files'])
    if args.metadata_change:
        assert len(metadata_changes) == 2 and all(all(change[key] for key in
            ('mtime_unchanged', 'ctime_changed', 'content_unchanged')) for change in metadata_changes)
    report = {'passed': True, 'elapsed_seconds': round(time.monotonic() - started, 3),
              'network_denied': True, 'installed_dependencies_denied': denied,
              'fresh_state_and_output': True, 'relocated_path_has_chinese_and_space': True,
              'real_model': True, 'input_formats': ['M4A', 'PCM16 WAV'],
              'input_count': 2, 'workers': len(batch['worker_pids']),
              'document': str(document), 'document_sha256': hashlib.sha256(document.read_bytes()).hexdigest(),
              'app': str(app), 'event_count': len(events), 'metadata_changes': metadata_changes}
    (output / '验收.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

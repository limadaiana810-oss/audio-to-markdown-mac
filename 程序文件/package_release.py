"""Create a relocatable offline bundle and a shareable DMG; never copy user data."""
import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile
from fetch_ffmpeg_source import ensure_source

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = ROOT / '程序文件'
MACHO = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sanitize_source(path):
    if path.name == 'models-light.manifest.json':
        data = json.loads(path.read_text('utf-8'))
        for item in data.get('sources', []):
            item['path'] = item['component'] + '/' + Path(item['path']).name
        write_json(path, data)
    elif path.name.startswith('requirements') and path.suffix == '.txt':
        text = path.read_text('utf-8')
        text = re.sub(r'/Users/[^\n]+?/程序文件/', '', text)
        path.write_text(text, encoding='utf-8')


def make_runtime(installed, cache):
    """A standalone interpreter plus plain site-packages avoids absolute venv links."""
    ensure_source()
    runtime = cache / 'runtime'
    signature = {
        'python': sha(installed / 'python/bin/python3.12'),
        'dependencies': sha(PROGRAM / 'requirements-light-macos-arm64.txt'),
        'models': sha(PROGRAM / 'models-light.manifest.json'),
        'audio_build': sha(PROGRAM / 'build_audio_runtime.sh'),
        'layout': 4,
    }
    mark = cache / 'runtime-cache.json'
    if runtime.is_dir() and mark.exists() and json.loads(mark.read_text()) == signature:
        return runtime
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    shutil.copytree(installed / 'python', runtime / 'python', symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    site = runtime / 'python/lib/python3.12/site-packages'
    shutil.rmtree(site)
    shutil.copytree(installed / 'venv-light/lib/python3.12/site-packages', site, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', 'direct_url.json'))
    # Only the interpreter is needed; venv/pip entrypoints contain install-time paths.
    for item in (runtime / 'python/bin').iterdir():
        if item.name not in ('python', 'python3', 'python3.12'):
            item.unlink()
    # Replace the upstream all-codec GPL binary with our audio-only LGPL build.
    for item in (site / 'imageio_ffmpeg/binaries').glob('ffmpeg-*'):
        item.unlink()
    # Locally compiled wheels can retain build-directory debug symbols.
    private_prefix = (str(Path.home()) + '/').encode()
    for path in site.rglob('*'):
        if path.suffix in ('.so', '.dylib') and path.is_file() and not path.is_symlink():
            if private_prefix in path.read_bytes():
                run(['/usr/bin/strip', '-S', path])
                if private_prefix in path.read_bytes():
                    raise ValueError('Private build path remains in ' + str(path.relative_to(site)))
    models = runtime / 'models-light'
    models.mkdir()
    manifest = json.loads((PROGRAM / 'models-light.manifest.json').read_text('utf-8'))
    for item in manifest['files']:
        source = installed / 'models-light' / item['path']
        assert source.stat().st_size == item['size'] and sha(source) == item['sha256'], source
        target = models / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(PROGRAM / 'models-light.manifest.json', models / 'models-light.manifest.json')
    sanitize_source(models / 'models-light.manifest.json')
    audio = cache / 'audio'
    if not (audio / 'ffmpeg').exists() or not (audio / 'build.sha256').exists() or (audio / 'build.sha256').read_text() != signature['audio_build']:
        run(['/bin/bash', PROGRAM / 'build_audio_runtime.sh', ROOT / '分发资源/ffmpeg-9.0.2.tar.xz', audio])
        (audio / 'build.sha256').write_text(signature['audio_build'])
    (runtime / 'bin').mkdir()
    shutil.copy2(audio / 'ffmpeg', runtime / 'bin/ffmpeg')
    remove_build_search_paths(runtime)
    write_json(runtime / 'portable.json', {'schema': 1, 'python': '3.12.16', 'engine': 'light',
                                          'architecture': 'arm64', 'offline': True})
    # Never permit a symlink back into the developer's installed environment.
    for path in runtime.rglob('*'):
        if path.is_symlink() and (os.path.isabs(os.readlink(path)) or not path.resolve().is_relative_to(runtime.resolve())):
            raise ValueError('Non-portable symlink: ' + str(path.relative_to(runtime)))
    write_json(mark, signature)
    return runtime


def make_notices(runtime, destination):
    destination.mkdir(parents=True)
    site = runtime / 'python/lib/python3.12/site-packages'
    packages = []
    for distribution in importlib.metadata.distributions(path=[str(site)]):
        metadata = distribution.metadata
        record = {'name': metadata.get('Name'), 'version': metadata.get('Version'),
                  'license': metadata.get('License-Expression') or metadata.get('License'),
                  'home_page': metadata.get('Home-page'), 'project_urls': metadata.get_all('Project-URL', [])}
        packages.append(record)
    write_json(destination / 'Python依赖清单.json', sorted(packages, key=lambda v: (v['name'] or '').lower()))
    license_root = destination / 'Python及依赖许可'
    count = 0
    for source in (runtime / 'python').rglob('*'):
        if source.is_file() and not source.is_symlink() and any(term in source.name.upper() for term in ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE')):
            target = license_root / source.relative_to(runtime / 'python')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
    for source in (ROOT / '分发资源').iterdir():
        if source.is_file():
            shutil.copy2(source, destination / source.name)
        elif source.name in ('模型许可', 'Python底层组件许可', '补充依赖许可'):
            shutil.copytree(source, destination / source.name)
    shutil.copy2(PROGRAM / 'build_audio_runtime.sh', destination / 'build_audio_runtime.sh')
    (destination / '组件说明.md').write_text('''# 第三方组件

应用内含 Python、FunASR、ONNX Runtime、PyTorch 和相关依赖；原始许可证保留在应用内部，并在本目录集中收录。详细版本及项目链接见 `Python依赖清单.json`。

分发构建去除了本机编译调试路径和上游 wheel 遗留的构建机器动态库搜索路径，保留依赖随包提供的动态库及相对加载路径；处理步骤见源码 `package_release.py`。

轻量模型由 Paraformer、FSMN VAD、小型 CT-Transformer 标点及 CAM++ 组成。来源及权重哈希见源码中的 `models.tsv` 和 `models-light.manifest.json`，模型声明及许可见 `模型许可`。

本应用通过独立进程调用 FFmpeg，包含 FFmpeg 9.0.2 的音频处理构建；没有启用 GPL、nonfree 或网络协议。FFmpeg 使用 LGPL 2.1 或更新版本许可，完整原始源码归档及构建脚本随包提供。应用没有对 FFmpeg 源码做修改，构建参数由 `build_audio_runtime.sh` 给出。可自行重编并替换应用资源目录下 `runtime/bin/ffmpeg`；修改后本地应用签名需重新签署。

FFmpeg 官方许可说明：https://ffmpeg.org/legal.html
Python standalone 来源：https://github.com/astral-sh/python-build-standalone

重编音频工具：在本目录运行 `bash build_audio_runtime.sh ffmpeg-9.0.2.tar.xz ./重编结果`，需要 Apple Silicon Mac 和 Xcode Command Line Tools。

用户录音、声纹、热词、识别结果、日志和缓存不包含在分发包内。
''', encoding='utf-8')
    return count


def macho_paths(root):
    result = []
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                if stream.read(4) in MACHO:
                    result.append(path)
    return result


def remove_build_search_paths(runtime):
    for binary in macho_paths(runtime):
        output = subprocess.check_output(['/usr/bin/otool', '-l', str(binary)], text=True)
        paths = set()
        for block in output.split('Load command '):
            if 'cmd LC_RPATH\n' in block:
                match = re.search(r'\bpath (.+?) \(offset', block)
                if match and match.group(1).startswith(('/Users/', '/opt/homebrew/', '/usr/local/')):
                    paths.add(match.group(1))
        for path in paths:
            run(['/usr/bin/install_name_tool', '-delete_rpath', path, binary],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def audit_binary(path, app):
    output = subprocess.check_output(['/usr/bin/otool', '-l', str(path)], text=True)
    versions = re.findall(r'\bminos\s+(\d+(?:\.\d+){0,2})', output)
    versions += re.findall(r'cmd LC_VERSION_MIN_MACOSX\n\s+cmdsize \d+\n\s+version (\d+(?:\.\d+){0,2})', output)
    if any(tuple(int(v) for v in version.split('.')) > (14, 0, 0) for version in versions):
        raise ValueError('Requires macOS newer than 14: ' + str(path.relative_to(app)) + ' ' + str(versions))
    rpaths = []
    for block in output.split('Load command '):
        if 'cmd LC_RPATH\n' in block:
            match = re.search(r'\bpath (.+?) \(offset', block)
            if match:
                rpaths.append(match.group(1))
    if any(p.startswith(('/Users/', '/opt/homebrew/', '/usr/local/')) for p in rpaths):
        raise ValueError('External library search path: ' + str(path.relative_to(app)))
    libs = subprocess.check_output(['/usr/bin/otool', '-L', str(path)], text=True)
    dependencies = '\n'.join(line for line in libs.splitlines() if line.startswith(('\t', ' ')))
    if any(prefix in dependencies for prefix in ('/Users/', '/opt/homebrew/', '/usr/local/')):
        raise ValueError('External dependency: ' + str(path.relative_to(app)))
    arch = subprocess.check_output(['/usr/bin/lipo', '-archs', str(path)], text=True).strip()
    if 'arm64' not in arch.split():
        raise ValueError('Missing arm64: ' + str(path.relative_to(app)))
    return {'path': str(path.relative_to(app)), 'architectures': arch, 'minimum_macos': versions,
            'library_search_paths': rpaths}


def source_archive(destination, version):
    with tempfile.TemporaryDirectory(prefix='recording-source-') as temp:
        root = Path(temp) / ('录音转写-' + version)
        shutil.copytree(PROGRAM, root / '程序文件', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
        shutil.copytree(ROOT / '分发资源', root / '分发资源')
        for name in ('README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
                     'CHANGELOG.md', 'CONTRIBUTING.md', 'CONTRIBUTING.en.md', '.gitignore', '.gitattributes',
                     '使用说明.txt', '启动.command', '安装.command', '卸载.command', '外部模型总结提示词.md'):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, root / name)
        for name in ('docs', '.github'):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, root / name,
                                ignore=shutil.ignore_patterns('.DS_Store', '__pycache__'))
        for path in root.rglob('*'):
            if path.is_file():
                sanitize_source(path)
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(root.parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / '发布')
    parser.add_argument('--runtime-only', action='store_true')
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    installed = Path(os.environ.get('TX_BUILD_APP_DIR', str(Path.home() / 'Library/Application Support/录音转写工具')))
    cache = ROOT / '.package-cache'
    cache.mkdir(exist_ok=True)
    runtime = make_runtime(installed, cache)
    run([runtime / 'python/bin/python3', '-I', '-B', PROGRAM / 'selftest.py', '--engine', 'light',
         '--model-dir', runtime / 'models-light', '--manifest', PROGRAM / 'models-light.manifest.json',
         '--verify-models-only'])
    if args.runtime_only:
        print(runtime)
        return
    version = (PROGRAM / 'VERSION').read_text().strip()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = cache / 'release-stage'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    app = stage / '录音转写.app'
    run(['/bin/bash', PROGRAM / 'build_app.sh', app])
    resources = app / 'Contents/Resources'
    shutil.copytree(runtime, resources / 'runtime', symlinks=True)
    for path in (resources / '程序文件').iterdir():
        if path.is_file():
            sanitize_source(path)
    for name in ('README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md',
                 'CONTRIBUTING.md', 'CONTRIBUTING.en.md'):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / 'docs', stage / 'docs')
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, resources / name)
    source_archive(stage / '源码.zip', version)
    license_count = make_notices(resources / 'runtime', stage / '第三方许可')
    shutil.copytree(stage / '第三方许可', resources / '第三方许可')
    (stage / 'Applications').symlink_to('/Applications')
    binaries = macho_paths(app)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        audit = list(pool.map(lambda p: audit_binary(p, app), binaries))
    for path in binaries:
        run(['/usr/bin/codesign', '--force', '--sign', '-', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(['/usr/bin/codesign', '--force', '--sign', '-', app])
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    write_json(output / '运行组件检查.json', {'version': version, 'binaries': audit, 'license_files': license_count,
                                           'minimum_macos': '14.0', 'signing': 'ad-hoc', 'notarized': False})
    if args.stage_only:
        print(app)
        return
    image = output / ('Audio-to-Markdown-' + version + '-AppleSilicon.dmg')
    pending_image = output / ('.' + image.stem + '.pending.dmg')
    run(['/usr/bin/hdiutil', 'create', '-ov', '-format', 'UDZO', '-fs', 'HFS+', '-volname',
         '录音转写 ' + version, '-srcfolder', stage, pending_image])
    run(['/usr/bin/hdiutil', 'verify', pending_image])
    os.replace(pending_image, image)
    (output / (image.name + '.sha256')).write_text(sha(image) + '  ' + image.name + '\n', encoding='utf-8')
    shutil.copy2(ROOT / 'README.md', output / 'README.md')
    write_json(output / '构建记录.json', {'version': version, 'dmg': image.name, 'bytes': image.stat().st_size,
                                         'sha256': sha(image), 'signing': 'ad-hoc', 'notarized': False,
                                         'macho_count': len(binaries), 'offline_runtime': True})
    print(image)


if __name__ == '__main__':
    main()

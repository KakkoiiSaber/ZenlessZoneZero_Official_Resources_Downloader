"""Real local Git remotes and a fake Docker CLI exercise the NAS shell runner."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'NAS shell integration runs on Linux CI')
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.work = self.root / 'work tree'
        self.remote = self.root / 'origin.git'
        self.git(self.seed, 'init', '--initial-branch=main')
        self.git(self.seed, 'config', 'user.name', 'Test')
        self.git(self.seed, 'config', 'user.email', 'test@example.invalid')
        source = Path(__file__).resolve().parent.parent
        for name in ['run-downloader.sh', 'Dockerfile',
                     'requirements-download.txt', '.dockerignore', 'compose.yaml', 'compose.wiki.yaml']:
            shutil.copyfile(source / name, self.seed / name)
        (self.seed / '.gitignore').write_text('.env\n.cache/\nstate/\nmedia/\n')
        (self.seed / 'web').mkdir()
        (self.seed / 'web' / 'app.js').write_text('version one')
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '-m', 'initial')
        self.git(self.root, 'clone', '--bare', str(self.seed), str(self.remote))
        self.git(self.seed, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.root, 'clone', str(self.remote), str(self.work))
        self.git(self.work, 'config', 'user.name', 'Test')
        self.git(self.work, 'config', 'user.email', 'test@example.invalid')
        (self.work / '.env').write_text('DOWNLOAD_PATH=/private/media\nNAS_UID=1000\n')
        (self.work / 'state').mkdir()
        (self.work / 'state' / 'keep').write_text('download journal')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'docker.log'
        docker = self.bin / 'docker'
        docker.write_text('''#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_TEST_LOG"
case "$*" in
  *"ps --status running -q downloader") [ "${RUNNING_DOWNLOAD:-0}" = 1 ] && echo active; exit 0 ;;
  *"ps --status running -q wiki") [ "${RUNNING_WIKI:-0}" = 1 ] && echo active; exit 0 ;;
  *"build downloader") [ "${FAIL_BUILD:-0}" = 1 ] && exit 8 ;;
esac
exit 0
''')
        docker.chmod(0o755)

    def git(self, directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def update(self, name='web/app.js', value='version two'):
        (self.seed / name).write_text(value)
        self.git(self.seed, 'add', name)
        self.git(self.seed, 'commit', '-m', 'update')
        self.git(self.seed, 'push', 'origin', 'main')

    def run_task(self, **extra):
        env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
               'DOCKER_TEST_LOG': str(self.log), **extra}
        result = subprocess.run(['/bin/sh', str(self.work / 'run-downloader.sh')],
                                env=env, capture_output=True, text=True, timeout=15)
        return result, self.log.read_text() if self.log.exists() else ''

    def test_fast_forward_preserves_private_files_and_reloads_wiki(self):
        self.assertEqual(self.run_task()[0].returncode, 0)
        self.log.write_text('')
        self.update()
        result, log = self.run_task(RUNNING_WIKI='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / 'web/app.js').read_text(), 'version two')
        self.assertEqual((self.work / 'state/keep').read_text(), 'download journal')
        self.assertIn('/private/media', (self.work / '.env').read_text())
        self.assertIn('up -d --no-build --force-recreate wiki', log)
        self.assertNotIn('build downloader', log)
        self.assertFalse((self.work / '.cache/runner.lock').exists())

    def test_offline_keeps_running_local_code(self):
        self.git(self.work, 'remote', 'set-url', 'origin', str(self.root / 'offline.git'))
        result, log = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GitHub is unavailable', result.stdout)
        self.assertIn('--exit-code-from downloader downloader', log)

    def test_local_modifications_are_not_overwritten(self):
        (self.work / 'web/app.js').write_text('private edit')
        self.update()
        result, _ = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Local repository changes', result.stdout)
        self.assertEqual((self.work / 'web/app.js').read_text(), 'private edit')

    def test_dependency_changes_rebuild_once_and_retry_after_failure(self):
        self.assertEqual(self.run_task()[0].returncode, 0)
        self.update('requirements-download.txt', 'requests>=2.32,<3\n# changed\n')
        old_marker = (self.work / '.cache/runtime-signature').read_text()
        result, _ = self.run_task(FAIL_BUILD='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.work / '.cache/runtime-signature').read_text(), old_marker)
        self.log.write_text('')
        result, log = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('build downloader', log)
        self.log.write_text('')
        result, log = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('build downloader', log)

    def test_running_downloader_defers_update_and_duplicate_download(self):
        self.update()
        result, log = self.run_task(RUNNING_DOWNLOAD='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.work / 'web/app.js').read_text(), 'version one')
        self.assertNotIn('--exit-code-from', log)

    def test_stopped_wiki_is_not_started_by_daily_task(self):
        self.update()
        result, log = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('up -d --no-build --force-recreate wiki', log)
        self.assertIn('--exit-code-from', log)

    def test_stale_lock_from_previous_boot_is_recovered(self):
        lock = self.work / '.cache/runner.lock'
        lock.mkdir(parents=True)
        (lock / 'pid').write_text(str(os.getpid()))
        (lock / 'boot').write_text('previous-boot')
        result, _ = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())

    def test_diverged_history_is_not_reset(self):
        (self.work / 'web/app.js').write_text('local commit')
        self.git(self.work, 'add', 'web/app.js')
        self.git(self.work, 'commit', '-m', 'local')
        self.update()
        result, _ = self.run_task()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('history has diverged', result.stdout)
        self.assertEqual((self.work / 'web/app.js').read_text(), 'local commit')

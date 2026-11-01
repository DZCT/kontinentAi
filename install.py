"""Fresh Ubuntu installation. Refuses to overwrite an existing bot installation."""
import ast
import grp
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

TARGET = Path('/opt/kontinent-bot')
SERVICE = Path('/etc/systemd/system/kontinent-bot.service')
FILES = ('bot.py', 'ui.py', 'configure.py', 'reconcile.py', 'requirements.txt', '.env.example')


def validate_source(source):
    for name in FILES:
        path = source / name
        if not path.is_file():
            raise RuntimeError('Missing release file: ' + name)
        if name.endswith('.py'):
            ast.parse(path.read_text(), filename=name, feature_version=(3, 10))
    if not (source / 'deploy/kontinent-bot.service').is_file():
        raise RuntimeError('Missing service definition')


def run(*args):
    subprocess.run(args, check=True)


def install(source):
    # Check before apt, copying, user creation, or changing services.
    if TARGET.exists() or SERVICE.exists():
        raise RuntimeError('Existing installation detected. Nothing was changed. This installer is for a NEW server only.')
    validate_source(source)
    if not sys.stdin.isatty():
        raise RuntimeError('Open an interactive SSH terminal to enter your credentials.')
    print('Installing system packages...', flush=True)
    run('apt-get', 'update')
    run('apt-get', 'install', '-y', 'python3-venv', 'ca-certificates')
    try:
        account = pwd.getpwnam('kontinent')
        if account.pw_uid == 0:
            raise RuntimeError('Refusing to use a root service account')
        grp.getgrnam('kontinent')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home', str(TARGET), '--shell', '/usr/sbin/nologin', 'kontinent')
    account = pwd.getpwnam('kontinent')
    gid = grp.getgrnam('kontinent').gr_gid
    TARGET.mkdir(mode=0o750)
    os.chown(TARGET, 0, gid)
    for name in FILES:
        dst = TARGET / name
        shutil.copyfile(source / name, dst)
        os.chown(dst, 0, gid)
        os.chmod(dst, 0o640)
    data = TARGET / 'data'
    data.mkdir(mode=0o700)
    os.chown(data, account.pw_uid, gid)
    # Deliberately avoid importing/copying .env, databases or backups from source.
    run(sys.executable, '-m', 'venv', str(TARGET / '.venv'))
    run(str(TARGET / '.venv/bin/pip'), 'install', '-r', str(TARGET / 'requirements.txt'))
    # Parent umask must allow the service user to traverse the root-owned venv.
    for root, dirs, files in os.walk(TARGET / '.venv'):
        os.chmod(root, 0o755)
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                os.chmod(p, 0o755 if p.stat().st_mode & 0o111 else 0o644)
    print('\nDependencies installed. Configuring your NEW bot...', flush=True)
    run(sys.executable, str(TARGET / 'configure.py'))
    shutil.copyfile(source / 'deploy/kontinent-bot.service', SERVICE)
    os.chown(SERVICE, 0, 0)
    os.chmod(SERVICE, 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'kontinent-bot')
    print('\nINSTALL OK. Send /start to your Telegram bot.')
    print('Status: sudo systemctl status kontinent-bot --no-pager')


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run: sudo python3 install.py')
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10+ is required (Ubuntu 22.04 or newer).')
    # Normal mode for dependencies; configure.py protects secrets separately.
    os.umask(0o022)
    try:
        install(Path(__file__).resolve().parent)
    except (Exception, KeyboardInterrupt) as exc:
        print('\nInstallation stopped:', str(exc) or 'Cancelled', file=sys.stderr)
        print('Do not delete an existing installation. Keep the output for troubleshooting.', file=sys.stderr)
        raise SystemExit(1)

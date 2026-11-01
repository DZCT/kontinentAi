"""Interactive first-time configuration; secrets never appear in shell commands."""
import getpass
import grp
import os
from pathlib import Path
import re


def make_config(token, owner, key):
    token, owner, key = token.strip(), owner.strip(), key.strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Telegram bot token format is invalid. Get it from @BotFather.')
    if not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
        raise ValueError('Owner ID must be a positive numeric Telegram user ID.')
    if not re.fullmatch(r'sk-kont-[A-Za-z0-9_-]+', key):
        raise ValueError('Kontinent key must start with sk-kont- and have no spaces.')
    return (f'TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_OWNER_ID={owner}\nKONTINENT_API_KEY={key}\n'
            'DEFAULT_MODEL=deepseek-v4.1-flash\nDAILY_BUDGET_USD=1\nTOTAL_BUDGET_USD=25\n'
            'DATABASE_PATH=data/bot.sqlite3\n')


def configure(target):
    target = Path(target)
    if target.exists():
        raise ValueError('A .env file already exists; it was not overwritten.')
    print('Enter credentials below. Secret input is invisible; paste it, then press Enter.')
    while True:
        token = getpass.getpass('1/3 Telegram bot token: ')
        owner = input('2/3 Your numeric Telegram user ID: ')
        key = getpass.getpass('3/3 Kontinent API key: ')
        try:
            text = make_config(token, owner, key)
            break
        except ValueError as exc:
            print(str(exc))
            print('Please try again, or press Ctrl+C to cancel.')
    # Exclusive create prevents accidentally overwriting a config created in parallel.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            if os.geteuid() == 0:
                os.fchown(f.fileno(), 0, grp.getgrnam('kontinent').gr_gid)
                os.fchmod(f.fileno(), 0o640)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    print('Configuration saved. No credentials were printed.')


if __name__ == '__main__':
    try:
        configure(Path(__file__).resolve().parent / '.env')
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) or 'Cancelled. Configuration was not saved.')
        raise SystemExit(1)

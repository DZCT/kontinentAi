"""Manual reconciliation after verifying an ambiguous charge in the dashboard."""
import argparse
import os
import uuid
from decimal import Decimal

from dotenv import load_dotenv
from bot import Store

load_dotenv()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cost-usd', required=True, type=Decimal)
args = parser.parse_args()
if not args.cost_usd.is_finite() or args.cost_usd < 0:
    parser.error('cost must be a finite nonnegative number')
store = Store(os.getenv('DATABASE_PATH', 'data/bot.sqlite3'))
if not store.get('uncertain', False):
    parser.error('No ambiguous request is pending')
# One transaction: recording the charge and unlocking must be atomic.
with store.db:
    store.db.execute("INSERT INTO usage VALUES (?,?,?,?,?)", (
        'manual-' + str(uuid.uuid4()), __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        int(args.cost_usd * 1_000_000), 0, 'manual-reconciliation'))
    store.db.execute("INSERT OR REPLACE INTO kv VALUES ('uncertain', 'false')")
print('Recorded verified cost and cleared the lock.')

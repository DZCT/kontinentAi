"""Private KontinentAI Telegram bot. Python 3.10+, no inbound ports."""
import asyncio
import base64
import io
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from ui import Interface, button, esc
from dotenv import load_dotenv

load_dotenv()
LOG = logging.getLogger('bot')
API = 'https://api.kontinentai.com'
MAX_FILE = 8 * 1024 * 1024
HELP = '''✦ راهنمای دستیار شخصی\n/menu — منوی اصلی و دکمه‌ها
پیام، عکس یا فایل PDF/متنی بفرستید. کپشن عکس، سؤال شماست.
/models — فهرست زنده و انتخاب مدل
/model MODEL_ID — انتخاب مستقیم
/new — پاک کردن حافظهٔ گفتگو
/settings — تنظیمات فعلی
/system متن — دستور شخصیت؛ /system reset بازنشانی
/tokens 4096 — سقف توکن خروجی (۱۲۸ تا ۱۶۳۸۴)
/reasoning off|low|medium|high — شدت استدلال
/json on|off — خروجی JSON (مدل باید پشتیبانی کند)
/stats — مصرف ثبت‌شده؛ موجودی واقعی در داشبورد سرویس
/export — دریافت گفتگو
/raw chat|messages|responses|count — سپس فایل JSON درخواست بفرستید
/cancel — لغو حالت درخواست پیشرفته
/help — راهنما
درخواست‌های پیشرفته پاسخ کامل JSON دارند؛ اجرای ابزارها خودکار نیست.'''


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, ts TEXT NOT NULL,
          cost INTEGER, tokens INTEGER NOT NULL, model TEXT NOT NULL);
        ''')

    def get(self, key, default=None):
        row = self.db.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, json.dumps(value)))

    def record(self, request_id, data):
        cost = data.get('kontinent', {}).get('cost_micros')
        if cost is not None:
            cost = int(cost)
            if cost < 0:
                raise ValueError('Invalid cost')
        usage = data.get('usage', {})
        tokens = usage.get('total_tokens', usage.get('input_tokens', 0) + usage.get('output_tokens', 0))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?)',
                            (request_id, datetime.now(timezone.utc).isoformat(), cost, tokens, data.get('model', '?')))
        return cost

    def totals(self, today=False):
        where = ' WHERE substr(ts,1,10)=?' if today else ''
        args = (datetime.now(timezone.utc).date().isoformat(),) if today else ()
        return self.db.execute('SELECT coalesce(sum(cost),0), count(*), coalesce(sum(tokens),0), '
                               'coalesce(sum(cost IS NULL),0) FROM usage' + where, args).fetchone()


class Bot(Interface):
    def __init__(self):
        self.token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
        self.key = os.environ['KONTINENT_API_KEY'].strip()
        self.owner = int(os.environ['TELEGRAM_OWNER_ID'])
        if self.owner <= 0 or not self.token or not self.key:
            raise ValueError('Invalid configuration')
        self.store = Store(os.getenv('DATABASE_PATH', 'data/bot.sqlite3'))
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(240, connect=20), follow_redirects=False)
        self.catalog = []
        self.default_model = os.getenv('DEFAULT_MODEL', 'claude-haiku-4-5-20251001')
        self.daily = int(float(os.getenv('DAILY_BUDGET_USD', '1')) * 1_000_000)
        self.total = int(float(os.getenv('TOTAL_BUDGET_USD', '25')) * 1_000_000)
        if self.daily <= 0 or self.total <= 0:
            raise ValueError('Budgets must be positive')
        self.system = 'شما دستیار شخصی دقیق و مفید هستید. پیش‌فرض به فارسی پاسخ دهید.'

    async def tg(self, method, body=None, files=None):
        url = f'https://api.telegram.org/bot{self.token}/{method}'
        r = await self.client.post(url, data=body if files else None,
                                   json=None if files else body, files=files)
        r.raise_for_status()
        result = r.json()
        if not result.get('ok'):
            raise RuntimeError('Telegram request failed')
        return result['result']

    async def say(self, text, keyboard=None):
        # Telegram limits UTF-16 code units, use small Unicode chunks.
        for i in range(0, len(text), 1800):
            body = {'chat_id': self.owner, 'text': text[i:i+1800]}
            if keyboard and i == 0:
                body['reply_markup'] = {'inline_keyboard': keyboard}
            await self.tg('sendMessage', body)

    async def document(self, name, value):
        raw = value.encode('utf-8')
        await self.tg('sendDocument', {'chat_id': str(self.owner)},
                      {'document': (name, raw, 'application/octet-stream')})

    async def models(self):
        r = await self.client.get(API + '/v1/models')
        r.raise_for_status()
        self.catalog = r.json()['data']
        return self.catalog

    async def model_info(self, model):
        if not self.catalog:
            await self.models()
        found = next((m for m in self.catalog if m['id'] == model), None)
        if not found:
            raise ValueError('مدل در فهرست سرویس نیست؛ /models را بزنید.')
        return found

    async def download(self, file):
        if file.get('file_size', 0) > MAX_FILE:
            raise ValueError('حداکثر اندازهٔ فایل ۸ مگابایت است.')
        meta = await self.tg('getFile', {'file_id': file['file_id']})
        if meta.get('file_size', 0) > MAX_FILE:
            raise ValueError('فایل بیش از حد بزرگ است.')
        data = bytearray()
        async with self.client.stream('GET', f"https://api.telegram.org/file/bot{self.token}/{meta['file_path']}") as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FILE:
                    raise ValueError('فایل بیش از حد بزرگ است.')
        return bytes(data)

    def budget(self):
        if self.store.get('uncertain', False):
            raise ValueError('هزینهٔ یک درخواست نامشخص است. ابتدا داشبورد را بررسی کنید؛ راهنمای رفع قفل در README است.')
        if self.store.totals(True)[0] >= self.daily or self.store.totals()[0] >= self.total:
            raise ValueError('سقف مصرف ثبت‌شده پر شده است؛ تنظیمات بودجه را در سرور تغییر دهید.')

    async def request(self, route, body):
        paid = route != 'messages/count_tokens'
        if paid:
            self.budget()
        rid = str(uuid.uuid4())
        headers = {'Authorization': f'Bearer {self.key}', 'Idempotency-Key': rid,
                   'anthropic-version': '2023-06-01'}
        if paid:
            # Persist before sending: a crash must not silently lose an unknown charge.
            self.store.set('uncertain', True)
        try:
            r = await self.client.post(API + '/v1/' + route, json=body, headers=headers)
        except httpx.HTTPError:
            raise ValueError('ارتباط قطع شد؛ درخواست تکرار نشد. هزینه را در داشبورد بررسی کنید.') from None
        if r.is_error:
            # Only definitive client rejections are known to be uncharged.
            if paid and r.status_code in (400, 401, 402, 403, 404, 413, 422, 429):
                self.store.set('uncertain', False)
            try:
                code = r.json().get('error', {}).get('code', '')
            except ValueError:
                code = ''
            if code == 'PROVIDER_ERROR':
                message = r.json().get('error', {}).get('message', '')
                free = 'nothing was charged' in message.lower()
                if paid and free:
                    self.store.set('uncertain', False)
                raise ValueError('⚠️ مدل فعلاً از سمت سرویس در دسترس نیست. ' +
                                 ('هزینه‌ای دریافت نشد؛ از منو مدل دیگری انتخاب کنید.' if free else
                                  'وضعیت هزینه نامشخص است؛ داشبورد را بررسی کنید.'))
            raise ValueError(f'⚠️ سرویس خطای HTTP {r.status_code} داد ({code}). تنظیمات یا اعتبار را بررسی کنید.')
        data = r.json()
        if paid:
            cost = self.store.record(rid, data)
            self.store.set('uncertain', cost is None)
        return data

    async def chat(self, content):
        model = self.store.get('model', self.default_model)
        info = await self.model_info(model)
        history = self.store.get('history', [])
        if isinstance(content, list) or any(isinstance(m['content'], list) for m in history):
            if 'image' not in info.get('input_modalities', []):
                raise ValueError('این مدل تصویر نمی‌پذیرد؛ مدل تصویری انتخاب کنید یا /new بزنید.')
        if '/v1/chat/completions' not in info.get('endpoints', ['/v1/chat/completions']):
            raise ValueError('این مدل روی chat فعال نیست؛ از /raw استفاده کنید.')
        messages = [{'role': 'system', 'content': self.store.get('system', self.system)}] + history + [{'role': 'user', 'content': content}]
        if len(json.dumps(messages, ensure_ascii=False)) > 180_000:
            raise ValueError('حافظهٔ گفتگو بزرگ شده؛ /new بزنید یا فایل کوچک‌تری بفرستید.')
        body = {'model': model, 'messages': messages, 'max_tokens': self.store.get('tokens', 4096)}
        reasoning = self.store.get('reasoning', 'off')
        if reasoning != 'off':
            body['reasoning_effort'] = reasoning
        if self.store.get('json', False):
            body['response_format'] = {'type': 'json_object'}
            messages[0]['content'] += '\nReturn valid JSON only.'
        await self.tg('sendChatAction', {'chat_id': self.owner, 'action': 'typing'})
        data = await self.request('chat/completions', body)
        message = data['choices'][0]['message']
        answer = message.get('content') or ''
        if message.get('tool_calls'):
            await self.document('tool-calls.json', json.dumps(data, ensure_ascii=False, indent=2))
            return
        if not isinstance(answer, str) or not answer:
            await self.document('response.json', json.dumps(data, ensure_ascii=False, indent=2))
            return
        # Keep complete turns, bounded to avoid unbounded input costs.
        self.store.set('history', (history + [{'role': 'user', 'content': content}, {'role': 'assistant', 'content': answer}])[-20:])
        if len(answer) > 14000:
            await self.document('answer.txt', answer)
        else:
            await self.ui_answer(answer)
        cost = data.get('kontinent', {}).get('cost_usd')
        note = f'مدل: {model} | هزینه: ' + (f'${cost:.6f}' if isinstance(cost, (int, float)) else 'نامشخص')
        if data['choices'][0].get('finish_reason') == 'length':
            note += '\nپاسخ به سقف توکن رسید؛ برای ادامه پیام بدهید.'
        await self.say('◈ ' + note, [[button('✦ مدل‌ها', 'u:models:0'), button('＋ گفتگوی تازه', 'u:new')], [button('⌂ خانه', 'u:home')]])

    async def raw(self, route, content):
        body = json.loads(content)
        if not isinstance(body, dict) or not isinstance(body.get('model'), str):
            raise ValueError('JSON باید یک object با model باشد.')
        info = await self.model_info(body['model'])
        endpoint = '/v1/' + route
        if endpoint not in info.get('endpoints', []) and route != 'messages/count_tokens':
            raise ValueError('مدل این endpoint را پشتیبانی نمی‌کند.')
        if route == 'messages/count_tokens' and '/v1/messages' not in info.get('endpoints', []):
            raise ValueError('شمارش توکن به مدل سازگار با Messages نیاز دارد.')
        if body.get('stream'):
            raise ValueError('حالت پیشرفته فقط پاسخ غیرجریانی می‌پذیرد.')
        body.pop('stream', None)
        if route != 'messages/count_tokens':
            field = 'max_output_tokens' if route == 'responses' else 'max_tokens'
            cap = self.store.get('tokens', 4096)
            value = body.pop('max_completion_tokens', body.get(field, cap))
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= cap:
                raise ValueError('حد خروجی درخواست باید کمتر یا برابر /tokens باشد.')
            body[field] = value
            if route == 'responses' and body.get('background'):
                raise ValueError('background در ربات پشتیبانی نمی‌شود.')
        data = await self.request(route, body)
        self.store.set('raw', None)
        await self.document('api-response.json', json.dumps(data, ensure_ascii=False, indent=2))

    async def command(self, text):
        cmd, _, arg = text.partition(' ')
        cmd, arg = cmd.split('@')[0].lower(), arg.strip()
        if cmd in ('/start', '/menu'):
            self.store.set('ui_input', None)
            await self.tg('sendMessage', {'chat_id': self.owner,
                'text': '✦ پنل شخصی شما آماده است. برای شروع، پیام بنویسید.',
                'reply_markup': {'keyboard': [[{'text': '✦ منوی اصلی'}, {'text': '✦ مدل‌ها'}],
                                             [{'text': '⚙️ تنظیمات'}, {'text': '📊 مصرف من'}]],
                                 'resize_keyboard': True, 'is_persistent': True}})
            await self.ui_home()
        elif cmd == '/help':
            await self.say(HELP, [[button('⌂ خانه', 'u:home')]])
        elif cmd == '/models':
            await self.ui_models(refresh=True)
        elif cmd == '/model':
            await self.model_info(arg)
            self.store.set('model', arg)
            await self.card('✅ <b>مدل انتخاب شد</b>\n\n<code>' + esc(arg) + '</code>\n\nپیام خود را بنویسید.', self.back())
        elif cmd == '/new':
            await self.ui_action('new')
        elif cmd == '/system':
            if not arg or len(arg) > 8000:
                raise ValueError('دستور شخصیت را در حداکثر ۸۰۰۰ نویسه بنویسید.')
            self.store.set('system', self.system if arg == 'reset' else arg)
            await self.say('دستور شخصیت ذخیره شد.')
        elif cmd == '/tokens':
            if not arg.isdigit() or not 128 <= int(arg) <= 16384:
                raise ValueError('عدد ۱۲۸ تا ۱۶۳۸۴ وارد کنید.')
            self.store.set('tokens', int(arg))
            await self.say('سقف خروجی ذخیره شد.')
        elif cmd == '/reasoning':
            if arg not in ('off', 'low', 'medium', 'high'):
                raise ValueError('گزینه‌ها: off low medium high')
            self.store.set('reasoning', arg)
            await self.say('ذخیره شد؛ پشتیبانی به مدل بستگی دارد.')
        elif cmd == '/json':
            if arg not in ('on', 'off'):
                raise ValueError('/json on یا /json off')
            self.store.set('json', arg == 'on')
            await self.say('ذخیره شد؛ پشتیبانی به مدل بستگی دارد.')
        elif cmd == '/settings':
            await self.ui_settings()
        elif cmd == '/stats':
            await self.ui_stats()
        elif cmd == '/export':
            await self.document('conversation.json', json.dumps(self.store.get('history', []), ensure_ascii=False, indent=2))
        elif cmd == '/raw':
            routes = {'chat': 'chat/completions', 'messages': 'messages', 'responses': 'responses', 'count': 'messages/count_tokens'}
            if arg not in routes:
                raise ValueError('/raw chat|messages|responses|count')
            self.store.set('raw', routes[arg])
            await self.say('🧩 حالت پیشرفته فعال است. فایل JSON درخواست را بفرستید؛ model الزامی است.', [[button('خروج و بازگشت', 'u:cancel')]])
        elif cmd == '/cancel':
            self.store.set('ui_input', None)
            self.store.set('raw', None)
            await self.say('حالت پیشرفته لغو شد.')
        else:
            await self.say('دستور ناشناخته؛ /help')

    async def handle(self, update):
        cb = update.get('callback_query')
        msg = cb.get('message', {}) if cb else update.get('message', {})
        sender = cb.get('from', {}) if cb else msg.get('from', {})
        # Reject groups, channels, forwards via other accounts, and every other user.
        if sender.get('id') != self.owner or msg.get('chat', {}).get('id') != self.owner or msg.get('chat', {}).get('type') != 'private':
            return
        if cb:
            await self.tg('answerCallbackQuery', {'callback_query_id': cb['id']})
            if cb.get('data', '').startswith('u:'):
                await self.ui_action(cb['data'][2:], msg.get('message_id'))
            elif cb.get('data', '').startswith('m:'):
                ids = self.store.get('catalog_ids', [])
                idx = int(cb['data'][2:])
                if 0 <= idx < len(ids):
                    await self.command('/model ' + ids[idx])
            return
        text = msg.get('text', '')
        shortcuts = {'✦ منوی اصلی': 'home', '✦ مدل‌ها': 'models:0',
                     '⚙️ تنظیمات': 'settings', '📊 مصرف من': 'stats'}
        if text in shortcuts:
            await self.ui_action(shortcuts[text])
            return
        if text.startswith('/'):
            await self.command(text)
            return
        if self.store.get('ui_input') == 'system':
            if not text or len(text) > 8000:
                raise ValueError('یک پیام متنی حداکثر ۸۰۰۰ نویسه بفرستید؛ /menu برای انصراف.')
            self.store.set('system', text)
            self.store.set('ui_input', None)
            await self.card('✅ <b>شخصیت دلخواه ذخیره شد</b>\n\n' + esc(text[:800]), self.back())
            return
        route = self.store.get('raw')
        if route:
            raw = (await self.download(msg['document'])).decode('utf-8-sig') if 'document' in msg else text
            await self.raw(route, raw)
            return
        if msg.get('media_group_id'):
            raise ValueError('عکس‌ها را تکی بفرستید؛ آلبوم پشتیبانی نمی‌شود.')
        if msg.get('photo'):
            raw = await self.download(msg['photo'][-1])
            content = [{'type': 'text', 'text': msg.get('caption', 'این تصویر را تحلیل کن.')},
                       {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()}}]
        elif msg.get('document'):
            doc = msg['document']
            name = doc.get('file_name', '')
            suffix = Path(name).suffix.lower()
            if suffix not in ('.pdf', '.txt', '.md', '.py', '.js', '.ts', '.json', '.csv', '.yaml', '.yml', '.html', '.css', '.log', '.sql', '.xml'):
                raise ValueError('فایل PDF یا متن/کد بفرستید. عکس را به شکل Photo ارسال کنید.')
            raw = await self.download(doc)
            if suffix == '.pdf':
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                if len(reader.pages) > 100:
                    raise ValueError('حداکثر ۱۰۰ صفحه PDF پذیرفته می‌شود.')
                pieces = []
                size = 0
                for page in reader.pages:
                    part = page.extract_text() or ''
                    size += len(part)
                    if size > 100_000:
                        raise ValueError('متن PDF بیش از ۱۰۰ هزار نویسه است.')
                    pieces.append(part)
                extracted = '\n'.join(pieces)
            else:
                extracted = raw.decode('utf-8-sig')
            if not extracted.strip() or len(extracted) > 100_000:
                raise ValueError('فایل باید متن قابل استخراج با حداکثر ۱۰۰ هزار نویسه داشته باشد؛ PDF اسکن‌شده را به شکل عکس بفرستید.')
            content = msg.get('caption', 'این فایل را بررسی و خلاصه کن.') + '\n\n' + extracted
        elif text:
            content = text
        else:
            raise ValueError('پیام متنی، عکس یا فایل بفرستید. سرویس تولید تصویر و تبدیل صدا ندارد.')
        await self.chat(content)

    async def run(self):
        # Never delete queued messages or an existing webhook implicitly.
        webhook = await self.tg('getWebhookInfo')
        if webhook.get('url'):
            raise RuntimeError('Existing Telegram webhook: remove it before enabling polling')
        await self.tg('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in
                      [('menu', '✦ منوی اصلی'), ('help', 'راهنما'), ('models', 'انتخاب مدل'), ('new', 'گفتگوی تازه'), ('stats', 'مصرف'), ('settings', 'تنظیمات'), ('export', 'دریافت گفتگو'), ('cancel', 'لغو حالت پیشرفته')]]})
        LOG.info('Bot polling started')
        try:
            while True:
                try:
                    updates = await self.tg('getUpdates', {'offset': self.store.get('offset', 0), 'timeout': 30,
                                           'allowed_updates': ['message', 'callback_query']})
                except (httpx.HTTPError, RuntimeError):
                    LOG.warning('Telegram polling failed; retrying')
                    await asyncio.sleep(5)
                    continue
                for update in updates:
                    # At most once after crashes: do not replay potentially billed work.
                    self.store.set('offset', update['update_id'] + 1)
                    try:
                        await self.handle(update)
                    except (ValueError, UnicodeError) as exc:
                        await self.say(str(exc)[:1000], [[button('⌂ خانه', 'u:home'), button('📊 مصرف', 'u:stats')]])
                    except Exception as exc:
                        LOG.error('Update failed: %s', type(exc).__name__)
                        try:
                            await self.say('پردازش ناموفق بود؛ درخواست خودکار تکرار نشد. /stats را بررسی کنید.')
                        except Exception:
                            LOG.error('Could not deliver error notice')
        finally:
            await self.client.aclose()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # HTTP request logs contain Telegram bot tokens in URL paths.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    try:
        asyncio.run(Bot().run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        LOG.error('Stopped: %s (check configuration/network)', type(exc).__name__)
        raise SystemExit(1)

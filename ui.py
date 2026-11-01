"""Native Telegram UI. No web hosting, no additional dependencies."""
import hashlib
import html
import math
import re


def esc(value):
    return html.escape(str(value), quote=False)


def button(label, action):
    return {'text': label, 'callback_data': action}


def model_key(model_id):
    return hashlib.sha256(model_id.encode()).hexdigest()[:16]


def rich_text(text):
    """Conservative model Markdown -> Telegram HTML; always escape untrusted HTML."""
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    out = []
    for part in parts:
        if part.startswith('```') and part.endswith('```'):
            code = part[3:-3]
            if '\n' in code and re.fullmatch(r'[\w+#.-]*', code.split('\n', 1)[0]):
                code = code.split('\n', 1)[1]
            out.append('<pre>' + esc(code.strip('\n')) + '</pre>')
        elif part.startswith('`') and part.endswith('`'):
            out.append('<code>' + esc(part[1:-1]) + '</code>')
        else:
            value = esc(part)
            value = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', value)
            value = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', value, flags=re.M)
            out.append(value)
    return ''.join(out)


class Interface:
    @staticmethod
    def back():
        return [[button('⌂ خانه', 'u:home'), button('⚙️ تنظیمات', 'u:settings')]]

    async def card(self, text, rows, message_id=None):
        body = {'chat_id': self.owner, 'text': text, 'parse_mode': 'HTML',
                'reply_markup': {'inline_keyboard': rows},
                'link_preview_options': {'is_disabled': True}}
        if message_id is not None:
            body['message_id'] = message_id
            try:
                return await self.tg('editMessageText', body)
            except Exception as exc:
                # Repeated taps are harmless, unavailable/old messages get a new card.
                import httpx
                if isinstance(exc, httpx.HTTPStatusError):
                    response = exc.response
                    if response.status_code == 400:
                        if 'message is not modified' in response.text.lower():
                            return
                        if any(s in response.text.lower() for s in ("message can't be edited", 'message to edit not found')):
                            body.pop('message_id', None)
                            return await self.tg('sendMessage', body)
                raise
        return await self.tg('sendMessage', body)

    async def ui_home(self, mid=None):
        model = self.store.get('model', self.default_model)
        turns = len(self.store.get('history', [])) // 2
        mode = 'پیشرفته' if self.store.get('raw') else 'گفتگو'
        await self.card(
            '✦ <b>دستیار شخصی شما</b>\n'
            '━━━━━━━━━━━━━━━━━━\n'
            'از یک سؤال ساده تا تحلیل فایل؛ از همین‌جا شروع کنید.\n\n'
            f'◈ <b>مدل فعال</b>\n<code>{esc(model)}</code>\n\n'
            f'💬 حافظه: {turns} نوبت  ·  حالت: {mode}\n'
            'پیام خود را بنویسید، یا یکی از گزینه‌ها را انتخاب کنید.',
            [[button('✦ انتخاب مدل', 'u:models:0'), button('＋ گفتگوی تازه', 'u:new')],
             [button('⚙️ تنظیمات', 'u:settings'), button('📊 مصرف من', 'u:stats')],
             [button('🎭 شخصیت دستیار', 'u:personas'), button('📎 عکس و فایل', 'u:files')],
             [button('🧩 پیشرفته', 'u:advanced'), button('❔ راهنما', 'u:help')]], mid)

    async def ui_models(self, page=0, mid=None, refresh=False):
        if refresh or not self.catalog:
            await self.models()
        items = [m for m in self.catalog if '/v1/chat/completions' in m.get('endpoints', [])]
        pages = max(1, math.ceil(len(items) / 5))
        page = max(0, min(page, pages - 1))
        active = self.store.get('model', self.default_model)
        # Stable identifiers: old buttons never choose a different model after a refresh.
        mapping = self.store.get('ui_models', {})
        mapping.update({model_key(m['id']): m['id'] for m in items})
        self.store.set('ui_models', mapping)
        rows = []
        for m in items[page * 5:page * 5 + 5]:
            marker = '✓ ' if m['id'] == active else ''
            vision = '  ◉' if 'image' in m.get('input_modalities', []) else ''
            rows.append([button(marker + m['id'] + vision, 'u:model:' + model_key(m['id']))])
        if pages > 1:
            nav = []
            if page:
                nav.append(button('‹ قبلی', f'u:models:{page-1}'))
            nav.append(button(f'{page+1} / {pages}', 'u:noop'))
            if page < pages - 1:
                nav.append(button('بعدی ›', f'u:models:{page+1}'))
            rows.append(nav)
        rows += [[button('↻ تازه‌سازی فهرست', 'u:refresh')], [button('⌂ خانه', 'u:home')]]
        await self.card('✦ <b>مدلی برای هر کار</b>\n━━━━━━━━━━━━━━━━━━\n'
                        f'فعال: <code>{esc(active)}</code>\n\n'
                        'روی مدل بزنید تا قیمت و امکاناتش را ببینید.\n'
                        '✓ انتخاب فعلی   ◉ تحلیل تصویر\n'
                        '<i>حضور در فهرست، در دسترس بودن لحظه‌ای را تضمین نمی‌کند.</i>', rows, mid)

    async def ui_model(self, key, mid=None, select=False):
        name = self.store.get('ui_models', {}).get(key)
        if not name:
            await self.ui_models(mid=mid, refresh=True)
            return
        info = await self.model_info(name)
        if select:
            self.store.set('model', name)
        p = info.get('pricing', {})
        prefix = '✅ <b>مدل انتخاب شد</b>' if select else '✦ <b>مشخصات مدل</b>'
        rows = [] if select else [[button('✓ استفاده از این مدل', 'u:choose:' + key)]]
        rows += [[button('‹ فهرست مدل‌ها', 'u:models:0'), button('⌂ خانه', 'u:home')]]
        await self.card(prefix + f'\n\n<code>{esc(name)}</code>\n\n'
                        f'👁 تحلیل تصویر: {"دارد" if "image" in info.get("input_modalities", []) else "ندارد"}\n'
                        f'🧠 استدلال: {"دارد" if info.get("reasoning") else "اعلام نشده"}\n'
                        f'📚 ظرفیت زمینه: {esc(info.get("context_window", "—"))} توکن\n\n'
                        '<b>قیمت هر یک میلیون توکن</b>\n'
                        f'ورودی: <code>${esc(p.get("usd_per_million_input", "?"))}</code>\n'
                        f'خروجی: <code>${esc(p.get("usd_per_million_output", "?"))}</code>\n\n'
                        + ('اکنون پیام خود را بنویسید.' if select else 'انتخاب مدل هزینه‌ای ندارد.'), rows, mid)

    async def ui_settings(self, mid=None):
        output = self.store.get('tokens', 4096)
        reasoning = self.store.get('reasoning', 'off')
        json_on = self.store.get('json', False)
        await self.card('⚙️ <b>دستیار، مطابق سلیقهٔ شما</b>\n━━━━━━━━━━━━━━━━━━\n'
                        f'حداکثر خروجی: <b>{output:,}</b> توکن\n'
                        f'شدت استدلال: <code>{esc(reasoning)}</code>\n'
                        f'خروجی JSON: <b>{"روشن" if json_on else "خاموش"}</b>\n\n'
                        'هر گزینه را لمس کنید تا تغییر کند.\n'
                        '<i>پشتیبانی JSON و استدلال به مدل بستگی دارد. خروجی طولانی‌تر می‌تواند هزینه را بیشتر کند.</i>',
                        [[button('📏 طول پاسخ', 'u:tokens'), button('🧠 استدلال', 'u:reasoning')],
                         [button('JSON  ' + ('● روشن' if json_on else '○ خاموش'), 'u:json')],
                         [button('🎭 شخصیت', 'u:personas'), button('📤 دریافت گفتگو', 'u:export')],
                         [button('⌂ خانه', 'u:home')]], mid)

    async def ui_stats(self, mid=None):
        total, count, tokens, unknown = self.store.totals()
        daily = self.store.totals(True)[0]
        filled = min(10, max(0, int(daily / self.daily * 10)))
        bar = '▰' * filled + '▱' * (10 - filled)
        lock = '\n⚠️ یک هزینه نامشخص است؛ ابتدا داشبورد را بررسی کنید.' if self.store.get('uncertain', False) else ''
        await self.card('📊 <b>نمای مصرف شما</b>\n━━━━━━━━━━━━━━━━━━\n'
                        f'امروز (UTC)\n<code>{bar}</code>\n'
                        f'<b>${daily/1e6:.6f}</b> از سقف ${self.daily/1e6:g}\n\n'
                        f'کل مصرف ثبت‌شده: <b>${total/1e6:.6f}</b>\n'
                        f'سقف کل: ${self.total/1e6:g}\n'
                        f'درخواست‌ها: <b>{count:,}</b>  ·  توکن‌ها: <b>{tokens:,}</b>\n'
                        f'هزینهٔ ثبت‌نشده: {unknown}{lock}\n\n'
                        '<i>این گزارش موجودی حساب نیست و مصرف سایت یا برنامه‌های دیگر را شامل نمی‌شود. سقف‌ها قبل از درخواست بررسی می‌شوند؛ آخرین درخواست ممکن است از سقف عبور کند.</i>',
                        [[{'text': '↗ داشبورد و موجودی واقعی', 'url': 'https://kontinentai.com/dashboard/balance'}],
                         [button('↻ تازه‌سازی', 'u:stats'), button('⌂ خانه', 'u:home')]], mid)

    async def ui_personas(self, mid=None):
        await self.card('🎭 <b>لحن و نقش دستیار</b>\n━━━━━━━━━━━━━━━━━━\n'
                        'یک شخصیت انتخاب کنید یا دستور خودتان را بنویسید.\n'
                        'تغییر شخصیت، گفتگو را پاک نمی‌کند.',
                        [[button('✦ عمومی', 'u:persona:general'), button('⌘ برنامه‌نویس', 'u:persona:coder')],
                         [button('📚 معلم', 'u:persona:teacher'), button('✍️ نویسنده', 'u:persona:writer')],
                         [button('🌍 مترجم', 'u:persona:translator'), button('⚡ کوتاه و دقیق', 'u:persona:brief')],
                         [button('✎ دستور دلخواه', 'u:custom')], [button('⌂ خانه', 'u:home')]], mid)

    async def ui_action(self, action, mid=None):
        if action not in ('custom', 'noop'):
            self.store.set('ui_input', None)
        if action == 'noop':
            return
        if action == 'home':
            self.store.set('ui_input', None)
            await self.ui_home(mid)
        elif action.startswith('models:'):
            await self.ui_models(int(action.split(':')[1]), mid)
        elif action == 'refresh':
            await self.ui_models(mid=mid, refresh=True)
        elif action.startswith(('model:', 'choose:')):
            kind, key = action.split(':', 1)
            await self.ui_model(key, mid, kind == 'choose')
        elif action == 'settings':
            await self.ui_settings(mid)
        elif action == 'stats':
            await self.ui_stats(mid)
        elif action == 'new':
            await self.card('＋ <b>شروعی تازه؟</b>\n\nحافظهٔ فعال پاک می‌شود. مدل و تنظیمات شما حفظ می‌شوند.\nاگر گفتگو را لازم دارید، ابتدا خروجی بگیرید.',
                            [[button('📤 دریافت گفتگو', 'u:export')],
                             [button('✓ شروع گفتگوی تازه', 'u:new:yes'), button('انصراف', 'u:home')]], mid)
        elif action == 'new:yes':
            for key, value in [('history', []), ('raw', None), ('ui_input', None)]:
                self.store.set(key, value)
            await self.card('✦ <b>صفحهٔ تازه آماده است</b>\n\nاز چه چیزی شروع کنیم؟ پیام خود را بنویسید.', self.back(), mid)
        elif action == 'tokens':
            await self.card('📏 <b>سقف طول پاسخ</b>\n\nاین عدد حداکثر توکن خروجی است، نه طول تضمینی پاسخ. توکن‌های استدلال هم ممکن است از این سقف مصرف شوند.',
                            [[button('کوتاه · 1024', 'u:token:1024'), button('معمولی · 4096', 'u:token:4096')],
                             [button('بلند · 8192', 'u:token:8192'), button('بیشترین · 16384', 'u:token:16384')],
                             [button('‹ تنظیمات', 'u:settings')]], mid)
        elif action.startswith('token:'):
            value = int(action.split(':')[1])
            if value in (1024, 4096, 8192, 16384):
                self.store.set('tokens', value)
            await self.ui_settings(mid)
        elif action == 'reasoning':
            await self.card('🧠 <b>شدت استدلال</b>\n\nتنظیم بیشتر ممکن است زمان و هزینه را افزایش دهد. «پیش‌فرض مدل» یعنی پارامتر اضافی ارسال نشود.',
                            [[button('پیش‌فرض مدل', 'u:reason:off'), button('کم', 'u:reason:low')],
                             [button('متوسط', 'u:reason:medium'), button('زیاد', 'u:reason:high')],
                             [button('‹ تنظیمات', 'u:settings')]], mid)
        elif action.startswith('reason:'):
            value = action.split(':')[1]
            if value in ('off', 'low', 'medium', 'high'):
                self.store.set('reasoning', value)
            await self.ui_settings(mid)
        elif action == 'json':
            self.store.set('json', not self.store.get('json', False))
            await self.ui_settings(mid)
        elif action == 'personas':
            await self.ui_personas(mid)
        elif action.startswith('persona:'):
            roles = {'general': self.system,
                     'coder': 'شما برنامه‌نویس ارشد هستید. به فارسی راهکار عملی، کد دقیق و توضیح روشن ارائه کنید.',
                     'teacher': 'شما معلم صبور هستید. به فارسی با مثال و مرحله‌به‌مرحله آموزش دهید.',
                     'writer': 'شما ویراستار و نویسندهٔ فارسی هستید. متن روان و متناسب با درخواست بنویسید.',
                     'translator': 'شما مترجم دقیق هستید. متن را با حفظ معنا و لحن به زبان درخواستی ترجمه کنید؛ اگر مشخص نبود زبان مقصد را بپرسید.',
                     'brief': 'به فارسی، کوتاه، روشن و مستقیم پاسخ دهید. جزئیات را فقط در صورت نیاز اضافه کنید.'}
            role = roles.get(action.split(':')[1])
            if role:
                self.store.set('system', role)
                await self.card('✅ <b>شخصیت دستیار تغییر کرد</b>\n\n' + esc(role), self.back(), mid)
        elif action == 'custom':
            self.store.set('ui_input', 'system')
            await self.card('✎ <b>دستیار شما چه شخصیتی داشته باشد؟</b>\n\nدستور دلخواه را در پیام بعدی بفرستید. این پیام به عنوان تنظیمات ذخیره می‌شود و درخواست پولی نیست.',
                            [[button('انصراف', 'u:home')]], mid)
        elif action == 'files':
            await self.card('📎 <b>عکس، سند، کد</b>\n━━━━━━━━━━━━━━━━━━\n'
                            '◉ <b>عکس:</b> از گیرهٔ تلگرام ارسال کنید و سؤال را در کپشن بنویسید؛ مدل تصویری لازم است.\n\n'
                            '▤ <b>فایل:</b> PDF متنی، متن و کد تا ۸ مگابایت؛ حداکثر ۱۰۰ هزار نویسه.\n\n'
                            'PDF اسکن‌شده را به شکل عکس صفحه بفرستید. عکس‌ها را تکی ارسال کنید.\n'
                            'تولید تصویر و تبدیل صدا در سرویس پشتیبانی نشده است.', self.back(), mid)
        elif action == 'advanced':
            await self.card('🧩 <b>دسترسی پیشرفته به API</b>\n\nقالب را انتخاب کنید، سپس فایل JSON کامل درخواست را بفرستید. پاسخ به صورت فایل JSON است. اجرای ابزارها خودکار نیست.',
                            [[button('Chat Completions', 'u:raw:chat'), button('Messages', 'u:raw:messages')],
                             [button('Responses', 'u:raw:responses'), button('شمارش توکن', 'u:raw:count')],
                             [button('خروج از حالت پیشرفته', 'u:cancel')], [button('⌂ خانه', 'u:home')]], mid)
        elif action.startswith('raw:'):
            self.store.set('ui_input', None)
            await self.command('/raw ' + action.split(':')[1])
        elif action == 'cancel':
            self.store.set('ui_input', None)
            self.store.set('raw', None)
            await self.ui_home(mid)
        elif action == 'export':
            await self.command('/export')
        elif action == 'help':
            await self.card('❔ <b>شروع در سه قدم</b>\n━━━━━━━━━━━━━━━━━━\n'
                            '۱ · از «انتخاب مدل» یک مدل انتخاب کنید.\n'
                            '۲ · سؤال را بنویسید یا عکس و فایل بفرستید.\n'
                            '۳ · برای موضوع جدید «گفتگوی تازه» را بزنید.\n\n'
                            'همیشه با /menu به خانه برگردید.\n'
                            'راهنمای کامل دستورها: /help\n\n'
                            '<b>حریم خصوصی</b>\nفقط حساب شما به ربات دسترسی دارد. گفتگو روی سرور ذخیره و برای پاسخ به API ارسال می‌شود.', self.back(), mid)

    async def ui_answer(self, text):
        # Chunk before formatting; every chunk has independently balanced HTML tags.
        chunks = []
        while text:
            end = min(1800, len(text))
            if end < len(text):
                boundary = text.rfind('\n', 0, end)
                if boundary > 800:
                    end = boundary + 1
            chunks.append(text[:end])
            text = text[end:]
        for chunk in chunks:
            await self.tg('sendMessage', {'chat_id': self.owner, 'text': rich_text(chunk), 'parse_mode': 'HTML',
                                         'link_preview_options': {'is_disabled': True}})

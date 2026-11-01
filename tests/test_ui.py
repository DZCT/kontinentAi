import copy
import hashlib
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from bot import Bot, Store
from ui import model_key, rich_text


class UIFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'KONTINENT_API_KEY': 'test',
                                     'TELEGRAM_OWNER_ID': '123', 'DATABASE_PATH': self.temp.name + '/db'}):
            self.bot = Bot()
        self.bot.tg = AsyncMock()
        self.bot.document = AsyncMock()
        self.bot.request = AsyncMock()
        self.bot.catalog = [{'id': f'model-{i}', 'input_modalities': ['text', 'image'],
                             'endpoints': ['/v1/chat/completions'], 'pricing': {'usd_per_million_output': 1}}
                            for i in range(14)]
        self.bot.models = AsyncMock(return_value=self.bot.catalog)

    async def asyncTearDown(self):
        await self.bot.client.aclose()
        self.temp.cleanup()

    def check_cards(self):
        for call in self.bot.tg.call_args_list:
            method, body = call.args
            if method not in ('sendMessage', 'editMessageText'):
                continue
            if body.get('parse_mode') == 'HTML':
                ET.fromstring('<root>' + body['text'] + '</root>')
            for row in body.get('reply_markup', {}).get('inline_keyboard', []):
                for b in row:
                    if 'callback_data' in b:
                        self.assertLessEqual(len(b['callback_data'].encode()), 64)

    async def test_navigation_edits_card_without_billable_calls(self):
        for action in ('home', 'models:0', 'models:1', 'models:2', 'settings', 'stats', 'personas',
                       'files', 'advanced', 'help', 'tokens', 'reasoning'):
            await self.bot.ui_action(action, 42)
        self.bot.request.assert_not_awaited()
        self.assertTrue(all(c.args[0] == 'editMessageText' for c in self.bot.tg.call_args_list))
        self.check_cards()

    async def test_model_identity_survives_catalog_reorder(self):
        await self.bot.ui_models()
        key = model_key('model-7')
        self.bot.catalog.reverse()
        await self.bot.ui_action('choose:' + key, 42)
        self.assertEqual(self.bot.store.get('model'), 'model-7')
        self.check_cards()

    async def test_new_requires_confirmation_and_keeps_preferences(self):
        history = [{'role': 'user', 'content': 'hello'}]
        self.bot.store.set('history', history)
        self.bot.store.set('model', 'model-7')
        await self.bot.command('/new')
        self.assertEqual(self.bot.store.get('history'), history)
        await self.bot.ui_action('new:yes', 42)
        self.assertEqual(self.bot.store.get('history'), [])
        self.assertEqual(self.bot.store.get('model'), 'model-7')

    async def test_settings_persist_and_personality_does_not_spend(self):
        for action in ('token:8192', 'reason:low', 'json', 'persona:coder', 'custom'):
            await self.bot.ui_action(action, 42)
        await self.bot.handle({'message': {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                                          'text': '<script> & personal instruction'}})
        self.assertEqual(self.bot.store.get('tokens'), 8192)
        self.assertEqual(self.bot.store.get('reasoning'), 'low')
        self.assertTrue(self.bot.store.get('json'))
        self.assertEqual(self.bot.store.get('system'), '<script> & personal instruction')
        self.assertIsNone(self.bot.store.get('ui_input'))
        self.bot.request.assert_not_awaited()
        self.check_cards()

    async def test_unauthorized_callback_cannot_change_settings(self):
        await self.bot.handle({'callback_query': {'id': 'c', 'from': {'id': 999}, 'data': 'u:json',
                                                 'message': {'message_id': 42, 'chat': {'id': 123, 'type': 'private'}}}})
        self.assertFalse(self.bot.store.get('json', False))
        self.bot.tg.assert_not_awaited()

    async def test_old_model_buttons_still_work(self):
        self.bot.store.set('catalog_ids', ['model-3'])
        await self.bot.handle({'callback_query': {'id': 'c', 'from': {'id': 123}, 'data': 'm:0',
                                                 'message': {'message_id': 42, 'chat': {'id': 123, 'type': 'private'}}}})
        self.assertEqual(self.bot.store.get('model'), 'model-3')

    async def test_start_preserves_history_and_adds_keyboard(self):
        self.bot.store.set('history', ['unchanged'])
        await self.bot.command('/start')
        first = self.bot.tg.call_args_list[0].args[1]
        self.assertIn('keyboard', first['reply_markup'])
        self.assertEqual(self.bot.store.get('history'), ['unchanged'])
        self.bot.request.assert_not_awaited()

    async def test_answer_formats_code_and_escapes_html(self):
        await self.bot.ui_answer('**عنوان**\n```python\nprint("<tag>")\n```\nمتن <script> & سلام')
        self.check_cards()
        value = self.bot.tg.call_args.args[1]['text']
        self.assertIn('<pre>', value)
        self.assertIn('&lt;script&gt;', value)
        self.assertNotIn('<script>', value)

    async def test_same_card_tap_is_harmless(self):
        req = httpx.Request('POST', 'https://api.telegram.org/test')
        response = httpx.Response(400, request=req, text='Bad Request: message is not modified')
        self.bot.tg = AsyncMock(side_effect=httpx.HTTPStatusError('bad', request=req, response=response))
        await self.bot.ui_home(42)
        self.bot.tg.assert_awaited_once()


class ProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_uncharged_provider_error_clears_lock(self):
        for message, locked in [('Nothing was charged. Retry in a moment.', False), ('Upstream failed', True)]:
            with tempfile.TemporaryDirectory() as d:
                with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'KONTINENT_API_KEY': 'test',
                                             'TELEGRAM_OWNER_ID': '123', 'DATABASE_PATH': d + '/db'}):
                    bot = Bot()
                await bot.client.aclose()
                bot.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502, json={
                    'error': {'code': 'PROVIDER_ERROR', 'message': message}})))
                with self.assertRaises(ValueError):
                    await bot.request('chat/completions', {})
                self.assertEqual(bot.store.get('uncertain'), locked)
                await bot.client.aclose()


import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from bot import Bot, Store


class StorageTests(unittest.TestCase):
    def test_accounting_is_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = d + '/state.db'
            s = Store(path)
            s.record('a', {'kontinent': {'cost_micros': 500}, 'usage': {'input_tokens': 10, 'output_tokens': 3}})
            s.record('a', {'kontinent': {'cost_micros': 500}})
            s.record('b', {})
            s.set('offset', 123)
            reopened = Store(path)
            self.assertEqual(reopened.get('offset'), 123)
            self.assertEqual(reopened.totals(), (500, 2, 13, 1))
            self.assertEqual(reopened.totals(True), reopened.totals())


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test', 'KONTINENT_API_KEY': 'test',
                                     'TELEGRAM_OWNER_ID': '123', 'DATABASE_PATH': self.temp.name + '/db'}):
            self.bot = Bot()
        self.bot.say = AsyncMock()
        self.bot.tg = AsyncMock()
        self.bot.document = AsyncMock()
        self.bot.catalog = [{'id': 'test-model', 'input_modalities': ['text'],
                             'endpoints': ['/v1/chat/completions', '/v1/messages']}]
        self.bot.store.set('model', 'test-model')

    async def asyncTearDown(self):
        await self.bot.client.aclose()
        self.temp.cleanup()

    async def test_other_users_and_groups_cannot_trigger_work(self):
        self.bot.chat = AsyncMock()
        for sender, chat, kind in [(999, 123, 'private'), (123, -12, 'group'), (999, 999, 'private')]:
            await self.bot.handle({'message': {'from': {'id': sender}, 'chat': {'id': chat, 'type': kind}, 'text': 'hi'}})
        self.bot.chat.assert_not_awaited()
        self.bot.say.assert_not_awaited()

    async def test_image_rejected_before_billable_call(self):
        self.bot.request = AsyncMock()
        with self.assertRaises(ValueError):
            await self.bot.chat([{'type': 'image_url', 'image_url': {'url': 'data:test'}}])
        self.bot.request.assert_not_awaited()

    async def test_failed_request_does_not_pollute_history(self):
        self.bot.request = AsyncMock(side_effect=ValueError('error'))
        with self.assertRaises(ValueError):
            await self.bot.chat('hello')
        self.assertEqual(self.bot.store.get('history', []), [])

    async def test_success_preserves_complete_turn_and_json_mode(self):
        self.bot.store.set('json', True)
        self.bot.request = AsyncMock(return_value={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                                                   'kontinent': {'cost_usd': 0.001}})
        await self.bot.chat('hello')
        self.assertEqual(len(self.bot.store.get('history')), 2)
        body = self.bot.request.call_args.args[1]
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertIn('JSON', body['messages'][0]['content'])

    async def test_raw_rejects_stream_and_excess_output(self):
        self.bot.request = AsyncMock()
        for extra in [{'stream': True}, {'max_tokens': 999999}]:
            with self.assertRaises(ValueError):
                await self.bot.raw('chat/completions', json.dumps({'model': 'test-model', **extra}))
        self.bot.request.assert_not_awaited()

    async def test_count_is_free_and_raw_response_is_exported(self):
        self.bot.store.set('uncertain', True)
        self.bot.request = AsyncMock(return_value={'input_tokens': 10})
        await self.bot.raw('messages/count_tokens', '{"model":"test-model","messages":[]}')
        self.bot.document.assert_awaited_once()

    async def test_request_accounting_and_uncertainty(self):
        async def transport(request):
            self.assertIn('idempotency-key', request.headers)
            return httpx.Response(200, json={'model': 'test-model', 'kontinent': {'cost_micros': 100}, 'usage': {'total_tokens': 3}})
        await self.bot.client.aclose()
        self.bot.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        await self.bot.request('chat/completions', {})
        self.assertEqual(self.bot.store.totals(), (100, 1, 3, 0))
        self.assertFalse(self.bot.store.get('uncertain'))

    async def test_timeout_blocks_future_spend_without_retry(self):
        attempts = []
        async def transport(request):
            attempts.append(request)
            raise httpx.ReadTimeout('timeout')
        await self.bot.client.aclose()
        self.bot.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        with self.assertRaises(ValueError):
            await self.bot.request('chat/completions', {})
        self.assertTrue(self.bot.store.get('uncertain'))
        with self.assertRaises(ValueError):
            await self.bot.request('chat/completions', {})
        self.assertEqual(len(attempts), 1)

    async def test_rejected_request_unlocks_and_budget_stops_calls(self):
        await self.bot.client.aclose()
        self.bot.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(402, json={'error': {'code': 'INSUFFICIENT_BALANCE'}})))
        with self.assertRaises(ValueError):
            await self.bot.request('chat/completions', {})
        self.assertFalse(self.bot.store.get('uncertain'))
        self.bot.store.record('budget', {'kontinent': {'cost_micros': self.bot.daily}})
        with self.assertRaises(ValueError):
            self.bot.budget()


if __name__ == '__main__':
    unittest.main()

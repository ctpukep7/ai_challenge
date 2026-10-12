import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app import Config, SYSTEM, WindowLimiter, create_app

TOKEN = '1' * 64
AUTH = {'Authorization': 'Bearer ' + TOKEN}

class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.now = 100.
        async def fake(request):
            self.calls.append(request)
            if request.url.path == '/api/tags':
                return httpx.Response(200, json={'models': [{'name': Config(TOKEN).model}]})
            if request.url.path == '/api/version':
                return httpx.Response(200, json={'version': 'test'})
            return httpx.Response(200, json={'message': {'content': 'Ответ'}, 'done': True, 'done_reason': 'stop',
                    'load_duration': 0, 'eval_duration': 1000000000, 'eval_count': 10, 'prompt_eval_count': 20})
        self.fake = fake
        self.app = create_app(Config(TOKEN), transport=httpx.MockTransport(fake), clock=lambda: self.now)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://local')
    async def asyncTearDown(self):
        await self.client.aclose()
    async def post(self, payload=None, **kwargs):
        return await self.client.post('/api/chat', headers=AUTH, json=payload or {'prompt': 'Привет'}, **kwargs)
    async def test_auth_all_api_and_no_docs(self):
        for path in ['/api/health','/api/limits','/api/chat','/api/unknown']:
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(TOKEN, response.text)
        self.assertFalse(self.calls)
        self.assertIsNone(self.app.openapi_url)
    async def test_limits_health_headers(self):
        response = await self.client.get('/api/limits', headers=AUTH)
        self.assertEqual(response.json()['max_input_bytes'], 6000)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        self.assertNotIn('access-control-allow-origin', response.headers)
        self.assertEqual((await self.client.get('/api/health', headers=AUTH)).status_code, 200)
    async def test_history_payload_metrics(self):
        response = await self.post({'prompt': 'Продолжи', 'history': [{'user': 'Вопрос', 'assistant': 'Ответ'}]})
        self.assertEqual(response.status_code, 200)
        sent = json.loads(self.calls[0].content)
        self.assertEqual([m['role'] for m in sent['messages']], ['system','user','assistant','user'])
        self.assertEqual(sent['options'], {'temperature': .7, 'num_ctx':4096,'num_predict':1024})
        self.assertFalse(sent['stream'])
        self.assertFalse(sent['think'])
        self.assertEqual(str(self.calls[0].url), 'http://127.0.0.1:11434/api/chat')
        metrics = response.json()['metrics']
        self.assertEqual(metrics['total_tokens'], 30)
        self.assertEqual(metrics['tokens_per_second'], 10)
        self.assertEqual(metrics['load_seconds'], 0)
    async def test_validation_no_echo(self):
        for payload in [{'prompt':''}, {'prompt':'a','secret':'never-echo'}, {'prompt':123}, {'prompt':'a','history':[{'user':'x','assistant':'y','extra':1}]}]:
            response = await self.post(payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('never-echo', response.text)
        self.assertFalse(self.calls)
    async def test_input_byte_limit_includes_system(self):
        exact = 6000-len(SYSTEM.encode())
        self.assertEqual((await self.post({'prompt':'a'*exact})).status_code, 200)
        self.assertEqual((await self.post({'prompt':'a'*(exact+1)})).status_code, 413)
        self.assertEqual((await self.post({'prompt':'я'*3000})).status_code, 413)
        self.assertEqual(len(self.calls), 1)
    async def test_history_limit(self):
        response = await self.post({'prompt':'x','history':[{'user':'a','assistant':'b'}]*5})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.calls)
    async def test_body_cap_without_content_length(self):
        async def chunks():
            for _ in range(4):
                yield b'a'*10000
        response = await self.client.post('/api/chat', content=chunks(), headers=AUTH)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.calls)
    async def test_body_cap_does_not_trust_content_length(self):
        response = await self.client.post('/api/chat',content=b'x'*40000,headers={**AUTH,'Content-Length':'1'})
        self.assertEqual(response.status_code,413)
        self.assertFalse(self.calls)
    async def test_deep_json_safe_error(self):
        response = await self.client.post('/api/chat',content=b'['*2000+b'0'+b']'*2000,headers=AUTH)
        self.assertEqual(response.status_code,422)
    async def test_bad_json(self):
        response = await self.client.post('/api/chat', content=b'{secret', headers=AUTH)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('secret', response.text)
    async def test_chat_rate_shared_and_reset(self):
        for _ in range(6):
            self.assertEqual((await self.post()).status_code, 200)
        response = await self.post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '60')
        self.now += 60
        self.assertEqual((await self.post()).status_code, 200)
    async def test_failed_auth_cannot_evict_chat_rate(self):
        for _ in range(6): await self.post()
        for i in range(1100): self.app.state.limiter.take(('auth',str(i)),30)
        self.assertEqual((await self.post()).status_code,429)
    async def test_extreme_ratio_null(self):
        payload = {'done':True,'message':{'content':'x'},'eval_count':1e308,'eval_duration':1,'prompt_eval_count':1e308}
        app = create_app(Config(TOKEN),transport=httpx.MockTransport(lambda request: httpx.Response(200,json=payload)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://local') as client:
            response = await client.post('/api/chat',json={'prompt':'x'},headers=AUTH)
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()['metrics']['tokens_per_second'])
        self.assertIsNone(response.json()['metrics']['total_tokens'])
    async def test_auth_rate_ignores_forwarded_headers(self):
        for i in range(30):
            response = await self.client.get('/api/limits', headers={'X-Forwarded-For': str(i)})
            self.assertEqual(response.status_code, 401)
        self.assertEqual((await self.client.get('/api/limits')).status_code, 429)
        self.assertEqual((await self.client.get('/api/limits', headers=AUTH)).status_code, 200)
    async def test_no_queue_and_release(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(request):
            entered.set()
            await release.wait()
            return await self.fake(request)
        app = create_app(Config(TOKEN), transport=httpx.MockTransport(blocked))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://local') as client:
            pending = asyncio.create_task(client.post('/api/chat', json={'prompt':'x'}, headers=AUTH))
            await entered.wait()
            self.assertEqual((await client.post('/api/chat', json={'prompt':'x'}, headers=AUTH)).status_code, 409)
            release.set()
            self.assertEqual((await pending).status_code, 200)
            self.assertFalse(app.state.slot.locked())
    async def test_upstream_errors_and_no_retry(self):
        failures = [httpx.ReadTimeout('secret'), httpx.ConnectError('secret'), httpx.Response(500, text='secret'), httpx.Response(200, text='not-json'), httpx.Response(200, json={'message':{'content':''}})]
        for item in failures:
            calls = []
            async def failure(request):
                calls.append(request)
                if isinstance(item, Exception): raise item
                return item
            app = create_app(Config(TOKEN), transport=httpx.MockTransport(failure))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://local') as client:
                response = await client.post('/api/chat', json={'prompt':'x'}, headers=AUTH)
            self.assertEqual(response.status_code, 504 if isinstance(item,httpx.ReadTimeout) else 502)
            self.assertNotIn('secret', response.text)
            self.assertEqual(len(calls), 1)
            self.assertFalse(app.state.slot.locked())
    async def test_missing_metrics_are_null_and_truncated(self):
        app = create_app(Config(TOKEN), transport=httpx.MockTransport(lambda request: httpx.Response(200,json={'message':{'content':'x'},'done':True,'done_reason':'length'})))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://local') as client:
            data = (await client.post('/api/chat',json={'prompt':'x'},headers=AUTH)).json()
        self.assertTrue(data['truncated'])
        self.assertIsNone(data['metrics']['total_tokens'])
        self.assertIsNone(data['metrics']['tokens_per_second'])
    async def test_surrogate_invalid_unicode(self):
        response = await self.client.post('/api/chat',content=b'{"prompt":"\\ud800"}', headers=AUTH)
        self.assertEqual(response.status_code,422)
        self.assertFalse(self.calls)
    async def test_malformed_upstream_shapes(self):
        for payload in [[], None, {'done':False,'message':{'content':'x'}}, {'done':True,'message':[]}]:
            app = create_app(Config(TOKEN), transport=httpx.MockTransport(lambda request: httpx.Response(200,json=payload)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://local') as client:
                response = await client.post('/api/chat',json={'prompt':'x'},headers=AUTH)
            self.assertEqual(response.status_code,502)
            self.assertIn('code',response.json()['error'])
    async def test_huge_metrics_safe_null(self):
        payload = {'done':True,'message':{'content':'x'},'eval_count':10**400}
        app = create_app(Config(TOKEN),transport=httpx.MockTransport(lambda request: httpx.Response(200,json=payload)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://local') as client:
            response = await client.post('/api/chat',json={'prompt':'x'},headers=AUTH)
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()['metrics']['output_tokens'])
    async def test_health_malformed(self):
        for payload in [[], {'models':None}, {'models':[None,2]}]:
            app = create_app(Config(TOKEN),transport=httpx.MockTransport(lambda request: httpx.Response(200,json=payload)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://local') as client:
                response = await client.get('/api/health',headers=AUTH)
            self.assertEqual(response.status_code,503)
    async def test_unknown_api_not_spa(self):
        self.assertEqual((await self.client.get('/api/unknown',headers=AUTH)).status_code,404)

class ConfigTests(unittest.TestCase):
    def test_token_permissions_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory)/'token'
            with self.assertRaises(RuntimeError): Config.from_file(token)
            token.write_text(TOKEN)
            token.chmod(0o600)
            self.assertEqual(Config.from_file(token).token,TOKEN)
            token.chmod(0o644)
            with self.assertRaises(RuntimeError): Config.from_file(token)
            token.chmod(0o600)
            token.write_text('weak')
            with self.assertRaises(RuntimeError): Config.from_file(token)
    def test_limiter_bounded_and_expiry(self):
        now = [0]
        limiter = WindowLimiter(lambda: now[0],bound=3)
        for i in range(10): limiter.take(i,1)
        self.assertEqual(len(limiter.entries),3)
        self.assertEqual(limiter.take(9,1),60)
        now[0] = 61
        self.assertEqual(limiter.take(9,1),0)
        self.assertEqual(len(limiter.entries),1)

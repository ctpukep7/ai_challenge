import io
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from client import NoRedirect, SafeClientError, request, validate_url

class ClientTests(unittest.TestCase):
    def test_url_validation(self):
        for url in ['http://192.168.0.171:5029','https://token@localhost','https://localhost?token=secret','https://localhost/#x','https://localhost/api','https://localhost:bad','https://localhost:0','https://localhost bad']:
            with self.subTest(url=url),self.assertRaises(SafeClientError): validate_url(url)
        self.assertEqual(validate_url('https://192.168.0.171:5029/'),'https://192.168.0.171:5029')
        self.assertEqual(validate_url('https://[::1]:5029'),'https://[::1]:5029')
    def test_request_config_and_no_proxy_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'token';token.write_text('a'*64)
            cert=Path(directory)/'cert'
            calls=[]
            context=object()
            def context_factory(**kwargs):
                self.assertEqual(kwargs,{'cafile':str(cert)})
                return context
            class Opener:
                def open(self,req,timeout):
                    calls.append((req,timeout))
                    return io.BytesIO(b'{"status":"ok"}')
            def opener_factory(*handlers):
                self.assertTrue(any(isinstance(h,ProxyHandler) and h.proxies=={} for h in handlers))
                self.assertTrue(any(isinstance(h,HTTPSHandler) and h._context is context for h in handlers))
                self.assertTrue(any(isinstance(h,NoRedirect) for h in handlers))
                return Opener()
            result=request('https://192.168.0.171:5029',cert,token,'ask','Привет',opener_factory=opener_factory,context_factory=context_factory)
            self.assertEqual(result,{'status':'ok'})
            req,timeout=calls[0]
            self.assertEqual(timeout,125)
            self.assertEqual(req.full_url,'https://192.168.0.171:5029/api/chat')
            self.assertEqual(req.method,'POST')
            self.assertIn(b'"history": []',req.data)
    def test_errors_never_echo_http_body(self):
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'token';token.write_text('a'*64)
            class Opener:
                def open(self,req,timeout):
                    raise HTTPError(req.full_url,401,'SECRET',{},io.BytesIO(b'SECRET'))
            with self.assertRaises(SafeClientError) as caught:
                request('https://localhost',Path(directory)/'cert',token,'health',opener_factory=lambda *args:Opener(),context_factory=lambda **kwargs:object())
            self.assertNotIn('SECRET',str(caught.exception))
    def test_redirect_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://cloud.example'))
    def test_token_file_required(self):
        with self.assertRaises(SafeClientError): request('https://localhost','cert','/no-such-token','health')

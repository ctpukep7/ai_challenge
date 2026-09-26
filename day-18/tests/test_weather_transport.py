"""Only weather GET transport failures can retry; bad observations cannot."""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from backend import weather

JOB = {'latitude':55.,'longitude':73.}


def body(operation):
    if operation == 'resolve':
        return {'results':[{'name':'Омск','latitude':55.,'longitude':73.,'country':'Россия','timezone':'Asia/Omsk'}]}
    return {'utc_offset_seconds':0,
            'current_units':{'temperature_2m':'°C','relative_humidity_2m':'%','wind_speed_10m':'km/h'},
            'current':{'temperature_2m':8.,'relative_humidity_2m':65.,'wind_speed_10m':12.,'weather_code':3,
                       'time':datetime.now(timezone.utc).isoformat()}}


async def collect(operation):
    return await weather.resolve('Омск') if operation == 'resolve' else await weather.current_weather(JOB)


class Transport(httpx.AsyncBaseTransport):
    def __init__(self,handler):
        self.handler = handler
        self.calls = 0
        self.closed = False
        self.urls = []
    async def handle_async_request(self,request):
        self.calls += 1
        self.urls.append(str(request.url))
        return await self.handler(request,self.calls)
    async def aclose(self):
        self.closed = True


def install_client(monkeypatch,handler):
    actual = httpx.AsyncClient
    clients,transports,timeouts = [],[],[]
    def factory(**kwargs):
        timeouts.append(kwargs['timeout'])
        transport = Transport(handler)
        client = actual(**kwargs,transport=transport)
        clients.append(client)
        transports.append(transport)
        return client
    monkeypatch.setattr(weather.httpx,'AsyncClient',factory)
    return clients,transports,timeouts


@pytest.mark.parametrize('operation',['resolve','current'])
@pytest.mark.parametrize('failure',['connect','timeout','503'])
def test_transient_get_retries_once_and_reuses_client(operation,failure,monkeypatch):
    async def handler(request,number):
        if number == 1:
            if failure == 'connect':
                raise httpx.ConnectError('SECRET',request=request)
            if failure == 'timeout':
                raise httpx.ReadTimeout('SECRET',request=request)
            return httpx.Response(503,request=request,text='SECRET')
        return httpx.Response(200,request=request,json=body(operation))
    clients,transports,timeouts = install_client(monkeypatch,handler)
    result = asyncio.run(collect(operation))
    assert result
    assert len(clients) == 1 and clients[0].is_closed and transports[0].closed
    assert transports[0].calls == 2 and transports[0].urls[0] == transports[0].urls[1]
    assert timeouts[0].connect == 5 and timeouts[0].read == 8


@pytest.mark.parametrize('operation',['resolve','current'])
@pytest.mark.parametrize('failure',['connect','timeout','503'])
def test_repeated_transport_failure_is_bounded(operation,failure,monkeypatch):
    async def handler(request,number):
        if failure == 'connect':
            raise httpx.ConnectError('SECRET',request=request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('SECRET',request=request)
        return httpx.Response(503,request=request,text='SECRET')
    clients,transports,_ = install_client(monkeypatch,handler)
    with pytest.raises(ValueError) as exc:
        asyncio.run(collect(operation))
    assert transports[0].calls == 2
    assert clients[0].is_closed and transports[0].closed
    assert 'SECRET' not in str(exc.value)


@pytest.mark.parametrize('operation',['resolve','current'])
@pytest.mark.parametrize('failure',[400,401,429,'json','data'])
def test_permanent_or_malformed_results_are_not_retried(operation,failure,monkeypatch):
    async def handler(request,number):
        if isinstance(failure,int):
            return httpx.Response(failure,request=request,text='SECRET')
        if failure == 'json':
            return httpx.Response(200,request=request,text='SECRET not-json')
        malformed = body(operation)
        if operation == 'resolve':
            malformed['results'][0]['latitude'] = 999
        else:
            malformed['current_units']['temperature_2m'] = 'fahrenheit'
        return httpx.Response(200,request=request,json=malformed)
    clients,transports,_ = install_client(monkeypatch,handler)
    with pytest.raises(ValueError) as exc:
        asyncio.run(collect(operation))
    assert transports[0].calls == 1
    assert clients[0].is_closed and transports[0].closed
    assert 'SECRET' not in str(exc.value)


@pytest.mark.parametrize('operation',['resolve','current'])
def test_weather_deadline_covers_both_get_attempts(operation,monkeypatch):
    actual_timeout = asyncio.timeout
    limits = []
    def brief_timeout(seconds):
        limits.append(seconds)
        return actual_timeout(0.02)
    monkeypatch.setattr(weather.asyncio,'timeout',brief_timeout)
    async def handler(request,number):
        if number == 1:
            raise httpx.ConnectError('first connection failed',request=request)
        await asyncio.Event().wait()
    clients,transports,_ = install_client(monkeypatch,handler)
    with pytest.raises(ValueError):
        asyncio.run(collect(operation))
    assert limits == [15] and transports[0].calls == 2
    assert clients[0].is_closed and transports[0].closed


def test_weather_cancellation_closes_client_without_retry(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        async def handler(request,number):
            entered.set()
            await asyncio.Event().wait()
        clients,transports,_ = install_client(monkeypatch,handler)
        task = asyncio.create_task(weather.current_weather(JOB))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transports[0].calls == 1 and clients[0].is_closed and transports[0].closed
    asyncio.run(scenario())

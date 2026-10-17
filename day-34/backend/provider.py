"""Day34 DeepSeek function calling; no project code is executed."""
import asyncio
import os
import httpx
from assistant_core import ProviderError

class DeepSeek:
    def __init__(self, api_key=None, model=None):
        self.api_key = api_key if api_key is not None else os.getenv('DEEPSEEK_API_KEY', '')
        self.model = model or os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')

    async def complete(self, messages, tools):
        if not self.api_key:
            raise ProviderError('Задайте DEEPSEEK_API_KEY в локальном .env')
        try:
            async with asyncio.timeout(95), httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False) as client:
                response = await client.post('https://api.deepseek.com/chat/completions', headers={'Authorization': f'Bearer {self.api_key}'}, json={
                    'model': self.model, 'messages': messages,
                    **({'tools': tools, 'tool_choice': 'auto'} if tools else {'tool_choice': 'none'}),
                    'response_format': {'type': 'json_object'}, 'max_tokens': 16000 if tools else 4000,
                    'temperature': .1, 'thinking': {'type': 'disabled'}})
                response.raise_for_status()
                data = response.json()
                choice = data['choices'][0]
                if choice.get('finish_reason') not in {'stop', 'tool_calls'}:
                    raise ProviderError('Ответ DeepSeek обрезан или не завершён; результат не опубликован')
                message = choice['message']
                if message.get('role') != 'assistant':
                    raise ProviderError('Неверное сообщение DeepSeek')
                # Preserve the actual assistant tool_calls and matching IDs, no reconstruction.
                return {'message': message, 'usage': data.get('usage')}
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in {401, 403}:
                message = 'DeepSeek отклонил доступ: проверьте DEEPSEEK_API_KEY и права модели'
            elif code == 402:
                message = 'DeepSeek: недостаточно средств на счёте провайдера'
            elif code == 429:
                message = 'DeepSeek: превышен лимит запросов; повторите вручную позже'
            else:
                message = f'DeepSeek вернул HTTP {code}; повторите вручную позже'
            raise ProviderError(message) from None
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError('Истекло время ожидания DeepSeek; повторите запрос вручную') from None
        except httpx.HTTPError:
            raise ProviderError('DeepSeek недоступен: не удалось установить соединение') from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError('DeepSeek вернул некорректный формат ответа') from None

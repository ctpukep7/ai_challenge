"""Three distinct processes expose only their own tools. No dotenv imports."""
import os
import sys
import functools
from mcp.server.fastmcp import FastMCP
from backend.errors import DomainError
from backend.store import Store
from backend.services import Service
from backend.providers import WeatherProvider, CalendarProvider, FixtureProvider


def main():
    server_id=sys.argv[1]
    fixture=os.getenv('DAY20_FIXTURE_PATH','')
    provider=FixtureProvider(fixture) if fixture else None
    service=Service(Store(os.environ['DAY20_DB_PATH']),weather=provider or WeatherProvider(),calendar=provider or CalendarProvider(os.getenv('YANDEX_CALDAV_USERNAME',''),os.getenv('YANDEX_CALDAV_APP_PASSWORD',''),os.getenv('YANDEX_CALDAV_URL','https://caldav.yandex.ru')),timezone=os.getenv('TIMEZONE','Asia/Omsk'))
    mcp=FastMCP('Day20 '+server_id)
    names={'weather':['resolve_city','get_hourly_forecast'],'calendar':['list_calendars','find_free_slots','create_trip_event'],'plans':['save_trip_draft','get_trip_plan']}[server_id]
    descriptions={
        'resolve_city':'Найти города по названию. При нескольких совпадениях уточнить выбор пользователя.',
        'get_hourly_forecast':'Почасовой прогноз для обнаруженного города и диапазона с часовым поясом. Только после resolve_city.',
        'list_calendars':'Обнаружить доступные календари. Несколько календарей требуют ответа пользователя в чате полным именем или ID; selected_calendar_id показывает проверенный выбор.',
        'find_free_slots':'Свободные интервалы без раскрытия личных встреч. Только после list_calendars.',
        'save_trip_draft':'Сохранить проект с настоящими forecast_id и availability_id этого запуска. Проверяет погоду каждого часа и свободный интервал.',
        'get_trip_plan':'Прочитать ранее сохранённый проект этого запуска.',
        'create_trip_event':'Записать точный проект только после persisted UI approval. Недоступен модели.'}
    def safe_tool(method):
        @functools.wraps(method)
        def wrapped(*args,**kwargs):
            try:return method(*args,**kwargs)
            except DomainError:raise
            except Exception:raise DomainError('Внешний источник недоступен; проверьте настройку и повторите чтение') from None
        return wrapped
    for name in names:mcp.add_tool(safe_tool(getattr(service,name)),name=name,description=descriptions[name])
    mcp.run(transport='stdio')

if __name__=='__main__':main()

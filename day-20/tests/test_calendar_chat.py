"""Manual chat clarification, verified by the host against fresh discovery."""
import asyncio
import json
from datetime import timedelta
import pytest
from backend.calendar_choice import resolve_calendar_selection
from backend.services import Service
from backend.store import Store
from backend.config import Settings
from backend.registry import Registry
from backend.agent import Agent
from backend.providers import instant

CATALOG=[dict(id='calendar-a',name='Тестовый первый'),dict(id='calendar-b',name='Тестовый второй')]

@pytest.mark.parametrize('prompt',['Тестовый второй','  ТЕСТОВЫЙ   ВТОРОЙ  ','«Тестовый второй»','Используй календарь “Тестовый второй”.','Я выбираю Тестовый второй!','calendar-b','Используй calendar-b'])
def test_manual_exact_name_id_or_positive_reply(prompt):
    assert resolve_calendar_selection(prompt,CATALOG)=='calendar-b'

@pytest.mark.parametrize('prompt',['Не используй календарь Тестовый второй','Можно использовать Тестовый второй?','Тестовый первый или Тестовый второй','первый','1','второй','Тестовый','calendar-unknown','Выбрать Тестовый второй?','Не Тестовый второй'])
def test_unsafe_ambiguous_or_fuzzy_reply_needs_clarification(prompt):
    assert resolve_calendar_selection(prompt,CATALOG) is None

@pytest.mark.parametrize('prompt',['Не пропустить','Используй календарь «Не пропустить».'])
def test_negation_word_inside_real_exact_label_is_not_an_instruction(prompt):
    assert resolve_calendar_selection(prompt,[dict(id='special',name='Не пропустить'),*CATALOG])=='special'


def test_fresh_catalogue_revalidates_chat_choice_and_refuses_model_wrong_id(tmp_path):
    class Calendar:
        def __init__(self):self.current=CATALOG
        def calendars(self):return self.current
        def busy(self,*args):return []
    store=Store(tmp_path/'calendar.sqlite');run,_=store.new_run('request','fingerprint','Тестовый второй',None)
    calendar=Calendar();service=Service(store,calendar=calendar)
    result=service.list_calendars(run['id']);assert result['selected_calendar_id']=='calendar-b';assert not result['requires_selection']
    assert store.get('run',run['id'])['calendar_id']=='calendar-b'
    with pytest.raises(ValueError):service.find_free_slots(result['calendars_id'],'calendar-a','2099-10-01T10:00:00+06:00','2099-10-01T18:00:00+06:00',3,'10:00','18:00',run['id'])
    calendar.current=[dict(id='calendar-a',name='Тестовый первый'),dict(id='calendar-b',name='Переименованный')]
    refreshed=service.list_calendars(run['id']);assert refreshed['selected_calendar_id'] is None and refreshed['requires_selection']
    assert store.get('run',run['id'])['calendar_id'] is None


def test_actual_stdio_two_message_clarification_preserves_original_trip_intent(tmp_path):
    start='2099-10-01T10:00:00+06:00';end='2099-10-01T18:00:00+06:00';trip_end='2099-10-01T13:00:00+06:00'
    original='Найди трёхчасовую поездку в Омске: 10:00–18:00, не ниже +5 °C, осадки до 30%, ветер до 8 м/с.'
    class ChatModel:
        def __init__(self):self.calls=0
        async def complete(self,messages,tools):
            user=[m['content'] for m in messages if m['role']=='user'];results=[json.loads(m['content']) for m in messages if m['role']=='tool']
            if user[-1]==original:
                if not results:return self.call('calendar__list_calendars',{})
                assert results[-1]['requires_selection']
                return dict(content='В каком календаре проверить занятость? Ответьте полным именем: Тестовый первый или Тестовый второй.',tool_calls=[])
            assert user[-1]=='Используй календарь «Тестовый второй».' and original in user
            if not results:return self.call('calendar__list_calendars',{})
            assert results[0]['selected_calendar_id']=='calendar-b'
            if len(results)==1:return self.call('weather__resolve_city',dict(name='Омск'))
            if len(results)==2:return self.call('weather__get_hourly_forecast',dict(city_id=results[1]['city_id'],city_index=0,start=start,end=end))
            if len(results)==3:return self.call('calendar__find_free_slots',dict(calendars_id=results[0]['calendars_id'],calendar_id=results[0]['selected_calendar_id'],start=start,end=end,duration_hours=3,day_start='10:00',day_end='18:00'))
            if len(results)==4:return self.call('plans__save_trip_draft',dict(forecast_id=results[2]['forecast_id'],availability_id=results[3]['availability_id'],start=start,end=trip_end,title='Тестовая поездка',reason='Проверены погода и занятость',min_temperature_c=5,max_precipitation_probability=30,max_wind_ms=8))
            if len(results)==5:return self.call('plans__get_trip_plan',dict(plan_id=results[4]['id']))
            return dict(content='План подготовлен',tool_calls=[])
        def call(self,name,args):
            self.calls+=1
            return dict(content=None,tool_calls=[dict(id=f'call-{self.calls}',type='function',function=dict(name=name,arguments=json.dumps(args)))])
    async def scenario():
        hours=[dict(time=(instant(start)+timedelta(hours=i)).isoformat(),temperature_c=10,precipitation_probability=10,wind_ms=2) for i in range(8)]
        fixture=tmp_path/'sources.json';fixture.write_text(json.dumps(dict(cities=[dict(name='Омск',latitude=55,longitude=73,timezone='Asia/Omsk')],forecast=dict(hours=hours,available_start=start,available_end=end),calendars=CATALOG)))
        settings=Settings(db_path=str(tmp_path/'chat.sqlite'),fixture_path=str(fixture));store=Store(settings.db_path);registry=Registry(settings);model=ChatModel();await registry.start()
        try:
            first,_=store.new_run('first','first',original,None);await Agent(store,registry,model,settings).run(first['id'])
            assert store.get('run',first['id'])['status']=='completed' and store.plan_for_run(first['id']) is None
            second,_=store.new_run('second','second','Используй календарь «Тестовый второй».',None)
            assert second['calendar_id'] is None
            await Agent(store,registry,model,settings).run(second['id'])
            final=store.get('run',second['id']);assert final['status']=='completed',final
            plan=store.plan_for_run(second['id']);assert plan['calendar_id']=='calendar-b' and plan['status']=='draft'
            assert plan['conditions']==dict(min_temperature_c=5,max_precipitation_probability=30,max_wind_ms=8)
            assert len(final['trace'])==6 and {t['server_id'] for t in final['trace']}=={'calendar','weather','plans'}
            assert store.get('run',second['id'])['calendar_id']=='calendar-b'
            assert json.loads(fixture.read_text()).get('writes',0)==0
            assert store.all('operation')==[]
        finally:await registry.close()
    asyncio.run(scenario())

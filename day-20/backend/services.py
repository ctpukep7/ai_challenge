from backend.errors import DomainError
from backend.calendar_choice import resolve_calendar_selection
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from hashlib import sha256
import json
import math
from backend.store import Store, uid, now
from backend.providers import WeatherProvider, CalendarProvider, FixtureProvider, instant


def payload_hash(plan):
    return sha256(json.dumps({k:plan[k] for k in ('title','calendar_id','start','end','timezone','uid')},sort_keys=True).encode()).hexdigest()

class Service:
    def __init__(self, store, weather=None, calendar=None, timezone='Asia/Omsk'):
        self.store=store;self.weather=weather or WeatherProvider();self.calendar=calendar;self.timezone=timezone

    def resolve_city(self, name: str, run_id: str) -> dict:
        candidates=self.weather.resolve(name)
        if not candidates:raise DomainError('Город не найден; уточните название')
        evidence=self.store.evidence('city',run_id,dict(candidates=candidates))
        return dict(city_id=evidence['id'],candidates=candidates)

    def get_hourly_forecast(self, city_id: str, city_index: int, start: str, end: str, run_id: str) -> dict:
        city_data=self.store.require(city_id,'city',run_id)
        if city_index<0 or city_index>=len(city_data['candidates']):raise DomainError('Некорректный выбор города')
        city=city_data['candidates'][city_index]
        lower=instant(start);upper=instant(end)
        if lower>=upper:raise DomainError('Конец должен быть позже начала')
        result=self.weather.forecast(city,self.timezone)
        if lower<instant(result['available_start']) or upper>instant(result['available_end']):raise DomainError('Даты вне фактически доступного почасового прогноза')
        hours=[h for h in result['hours'] if instant(h['time'])<upper and instant(h['time'])+timedelta(hours=1)>lower]
        evidence=self.store.evidence('forecast',run_id,dict(city=city,city_id=city_id,city_index=city_index,start=start,end=end,timezone=self.timezone,hours=hours))
        return dict(forecast_id=evidence['id'],city=city,start=start,end=end,timezone=self.timezone,hours=hours)

    def _calendar_selection(self,run_id,calendars):
        run=self.store.get('run',run_id)
        # Explicit IDs from the legacy API retain their previous contract. Chat choices
        # are revalidated against each newly discovered catalogue, never global history.
        if run.get('calendar_id') and run.get('calendar_selection_source')!='chat':
            return run['calendar_id'] if any(c['id']==run['calendar_id'] for c in calendars) else None
        selected=resolve_calendar_selection(run['prompt'],calendars)
        if selected or run.get('calendar_selection_source')=='chat':
            self.store.update_run(run_id,calendar_id=selected,calendar_selection_source='chat')
        return selected

    def list_calendars(self, run_id: str) -> dict:
        calendars=self.calendar.calendars()
        if not calendars:raise DomainError('Доступных календарей нет')
        selected=self._calendar_selection(run_id,calendars)
        evidence=self.store.evidence('calendars',run_id,dict(calendars=calendars))
        return dict(calendars_id=evidence['id'],calendars=calendars,selected_calendar_id=selected,requires_selection=len(calendars)>1 and selected is None)

    def find_free_slots(self, calendars_id: str, calendar_id: str, start: str, end: str, duration_hours: float, day_start: str, day_end: str, run_id: str) -> dict:
        calendars=self.store.require(calendars_id,'calendars',run_id)['calendars']
        chosen=[c for c in calendars if c['id']==calendar_id]
        if len(chosen)!=1:raise DomainError('Календарь не обнаружен в этом запуске')
        selected=self._calendar_selection(run_id,calendars)
        run=self.store.get('run',run_id)
        if run.get('calendar_id') and run['calendar_id']!=calendar_id:raise DomainError('Нужно использовать явно выбранный календарь')
        if selected and selected!=calendar_id:raise DomainError('Нужно использовать явно выбранный календарь')
        if len(calendars)>1 and not selected:raise DomainError('Несколько календарей: попросите пользователя ответить в чате полным именем или ID календаря')
        lower=instant(start);upper=instant(end)
        if lower>=upper or upper-lower>timedelta(days=16):raise DomainError('Поддерживается диапазон от 1 часа до 16 дней')
        if not 0<duration_hours<=24:raise DomainError('Длительность должна быть от 0 до 24 часов')
        begin_time=time.fromisoformat(day_start);finish_time=time.fromisoformat(day_end)
        if begin_time>=finish_time:raise DomainError('Дневное окно должно завершаться в тот же день')
        busy=self.calendar.busy(calendar_id,start,end,self.timezone)
        zone=ZoneInfo(self.timezone);day=lower.astimezone(zone).date();last=upper.astimezone(zone).date();slots=[]
        while day<=last:
            begin=max(lower,datetime.combine(day,begin_time,tzinfo=zone));finish=min(upper,datetime.combine(day,finish_time,tzinfo=zone));cursor=begin
            if begin<finish:
                for b,e in busy:
                    if e<=begin or b>=finish:continue
                    if b>cursor and b-cursor>=timedelta(hours=duration_hours):slots.append(dict(start=cursor.isoformat(),end=min(b,finish).isoformat()))
                    cursor=max(cursor,e)
                if finish-cursor>=timedelta(hours=duration_hours):slots.append(dict(start=cursor.isoformat(),end=finish.isoformat()))
            day+=timedelta(days=1)
        data=dict(calendar_id=calendar_id,calendar_name=chosen[0]['name'],start=start,end=end,duration_hours=duration_hours,day_start=day_start,day_end=day_end,timezone=self.timezone,slots=slots)
        evidence=self.store.evidence('availability',run_id,data)
        return dict(availability_id=evidence['id'],**data)

    def save_trip_draft(self, forecast_id: str, availability_id: str, start: str, end: str, title: str, reason: str, min_temperature_c: float, max_precipitation_probability: float, max_wind_ms: float, run_id: str) -> dict:
        forecast=self.store.require(forecast_id,'forecast',run_id);availability=self.store.require(availability_id,'availability',run_id)
        lower=instant(start);upper=instant(end)
        if not title.strip() or len(title)>150 or len(reason)>2000:raise DomainError('Задайте краткое название и обоснование')
        if lower<=datetime.now(ZoneInfo(self.timezone)):raise DomainError('Поездка должна начинаться в будущем')
        if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (min_temperature_c,max_precipitation_probability,max_wind_ms)):raise DomainError('Ограничения погоды должны быть конечными числами')
        if upper<=lower or upper-lower!=timedelta(hours=availability['duration_hours']):raise DomainError('Длительность не соответствует проверенной доступности')
        if not any(instant(s['start'])<=lower and instant(s['end'])>=upper for s in availability['slots']):raise DomainError('Интервал не свободен в проверенном календаре')
        if lower<instant(forecast['start']) or upper>instant(forecast['end']):raise DomainError('Интервал не покрыт прогнозом')
        if not 0<=max_precipitation_probability<=100 or max_wind_ms<0:raise DomainError('Некорректные ограничения погоды')
        hours=sorted([h for h in forecast['hours'] if instant(h['time'])<upper and instant(h['time'])+timedelta(hours=1)>lower],key=lambda h:instant(h['time']))
        cursor=lower
        for hour in hours:
            begin=instant(hour['time']);finish=begin+timedelta(hours=1)
            if begin>cursor:raise DomainError('Прогноз содержит пропуск')
            if any(not isinstance(hour.get(k),(int,float)) or isinstance(hour.get(k),bool) or not math.isfinite(hour[k]) for k in ('temperature_c','precipitation_probability','wind_ms')):raise DomainError('Почасовые данные погоды неполны')
            if not 0<=hour['precipitation_probability']<=100 or hour['wind_ms']<0:raise DomainError('Почасовые данные погоды некорректны')
            if hour['temperature_c']<min_temperature_c or hour['precipitation_probability']>max_precipitation_probability or hour['wind_ms']>max_wind_ms:raise DomainError('Погода не подходит на всём интервале')
            cursor=max(cursor,finish)
        if cursor<upper:raise DomainError('Прогноз не покрывает весь интервал')
        plan=dict(id=uid(),run_id=run_id,title=title.strip(),city=forecast['city']['name'],calendar_id=availability['calendar_id'],calendar_name=availability['calendar_name'],start=start,end=end,timezone=self.timezone,reason=reason,
                  forecast_id=forecast_id,availability_id=availability_id,conditions=dict(min_temperature_c=min_temperature_c,max_precipitation_probability=max_precipitation_probability,max_wind_ms=max_wind_ms),
                  weather_summary=dict(min_temperature_c=min(h['temperature_c'] for h in hours),max_precipitation_probability=max(h['precipitation_probability'] for h in hours),max_wind_ms=max(h['wind_ms'] for h in hours)),status='draft',uid=uid()+'@day20.local',created_at=now(),error=None)
        self.store.put('plan',plan)
        return plan

    def get_trip_plan(self, plan_id: str, run_id: str) -> dict:
        plan=self.store.get('plan',plan_id)
        if plan['run_id']!=run_id:raise DomainError('План не относится к этому запуску')
        return plan

    def create_trip_event(self, plan_id: str) -> dict:
        # Claim the one persisted operation across hosts/processes before any network work.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');plan=self.store.get('plan',plan_id,db)
            if plan['status']=='created':return plan
            if plan['status'] not in ('approved','uncertain'):raise DomainError('Для записи нужно точное подтверждение карточки')
            op=self.store.get('operation',plan_id,db)
            if op.get('plan_id')!=plan_id or op.get('uid')!=plan['uid'] or op['payload_hash']!=payload_hash(plan) or not op.get('approved_at'):raise DomainError('Подтверждённые поля изменились')
            if op['status']=='writing':return dict(plan,status='uncertain',error='Подтверждение уже выполняется; дождитесь результата')
            if op['status'] not in ('pending','approved','uncertain','created'):raise DomainError('Календарная операция не допускает запись')
            reconcile=plan['status']=='uncertain' or op['status'] in ('uncertain','created')
            if op['status']=='conflict':return plan
            op['status']='writing';self.store.put('operation',op,db)
        def save_status(status,error=None):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');current=self.store.get('plan',plan_id,db)
                if current['status']=='created':return current
                current.update(status=status,error=error);self.store.put('plan',current,db)
                operation=self.store.get('operation',plan_id,db);operation.update(status=status,error=error);self.store.put('operation',operation,db)
                return current
        def matches(existing):
            return existing['uid']==plan['uid'] and existing['title']==plan['title'] and instant(existing['start'])==instant(plan['start']) and instant(existing['end'])==instant(plan['end'])
        try:
            if not reconcile and instant(plan['start'])<=datetime.now(ZoneInfo(plan['timezone'])):return save_status('conflict','Время поездки прошло; создайте новый план')
            existing=self.calendar.lookup(plan['calendar_id'],plan['uid'])
            if existing:
                if not matches(existing):return save_status('uncertain','UID существует с другими полями; требуется ручная сверка')
                return save_status('created')
            if reconcile:return save_status('uncertain','Событие по UID не найдено; повторная запись запрещена, требуется сверка')
            if self.calendar.busy(plan['calendar_id'],plan['start'],plan['end'],plan['timezone']):return save_status('conflict','Календарь изменился: выбранное время теперь занято. Создайте новый план')
            self.calendar.create(plan,plan['uid'])
            return save_status('created')
        except Exception:
            # Do not expose upstream credentials, URLs or private ICS in errors.
            try:
                existing=self.calendar.lookup(plan['calendar_id'],plan['uid'])
                if existing and matches(existing):return save_status('created')
            except Exception:pass
            return save_status('uncertain','Исход календарной операции неизвестен. Повтор подтверждения выполняет только сверку UID')

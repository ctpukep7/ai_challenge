from backend.errors import DomainError
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
import json
import time as sleep_time
from hashlib import sha256
import httpx
from pathlib import Path
import caldav
from icalendar import Calendar, Event


def instant(value):
    try:result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError):raise DomainError('Время должно быть в формате ISO8601 с часовым поясом') from None
    if result.tzinfo is None:raise DomainError('Время должно содержать часовой пояс')
    return result


def busy_intervals(ics_documents, start, end, tz):
    """Search results must already be expanded by CalDAV (including overrides)."""
    zone=ZoneInfo(tz); lower=instant(start);upper=instant(end);intervals=[]
    for data in ics_documents:
        events=Calendar.from_ical(data).walk('VEVENT')
        if not events:raise DomainError('Календарь вернул некорректное событие')
        for event in events:
            if str(event.get('TRANSP','OPAQUE')).upper()=='TRANSPARENT' or str(event.get('STATUS','')).upper()=='CANCELLED':continue
            if event.get('RRULE') or event.get('RDATE') or event.get('EXDATE'):
                raise DomainError('Сервер не раскрыл повторяющееся событие; свободное время неизвестно')
            for field in ('DTSTART','DTEND'):
                if event.get(field) is not None and event[field].params.get('TZID'):
                    ZoneInfo(str(event[field].params['TZID']))
            begin=event.decoded('DTSTART',None)
            if begin is None:raise DomainError('Событие без начала; занятость неизвестна')
            finish=event.decoded('DTEND',None)
            duration=event.decoded('DURATION',None)
            if finish is None:finish=begin+(duration or (timedelta(days=1) if not isinstance(begin,datetime) else timedelta(0)))
            def to_dt(v):
                if isinstance(v,datetime):return v.replace(tzinfo=zone) if v.tzinfo is None else v
                if isinstance(v,date):return datetime.combine(v,time.min,tzinfo=zone)
                raise DomainError('Некорректное время календаря')
            begin=to_dt(begin);finish=to_dt(finish)
            if finish<begin:raise DomainError('Некорректная длительность события')
            if finish==begin:continue
            if begin<upper and finish>lower:intervals.append((max(begin,lower),min(finish,upper)))
    intervals.sort(key=lambda x:x[0]);merged=[]
    for begin,finish in intervals:
        if merged and begin<=merged[-1][1]:merged[-1]=(merged[-1][0],max(finish,merged[-1][1]))
        else:merged.append((begin,finish))
    return merged

class WeatherProvider:
    def resolve(self, name):
        with httpx.Client(timeout=20) as client:
            result=client.get('https://geocoding-api.open-meteo.com/v1/search',params={'name':name,'count':5,'language':'ru','format':'json'})
            result.raise_for_status()
            return [dict(name=r['name'],country=r.get('country',''),region=r.get('admin1',''),latitude=r['latitude'],longitude=r['longitude'],timezone=r.get('timezone','UTC')) for r in result.json().get('results',[])]
    def forecast(self, city, tz):
        with httpx.Client(timeout=25) as client:
            response=client.get('https://api.open-meteo.com/v1/forecast',params={'latitude':city['latitude'],'longitude':city['longitude'],'hourly':'temperature_2m,precipitation_probability,wind_speed_10m','wind_speed_unit':'ms','timezone':tz,'forecast_days':16})
            response.raise_for_status();data=response.json();hourly=data['hourly']
        zone=ZoneInfo(tz)
        hours=[dict(time=datetime.fromisoformat(t).replace(tzinfo=zone).isoformat(),temperature_c=temperature,precipitation_probability=rain,wind_ms=wind) for t,temperature,rain,wind in zip(hourly['time'],hourly['temperature_2m'],hourly['precipitation_probability'],hourly['wind_speed_10m'],strict=True)]
        if not hours:raise DomainError('Прогноз не содержит доступных часов')
        return dict(hours=hours,timezone=tz,available_start=hours[0]['time'],available_end=(instant(hours[-1]['time'])+timedelta(hours=1)).isoformat())

class CalendarProvider:
    def __init__(self, username, password, url):self.username=username;self.password=password;self.url=url
    def _client(self):
        if not self.username or not self.password:raise DomainError('Настройте отдельный пароль приложения Яндекс Календарь в .env')
        return caldav.DAVClient(url=self.url,username=self.username,password=self.password,timeout=25)
    def calendars(self):
        with self._client() as client:
            return [dict(id=sha256(str(calendar.url).encode()).hexdigest()[:24],name=str(calendar.name or 'Календарь')) for calendar in client.principal().get_calendars()]
    def _calendar(self, client, id):
        calendars=client.principal().get_calendars()
        matching=[c for c in calendars if sha256(str(c.url).encode()).hexdigest()[:24]==id]
        if len(matching)!=1:raise DomainError('Выбранный календарь недоступен')
        return matching[0]
    def busy(self, calendar_id, start, end, tz):
        with self._client() as client:
            calendar=self._calendar(client,calendar_id)
            events=calendar.search(start=instant(start),end=instant(end),event=True,expand=True)
            return busy_intervals([event.data for event in events],start,end,tz)
    def lookup(self, calendar_id, uid):
        with self._client() as client:
            calendar=self._calendar(client,calendar_id)
            try:event=calendar.get_event_by_uid(uid)
            except caldav.error.NotFoundError:return None
            events=Calendar.from_ical(event.data).walk('VEVENT')
            if len(events)!=1:raise DomainError('Сохранённый UID неоднозначен')
            obj=events[0]
            return dict(uid=str(obj['UID']),title=str(obj.get('SUMMARY','')),start=obj.decoded('DTSTART').isoformat(),end=obj.decoded('DTEND').isoformat())
    def create(self, plan, uid):
        ics=Calendar();ics.add('prodid','-//Day20//MCP orchestration//RU');ics.add('version','2.0')
        event=Event();event.add('uid',uid);event.add('dtstamp',datetime.now(timezone.utc));event.add('dtstart',instant(plan['start']));event.add('dtend',instant(plan['end']));event.add('summary',plan['title']);ics.add_component(event)
        with self._client() as client:self._calendar(client,plan['calendar_id']).add_event(ics.to_ical().decode(),no_overwrite=True)

class FixtureProvider:
    """Explicit offline testing adapter; never enabled by .env or production defaults."""
    def __init__(self,path):self.path=Path(path)
    def read(self):return json.loads(self.path.read_text())
    def resolve(self,name):
        data=self.read()
        if data.get('resolve_delay'):sleep_time.sleep(data['resolve_delay'])
        return data['cities']
    def forecast(self,city,tz):return self.read()['forecast']
    def calendars(self):return self.read()['calendars']
    def busy(self,calendar_id,start,end,tz):return busy_intervals(self.read().get('ics',[]),start,end,tz)
    def lookup(self,calendar_id,uid):return self.read().get('events',{}).get(uid)
    def create(self,plan,uid):
        data=self.read();data.setdefault('events',{})[uid]=dict(uid=uid,title=plan['title'],start=plan['start'],end=plan['end']);data['writes']=data.get('writes',0)+1;self.path.write_text(json.dumps(data))
        if data.get('lost_response'):raise TimeoutError('Response lost after write')

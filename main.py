import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

SOURCE = 'https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json'
TZ = ZoneInfo('Europe/Prague')

def request_json(url, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = Request(url, data=data, method=method, headers={
        'User-Agent': 'PokemonGOCommunityEvents/1.0',
        'Content-Type': 'application/json',
    })
    with urlopen(req, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None

def parse_date(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc)

def payload_for(event):
    start = parse_date(event['start'])
    end = parse_date(event['end']) if event.get('end') else None
    def fmt(dt):
        return dt.astimezone(TZ).strftime('%d. %m. %Y v %H:%M')
    fields = [{'name': 'Začátek — český čas', 'value': fmt(start)},
              {'name': 'Odpočet', 'value': f'<t:{int(start.timestamp())}:R>'}]
    if end:
        fields.append({'name': 'Konec — český čas', 'value': fmt(end)})
    fields.append({'name': 'Typ akce', 'value': str(event.get('heading') or 'Pokémon GO')[:1024]})
    embed = {'title': html.unescape(event['name'])[:256],
             'description': '📅 Blížící se Pokémon GO event. Podrobnosti a bonusy najdeš v odkazu na akci.',
             'url': event['link'], 'color': 16763904, 'fields': fields,
             'footer': {'text': 'Zdroj: Leek Duck / ScrapedDuck • Europe/Prague'}}
    if str(event.get('image', '')).startswith('https://'):
        embed['image'] = {'url': event['image']}
    return {'embeds': [embed], 'allowed_mentions': {'parse': []}}

def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, message TEXT, fingerprint TEXT)')
    db.commit()
    return db

def deliver(db, webhook, key, payload):
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    row = db.execute('SELECT message, fingerprint FROM sent WHERE id=?', (key,)).fetchone()
    if row and row[1] == fingerprint:
        return
    if row:
        try:
            request_json(webhook + '/messages/' + row[0], payload, 'PATCH')
            message_id = row[0]
        except HTTPError as err:
            if err.code != 404:
                raise
            # A manually deleted announcement stays deleted.
            message_id = row[0]
    else:
        result = request_json(webhook + '?wait=true', payload, 'POST')
        message_id = result['id']
    db.execute('INSERT OR REPLACE INTO sent VALUES (?, ?, ?)', (key, message_id, fingerprint))
    db.commit()
    print('Oznámení uloženo: ' + key, flush=True)

def process_events(db, webhook, events, now):
    for event in events:
        try:
            if not event.get('start') or not event.get('eventID'):
                continue
            start = parse_date(event['start'])
            remaining = start.timestamp() - now.timestamp()
            already_sent = db.execute('SELECT 1 FROM sent WHERE id=?', (event['eventID'],)).fetchone()
            if remaining <= 0 or (remaining > 86400 and not already_sent):
                continue
            deliver(db, webhook, event['eventID'], payload_for(event))
        except (ValueError, KeyError, TypeError):
            print('Přeskakuji event s neúplnými údaji.', flush=True)

def main():
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '').strip().rstrip('/')
    if not re.fullmatch(r'https://discord\.com/api/webhooks/\d+/[\w.-]+', webhook):
        raise SystemExit('Nastav platnou proměnnou DISCORD_WEBHOOK_URL.')
    db = open_db('state/events.sqlite')
    print('Automatizace spuštěna; časové pásmo Europe/Prague.', flush=True)
    try:
            deliver(db, webhook, '__welcome__', {
                'content': '✅ Automatické hlídání Pokémon GO eventů je zapnuté. Upozornění posílám přibližně 24 hodin před začátkem. Časy platí pro Česko. Při prvním spuštění doplním také dosud nezačaté akce v příštích 24 hodinách.',
                'allowed_mentions': {'parse': []},
            })
            events = request_json(SOURCE)
            if not isinstance(events, list) or not events:
                raise ValueError('Neplatný zdroj eventů')
            print(f'Zdroj načten: {len(events)} eventů.', flush=True)
            process_events(db, webhook, events, datetime.now(timezone.utc))
    except Exception as err:
        # Never log the exception message: it may contain the webhook secret.
        code = getattr(err, 'code', '')
        print(f'Chyba {type(err).__name__} {code}. Zkontroluj nastavení a další běh.', flush=True)
        raise SystemExit(1) from None
    finally:
        db.close()

if __name__ == '__main__':
    main()

import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SOURCE = 'https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.min.json'
TZ = ZoneInfo('Europe/Prague')

CATEGORY_HEADINGS = {
    'cz': '# 🇨🇿 Eventy pro ČR',
    'foreign': '# 🌍 Eventy v zahraničí',
    'unknown': '# ❔ Dostupnost neověřena',
}

class EventPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.has_tags = False
        self.location_specific = False
        self.text = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get('class', '').split()
        if 'page-tags' in classes:
            self.has_tags = True
        if 'tag' in classes and 'location-specific' in classes:
            self.location_specific = True

    def handle_data(self, data):
        self.text.append(data.strip())

def classify_page(event, page):
    parser = EventPage()
    parser.feed(page)
    if not parser.has_tags:
        return 'unknown', 'Zdroj neposkytl rozpoznatelné údaje o dostupnosti.'
    if not parser.location_specific:
        return 'cz', 'Zdroj akci neoznačuje jako místně omezenou; zahrnuta mezi eventy pro ČR.'
    text = '\n'.join(x for x in parser.text if x)
    location = re.search(r'\bLocation:\s*([^\n]+)', text, re.I)
    place = location.group(1)[:250] if location else ''
    identifying_text = event.get('name', '') + ' ' + place
    if re.search(r'\b(Czechia|Czech Republic|Česko|Česká republika|Prague|Praha|Brno|Ostrava|Plzeň|Pilsen|Olomouc)\b', identifying_text, re.I):
        return 'cz', 'Místní akce v ČR' + (': ' + place if place else '. Podrobnosti v odkazu.')
    if re.search(r'\b(Sendai|Tohoku|Japan|Mexico City|London|Kaohsiung|Los Angeles|Taiwan|Osaka|Tokyo|New York|Paris|Berlin|Madrid|Barcelona|Seoul|Singapore|Hong Kong|Taipei|Tainan|Sao Paulo|São Paulo|Jakarta|Bali|Mumbai|Delhi|Sydney|Melbourne|Vancouver|Toronto|Seattle|Chicago|Dortmund|Liverpool|Oslo|Copenhagen|Helsinki|Stockholm|Rotterdam|Amsterdam|Valencia|Milan|Rome)\b', identifying_text, re.I):
        return 'foreign', ('Místo: ' + place if place else 'Místní akce v zahraničí — místo je v názvu a detailu akce.')
    return 'unknown', 'Místně omezená akce; dostupnost pro ČR nebyla spolehlivě rozpoznána.'

def region_for(db, event):
    key = event['eventID']
    now = time.time()
    row = db.execute('SELECT category, detail, checked FROM regions WHERE id=?', (key,)).fetchone()
    if row and now - row[2] < (900 if row[0] == 'unknown' else 21600):
        return row[0], row[1]
    try:
        url = event['link']
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'leekduck.com' or not parsed.path.startswith('/events/'):
            raise ValueError('Unexpected event URL')
        req = Request(url, headers={'User-Agent': 'PokemonGOCommunityEvents/1.1'})
        with urlopen(req, timeout=20) as response:
            page = response.read(2_000_000).decode('utf-8')
        category, detail = classify_page(event, page)
    except Exception:
        if row:
            return row[0], row[1]
        category, detail = 'unknown', 'Dostupnost se nepodařilo načíst. Ověř ji v odkazu na akci.'
    db.execute('INSERT OR REPLACE INTO regions VALUES (?, ?, ?, ?)', (key, category, detail, now))
    db.commit()
    return category, detail

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

def payload_for(event, category='unknown', detail='Ověř dostupnost v detailu akce.'):
    start = parse_date(event['start'])
    end = parse_date(event['end']) if event.get('end') else None
    def fmt(dt):
        return dt.astimezone(TZ).strftime('%d. %m. %Y v %H:%M')
    fields = [{'name': 'Začátek — český čas', 'value': fmt(start)},
              {'name': 'Odpočet', 'value': f'<t:{int(start.timestamp())}:R>'}]
    if end:
        fields.append({'name': 'Konec — český čas', 'value': fmt(end)})
    fields.append({'name': 'Typ akce', 'value': str(event.get('heading') or 'Pokémon GO')[:1024]})
    fields.append({'name': 'Dostupnost', 'value': detail[:1024]})
    embed = {'title': html.unescape(event['name'])[:256],
             'description': '📅 Blížící se Pokémon GO event. Podrobnosti a bonusy najdeš v odkazu na akci.',
             'url': event['link'], 'color': {'cz': 3066993, 'foreign': 3447003, 'unknown': 16763904}[category], 'fields': fields,
             'footer': {'text': 'Zdroj: Leek Duck / ScrapedDuck • Europe/Prague'}}
    if str(event.get('image', '')).startswith('https://'):
        embed['image'] = {'url': event['image']}
    return {'content': CATEGORY_HEADINGS[category], 'embeds': [embed], 'allowed_mentions': {'parse': []}}

def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, message TEXT, fingerprint TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS regions (id TEXT PRIMARY KEY, category TEXT, detail TEXT, checked REAL)')
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
            category, detail = region_for(db, event)
            deliver(db, webhook, event['eventID'], payload_for(event, category, detail))
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

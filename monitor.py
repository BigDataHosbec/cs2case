import os, time, math, logging, requests
from bs4 import BeautifulSoup
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# ── CONFIG (desde variables de entorno) ───────────────────────────────────────
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
CASE_URL         = os.environ.get('CASE_URL', 'https://skin.club/es/cases/open/cc-bf4940a7e')
CHECK_INTERVAL   = int(os.environ.get('CHECK_INTERVAL', '60'))   # segundos
THRESHOLD_IND    = float(os.environ.get('THRESHOLD_IND', '90'))  # % alerta individual
THRESHOLD_COMB   = float(os.environ.get('THRESHOLD_COMB', '90')) # % alerta sequía global
ALERT_COOLDOWN   = int(os.environ.get('ALERT_COOLDOWN', '300'))  # seg entre alertas iguales

# ── ITEMS ─────────────────────────────────────────────────────────────────────
RARE_ITEMS = [
    {'key': 'butterfly',    'name': '★ Butterfly Knife Case Hardened', 'prob': 0.00002},
    {'key': 'spec_gloves',  'name': '★ Specialist Gloves Blackbook',   'prob': 0.00002},
    {'key': 'awp',          'name': 'AWP Queens Gambit',               'prob': 0.00002},
    {'key': 'sport_gloves', 'name': '★ Sport Gloves Frosty',           'prob': 0.00002},
    {'key': 'ak47',         'name': 'AK-47 Vulcan',                    'prob': 0.0001 },
]

KEYWORDS = {
    'butterfly':    ['butterfly'],
    'spec_gloves':  ['specialist', 'blackbook'],
    'awp':          ['queen', 'gambit'],
    'sport_gloves': ['sport gloves', 'frosty'],
    'ak47':         ['ak-47', 'vulcan'],
}

PROB_TOTAL = sum(i['prob'] for i in RARE_ITEMS)
EXPECTED_EVERY = round(1 / PROB_TOTAL)

# ── MATH ──────────────────────────────────────────────────────────────────────
def prob_acum(p, n):
    if p <= 0 or n <= 0: return 0.0
    return (1 - (1 - p) ** n) * 100

def prob_acum_combined(n):
    if n <= 0: return 0.0
    prod = 1.0
    for item in RARE_ITEMS:
        prod *= (1 - item['prob']) ** n
    return (1 - prod) * 100

# ── SCRAPING ──────────────────────────────────────────────────────────────────
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
    'Accept-Language': 'es-ES,es;q=0.9',
}

def identify_item(weapon, skin):
    text = (weapon + ' ' + skin).lower()
    for key, kws in KEYWORDS.items():
        if any(k in text for k in kws):
            return key
    return None

def drop_id(d):
    return f"{d['weapon']}|{d['skin']}|{d['wear']}|{d['price']}"

def scrape():
    try:
        resp = requests.get(CASE_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Contador
        ctr_el  = soup.select_one('._usY9LTl4GWg-')
        counter = None
        if ctr_el:
            counter = int(ctr_el.get_text(strip=True).replace('\xa0','').replace(' ','').replace('.','').replace(',',''))

        # Drops
        cards = soup.select('._GmTg14yUcXI-')
        drops = []
        for card in cards:
            price  = card.select_one('._NqKX8CAt4s8-')
            weapon = card.select_one('._oYs1Jv-OYmk-')
            skin   = card.select_one('._DF8kvS3ITXA-')
            wear   = card.select_one('._8QYmTIS34ak-')
            if not weapon: continue
            drops.append({
                'price':   price.get_text(strip=True)  if price  else '',
                'weapon':  weapon.get_text(strip=True) if weapon else '',
                'skin':    skin.get_text(strip=True)   if skin   else '',
                'wear':    wear.get_text(strip=True)   if wear   else '',
            })

        for d in drops:
            d['item_key'] = identify_item(d['weapon'], d['skin'])

        log.info(f"Scraped OK — counter: {counter}, drops: {len(drops)}")
        return {'counter': counter, 'drops': drops}

    except Exception as e:
        log.error(f"Scrape error: {e}")
        return None

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            'chat_id':    TELEGRAM_CHAT_ID,
            'text':       text,
            'parse_mode': 'HTML'
        }, timeout=10)
        log.info("Telegram enviado")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── ESTADO ────────────────────────────────────────────────────────────────────
state = {
    'base_counter':      None,
    'rare_tracking':     {item['key']: None for item in RARE_ITEMS},  # last seen counter
    'last_any_rare':     None,   # counter del último drop raro cualquiera
    'prev_drop_ids':     set(),
    'last_alert_ind':    {},     # key -> timestamp última alerta
    'last_alert_comb':   0,      # timestamp última alerta combinada
    'initialized':       False,
}

def initialize(counter, drops):
    state['base_counter']  = counter
    state['last_any_rare'] = counter
    for item in RARE_ITEMS:
        in_feed = any(d['item_key'] == item['key'] for d in drops)
        state['rare_tracking'][item['key']] = counter if in_feed else counter
    state['prev_drop_ids'] = set(drop_id(d) for d in drops)
    state['initialized']   = True
    log.info(f"Inicializado — base counter: {counter}")
    send_telegram(
        f"🟢 <b>CS2 Monitor iniciado</b>\n"
        f"Caja: DONT TRUST\n"
        f"Counter base: <code>{counter:,}</code>\n"
        f"Drop raro esperado cada ~{EXPECTED_EVERY:,} cajas\n"
        f"Chequeando cada {CHECK_INTERVAL}s"
    )

def process(data):
    counter = data['counter']
    drops   = data['drops']
    now     = time.time()

    if not state['initialized']:
        initialize(counter, drops)
        return

    # Drops nuevos respecto al check anterior
    current_ids = set(drop_id(d) for d in drops)
    new_drops   = [d for d in drops if drop_id(d) not in state['prev_drop_ids']]
    new_rares   = [d for d in new_drops if d['item_key'] is not None]

    if new_rares:
        log.info(f"Nuevos drops raros: {[d['weapon']+' '+d['skin'] for d in new_rares]}")

    # Reset tracking individual solo para drops nuevos de ese item
    for d in new_rares:
        state['rare_tracking'][d['item_key']] = counter

    # Reset tracking combinado si cayó cualquier raro
    if new_rares:
        state['last_any_rare'] = counter

    state['prev_drop_ids'] = current_ids

    # ── Calcular presiones ────────────────────────────────────────────────────
    hot_items = []
    for item in RARE_ITEMS:
        last_seen = state['rare_tracking'][item['key']] or state['base_counter']
        n   = max(0, counter - last_seen)
        pct = prob_acum(item['prob'], n)
        hot_items.append({**item, 'n': n, 'pct': pct})

    boxes_since_any = max(0, counter - (state['last_any_rare'] or state['base_counter']))
    combined_pct    = prob_acum_combined(boxes_since_any)

    items_summary = ', '.join(i['key'] + ':' + str(round(i['pct'],1)) + '%' for i in hot_items)
    log.info(f"Counter: {counter} | Combined: {combined_pct:.1f}% ({boxes_since_any} cajas) | Items: {items_summary}")

    # ── Alertas individuales ──────────────────────────────────────────────────
    for item in hot_items:
        if item['pct'] >= THRESHOLD_IND:
            last = state['last_alert_ind'].get(item['key'], 0)
            if now - last > ALERT_COOLDOWN:
                state['last_alert_ind'][item['key']] = now
                send_telegram(
                    f"🔥 <b>MOMENTO CALIENTE — {item['name']}</b>\n\n"
                    f"Probabilidad acumulada: <b>{item['pct']:.1f}%</b>\n"
                    f"Cajas sin aparecer: <code>{item['n']:,}</code>\n"
                    f"Counter global: <code>{counter:,}</code>\n\n"
                    f"⚡ <i>Estadísticamente ya debería haber caído</i>"
                )

    # ── Alerta sequía global ──────────────────────────────────────────────────
    if combined_pct >= THRESHOLD_COMB:
        if now - state['last_alert_comb'] > ALERT_COOLDOWN:
            state['last_alert_comb'] = now
            bars = ''.join(
                f"{'🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'} "
                f"{i['name']}: {i['pct']:.1f}%\n"
                for i in hot_items
            )
            send_telegram(
                f"⚠️ <b>SEQUÍA GLOBAL — {combined_pct:.1f}%</b>\n\n"
                f"<code>{boxes_since_any:,}</code> cajas sin ningún drop raro\n"
                f"(esperado cada ~{EXPECTED_EVERY:,})\n\n"
                f"{bars}\n"
                f"Counter: <code>{counter:,}</code>"
            )

    # ── Notificar drops nuevos raros (informativo, sin umbral) ────────────────
    for d in new_rares:
        item = next((i for i in RARE_ITEMS if i['key'] == d['item_key']), None)
        if item:
            send_telegram(
                f"✅ <b>DROP DETECTADO</b> — {d['weapon']} {d['skin']}\n"
                f"Desgaste: {d['wear']} | Precio: {d['price']}\n"
                f"Counter: <code>{counter:,}</code>\n"
                f"<i>Contador de {item['name']} reseteado</i>"
            )

# ── LOOP PRINCIPAL ────────────────────────────────────────────────────────────
def main():
    log.info("CS2 Monitor arrancando...")
    while True:
        data = scrape()
        if data and data['counter']:
            process(data)
        else:
            log.warning("Sin datos en este ciclo")
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()

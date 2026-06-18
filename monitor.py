import os, time, logging
from playwright.sync_api import sync_playwright
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
CASE_URL         = os.environ.get('CASE_URL', 'https://skin.club/es/cases/open/cc-bf4940a7e')
CHECK_INTERVAL   = int(os.environ.get('CHECK_INTERVAL', '60'))
THRESHOLD_IND    = float(os.environ.get('THRESHOLD_IND', '90'))
THRESHOLD_COMB   = float(os.environ.get('THRESHOLD_COMB', '90'))
ALERT_COOLDOWN   = int(os.environ.get('ALERT_COOLDOWN', '300'))

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

PROB_TOTAL     = sum(i['prob'] for i in RARE_ITEMS)
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

def identify_item(weapon, skin):
    text = (weapon + ' ' + skin).lower()
    for key, kws in KEYWORDS.items():
        if any(k in text for k in kws):
            return key
    return None

def drop_id(d):
    return f"{d['weapon']}|{d['skin']}|{d['wear']}|{d['price']}"

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
        if r.status_code == 200:
            log.info("Telegram enviado OK")
        else:
            log.error(f"Telegram respondió {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── SCRAPING CON PLAYWRIGHT (ejecuta el JavaScript de la página) ──────────────
# Script JS que se ejecuta DENTRO de la página, igual que en la extensión
EXTRACT_JS = """
() => {
  const counterEl = document.querySelector('._usY9LTl4GWg-');
  const counter = counterEl
    ? parseInt(counterEl.textContent.trim().replace(/[\\s\\.,]/g, ''), 10)
    : null;
  const cards = [...document.querySelectorAll('._GmTg14yUcXI-')];
  const drops = cards.map(c => ({
    price:  c.querySelector('._NqKX8CAt4s8-')?.textContent?.trim() || '',
    weapon: c.querySelector('._oYs1Jv-OYmk-')?.textContent?.trim() || '',
    skin:   c.querySelector('._DF8kvS3ITXA-')?.textContent?.trim() || '',
    wear:   c.querySelector('._8QYmTIS34ak-')?.textContent?.trim() || '',
  })).filter(d => d.weapon);
  return { counter, drops };
}
"""

_browser = None
_page = None

def get_page():
    global _browser, _page
    if _page is None:
        pw = sync_playwright().start()
        _browser = pw.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
        _page = _browser.new_page(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
            locale='es-ES'
        )
    return _page

def scrape():
    try:
        page = get_page()
        page.goto(CASE_URL, wait_until='networkidle', timeout=45000)
        # Esperar a que aparezcan las cards (renderizadas por JS)
        try:
            page.wait_for_selector('._GmTg14yUcXI-', timeout=15000)
        except Exception:
            log.warning("No aparecieron cards tras esperar")
        time.sleep(2)
        data = page.evaluate(EXTRACT_JS)

        for d in data['drops']:
            d['item_key'] = identify_item(d['weapon'], d['skin'])

        log.info(f"Scraped OK — counter: {data['counter']}, drops: {len(data['drops'])}")
        return data
    except Exception as e:
        log.error(f"Scrape error: {e}")
        # Resetear navegador en caso de fallo
        global _browser, _page
        try:
            if _browser: _browser.close()
        except: pass
        _browser = None
        _page = None
        return None

# ── ESTADO ────────────────────────────────────────────────────────────────────
state = {
    'base_counter':    None,
    'rare_tracking':   {item['key']: None for item in RARE_ITEMS},
    'last_any_rare':   None,
    'prev_drop_ids':   set(),
    'last_alert_ind':  {},
    'last_alert_comb': 0,
    'initialized':     False,
}

def initialize(counter, drops):
    state['base_counter']  = counter
    state['last_any_rare'] = counter
    for item in RARE_ITEMS:
        state['rare_tracking'][item['key']] = counter
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

    current_ids = set(drop_id(d) for d in drops)
    new_drops   = [d for d in drops if drop_id(d) not in state['prev_drop_ids']]
    new_rares   = [d for d in new_drops if d['item_key'] is not None]

    if new_rares:
        log.info(f"Nuevos drops raros: {[d['weapon']+' '+d['skin'] for d in new_rares]}")

    for d in new_rares:
        state['rare_tracking'][d['item_key']] = counter
    if new_rares:
        state['last_any_rare'] = counter

    state['prev_drop_ids'] = current_ids

    hot_items = []
    for item in RARE_ITEMS:
        last_seen = state['rare_tracking'][item['key']] or state['base_counter']
        n   = max(0, counter - last_seen)
        pct = prob_acum(item['prob'], n)
        hot_items.append({**item, 'n': n, 'pct': pct})

    boxes_since_any = max(0, counter - (state['last_any_rare'] or state['base_counter']))
    combined_pct    = prob_acum_combined(boxes_since_any)

    items_summary = ', '.join(i['key'] + ':' + str(round(i['pct'],1)) + '%' for i in hot_items)
    log.info(f"Counter: {counter} | Combined: {combined_pct:.1f}% ({boxes_since_any} cajas) | {items_summary}")

    # Alertas individuales
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

    # Alerta sequía global
    if combined_pct >= THRESHOLD_COMB:
        if now - state['last_alert_comb'] > ALERT_COOLDOWN:
            state['last_alert_comb'] = now
            bars = ''.join(
                f"{'🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'} {i['name']}: {i['pct']:.1f}%\n"
                for i in hot_items
            )
            send_telegram(
                f"⚠️ <b>SEQUÍA GLOBAL — {combined_pct:.1f}%</b>\n\n"
                f"<code>{boxes_since_any:,}</code> cajas sin ningún drop raro\n"
                f"(esperado cada ~{EXPECTED_EVERY:,})\n\n"
                f"{bars}\n"
                f"Counter: <code>{counter:,}</code>"
            )

    # Drops nuevos (informativo)
    for d in new_rares:
        item = next((i for i in RARE_ITEMS if i['key'] == d['item_key']), None)
        if item:
            send_telegram(
                f"✅ <b>DROP DETECTADO</b> — {d['weapon']} {d['skin']}\n"
                f"Desgaste: {d['wear']} | Precio: {d['price']}\n"
                f"Counter: <code>{counter:,}</code>\n"
                f"<i>Contador de {item['name']} reseteado</i>"
            )

# ── LOOP ──────────────────────────────────────────────────────────────────────
def main():
    log.info("CS2 Monitor (Playwright) arrancando...")
    # Mensaje de arranque inmediato para confirmar que el deploy funciona
    send_telegram("⏳ <b>CS2 Monitor desplegado</b> — iniciando navegador y primer scrapeo...")
    while True:
        data = scrape()
        if data and data.get('counter'):
            process(data)
        else:
            log.warning("Sin datos en este ciclo")
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()

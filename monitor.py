import os, time, logging, json, threading
from datetime import datetime
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = str(os.environ['TELEGRAM_CHAT_ID'])
CASE_ID          = os.environ.get('CASE_ID', 'cc-bf4940a7e')
CASE_WEB_URL     = os.environ.get('CASE_WEB_URL', f'https://skin.club/es/cases/open/{CASE_ID}')
API_URL          = f"https://gate.skin.club/apiv2/cases/{CASE_ID}"
CHECK_INTERVAL   = int(os.environ.get('CHECK_INTERVAL', '60'))
THRESHOLD_IND    = float(os.environ.get('THRESHOLD_IND', '90'))
THRESHOLD_COMB   = float(os.environ.get('THRESHOLD_COMB', '90'))
ALERT_COOLDOWN   = int(os.environ.get('ALERT_COOLDOWN', '300'))
DEAD_AFTER_MIN   = int(os.environ.get('DEAD_AFTER_MIN', '60'))   # caja muerta si counter no sube en X min
PORT             = int(os.environ.get('PORT', '8080'))
STATE_FILE       = os.environ.get('STATE_FILE', '/data/state.json')

# ── ITEMS RAROS ───────────────────────────────────────────────────────────────
RARE_GROUPS = [
    {'key': 'butterfly',    'name': '★ Butterfly Knife Case Hardened', 'short': 'Butterfly', 'match': ['butterfly knife']},
    {'key': 'spec_gloves',  'name': '★ Specialist Gloves Blackbook',   'short': 'Spec Gloves', 'match': ['specialist gloves']},
    {'key': 'awp',          'name': 'AWP Queen\'s Gambit',             'short': 'AWP QG', 'match': ["queen's gambit"]},
    {'key': 'sport_gloves', 'name': '★ Sport Gloves Frosty',           'short': 'Sport Gloves', 'match': ['sport gloves']},
    {'key': 'ak47',         'name': 'AK-47 Vulcan',                    'short': 'AK Vulcan', 'match': ['vulcan']},
]

def match_group(name):
    low = name.lower()
    for g in RARE_GROUPS:
        if any(m in low for m in g['match']):
            return g['key']
    return None

def group_name(key):
    g = next((x for x in RARE_GROUPS if x['key'] == key), None)
    return g['name'] if g else key

# ── MATH ──────────────────────────────────────────────────────────────────────
def prob_acum(p, n):
    if p <= 0 or n <= 0: return 0.0
    return (1 - (1 - p) ** n) * 100

def combined_prob(probs_sum, n):
    if n <= 0: return 0.0
    return (1 - (1 - probs_sum) ** n) * 100

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML',
                   'disable_web_page_preview': True}
        if reply_markup:
            payload['reply_markup'] = json.dumps(reply_markup)
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── FETCH API ─────────────────────────────────────────────────────────────────
HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Origin': 'https://skin.club',
    'Referer': 'https://skin.club/',
    'Accept-Language': 'es-ES,es;q=0.9',
}

def fetch_data():
    try:
        r = requests.get(API_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        d = r.json()['data']

        counter    = d['stats']['opening_count']
        case_price = d.get('price', 0) / 100.0

        group_probs = {g['key']: 0.0 for g in RARE_GROUPS}
        contents = (d.get('last_successful_generation') or {}).get('contents', [])
        ev = 0.0
        for c in contents:
            name = c['item']['market_hash_name']
            p = float(c['chance_percent']) / 100.0
            price = c['item'].get('price', 0) / 100.0
            ev += p * price
            key = match_group(name)
            if key:
                group_probs[key] += p

        drops = []
        for t in d.get('top_drops', []):
            item = (t.get('reason') or {}).get('item') or t.get('item') or {}
            name = item.get('market_hash_name', '')
            if not name:
                continue
            drops.append({
                'name': name, 'price': t['price'] / 100.0,
                'created_at': t.get('created_at', ''), 'drop_id': t.get('id'),
                'key': match_group(name),
            })

        return {'counter': counter, 'group_probs': group_probs, 'drops': drops,
                'case_price': case_price, 'ev': ev}
    except Exception as e:
        log.error(f"Fetch error: {e}")
        return None

# ── ESTADO ────────────────────────────────────────────────────────────────────
state = {
    'base_counter':     None,
    'group_last_seen':  {},
    'last_any_rare':    None,
    'seen_drop_ids':    set(),
    'last_alert_ind':   {},
    'last_alert_comb':  0,
    'initialized':      False,
    'group_probs':      {},
    'drop_history':     [],
    'pressure_snapshots': [],     # [{t, counter, combined_pct}] para la gráfica
    'last_counter':     None,
    'last_counter_change_ts': None,  # epoch de la última vez que el counter subió
    'dead_alerted':     False,
}

# Flags de control desde panel/telegram
control = {'force_check': False, 'reset_requested': False}

# ── PERSISTENCIA ──────────────────────────────────────────────────────────────
def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        s = dict(state)
        s['seen_drop_ids'] = list(state['seen_drop_ids'])
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(s, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"No se pudo guardar estado: {e}")

def load_state():
    try:
        if not os.path.exists(STATE_FILE):
            log.info("No hay estado previo — arranque limpio")
            return False
        with open(STATE_FILE) as f:
            data = json.load(f)
        data['seen_drop_ids'] = set(data.get('seen_drop_ids', []))
        for k in state.keys():
            if k in data:
                state[k] = data[k]
        log.info(f"Estado recuperado — base_counter: {state.get('base_counter')}, "
                 f"drops: {len(state.get('drop_history', []))}")
        return True
    except Exception as e:
        log.error(f"No se pudo cargar estado: {e}")
        return False

def reset_state(new_counter):
    state['base_counter']  = new_counter
    state['last_any_rare'] = new_counter
    for g in RARE_GROUPS:
        state['group_last_seen'][g['key']] = new_counter
    state['drop_history']  = []
    state['pressure_snapshots'] = []
    state['last_alert_ind'] = {}
    state['last_alert_comb'] = 0
    state['dead_alerted']  = False
    save_state()

# ── HISTÓRICO ─────────────────────────────────────────────────────────────────
def parse_dt(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None

def compute_history(now_dt):
    hist = state['drop_history']
    parsed = [(parse_dt(d.get('created_at', '')), d) for d in hist]
    parsed = [(dt, d) for (dt, d) in parsed if dt]
    parsed.sort(key=lambda x: x[0])

    def within(hours):
        cutoff = now_dt.timestamp() - hours * 3600
        return [d for (dt, d) in parsed if dt.timestamp() >= cutoff]

    last24, last48 = within(24), within(48)
    by_item_24 = {}
    for d in last24:
        by_item_24[d['key']] = by_item_24.get(d['key'], 0) + 1

    intervals = [(parsed[i][0] - parsed[i-1][0]).total_seconds()/3600 for i in range(1, len(parsed))]
    avg_gap_h = round(sum(intervals)/len(intervals), 1) if intervals else None
    hours_since_last = round((now_dt.timestamp() - parsed[-1][0].timestamp())/3600, 1) if parsed else None
    span_h = round((parsed[-1][0].timestamp() - parsed[0][0].timestamp())/3600, 1) if len(parsed) >= 2 else None

    timeline = [{'name': d['name'], 'price': f"${d['price']:.2f}",
                 'created_at': d['created_at'], 'key': d['key'],
                 'pressure_individual': d.get('pressure_individual'),
                 'pressure_combined': d.get('pressure_combined'),
                 'boxes_individual': d.get('boxes_individual'),
                 'boxes_combined': d.get('boxes_combined')} for (dt, d) in reversed(parsed)]

    stats = {'count_24h': len(last24), 'count_48h': len(last48), 'by_item_24h': by_item_24,
             'avg_gap_h': avg_gap_h, 'hours_since_last': hours_since_last,
             'span_h': span_h, 'total_tracked': len(parsed)}
    return stats, timeline

# ── DASHBOARD SNAPSHOT ────────────────────────────────────────────────────────
dashboard = {
    'counter': None, 'updated': None, 'combined_pct': 0, 'boxes_since_any': 0,
    'hot_items': [], 'expected_every': 0, 'check_interval': CHECK_INTERVAL,
    'status': 'arrancando', 'history_24h': [], 'history_stats': {},
    'pressure_series': [], 'kpis': {}, 'case_url': CASE_WEB_URL,
    'dead': False,
}

def initialize(data):
    counter = data['counter']
    state['base_counter']   = counter
    state['last_any_rare']  = counter
    state['group_probs']    = data['group_probs']
    for g in RARE_GROUPS:
        state['group_last_seen'][g['key']] = counter
    state['seen_drop_ids']  = set(d['drop_id'] for d in data['drops'])
    state['last_counter']   = counter
    state['last_counter_change_ts'] = time.time()
    state['initialized']    = True

    seed = [d for d in data['drops'] if d['key'] is not None]
    seed.sort(key=lambda x: x.get('created_at', ''))
    state['drop_history'] = seed

    total_p = sum(data['group_probs'].values())
    expected = round(1 / total_p) if total_p > 0 else 0
    dashboard['expected_every'] = expected

    log.info(f"Inicializado — counter: {counter}, prob total: {total_p*100:.4f}%")
    lines = '\n'.join(f"• {g['name']}: {state['group_probs'][g['key']]*100:.3f}%" for g in RARE_GROUPS)
    send_telegram(
        f"🟢 <b>CS2 Monitor iniciado</b>\n"
        f"Counter base: <code>{counter:,}</code>\n"
        f"Drop raro esperado cada ~{expected:,} cajas\n\n"
        f"<b>Probabilidades:</b>\n{lines}"
    )

def process(data):
    counter = data['counter']
    drops   = data['drops']
    now     = time.time()
    now_dt  = datetime.utcnow()

    if not state['initialized']:
        initialize(data)

    if data['group_probs']:
        state['group_probs'] = data['group_probs']

    # ── Detección de caja muerta ────────────────────────────────────────────
    if state['last_counter'] is None:
        state['last_counter'] = counter
        state['last_counter_change_ts'] = now
    elif counter > state['last_counter']:
        state['last_counter'] = counter
        state['last_counter_change_ts'] = now
        state['dead_alerted'] = False
    else:
        stalled_min = (now - (state['last_counter_change_ts'] or now)) / 60
        if stalled_min >= DEAD_AFTER_MIN and not state['dead_alerted']:
            state['dead_alerted'] = True
            send_telegram(
                f"💀 <b>POSIBLE CAJA MUERTA</b>\n"
                f"El contador no sube desde hace {int(stalled_min)} min.\n"
                f"Counter: <code>{counter:,}</code>\n"
                f"<i>¿Se ha retirado la caja o cambió la API?</i>"
            )

    dead_now = state['dead_alerted']

    # ── Drops nuevos ────────────────────────────────────────────────────────
    new_drops = [d for d in drops if d['drop_id'] not in state['seen_drop_ids']]
    new_rares = [d for d in new_drops if d['key'] is not None]
    for d in drops:
        state['seen_drop_ids'].add(d['drop_id'])
    if len(state['seen_drop_ids']) > 500:
        state['seen_drop_ids'] = set(d['drop_id'] for d in drops)

    if new_rares:
        log.info(f"Nuevos raros: {[d['name'] for d in new_rares]}")

    # Calcular la presión que tenía cada drop JUSTO ANTES de caer (antes de resetear).
    # Esto se guarda en el propio drop para mostrarlo luego en el histórico.
    total_p_now = sum(state['group_probs'].values())
    for d in new_rares:
        key = d['key']
        p_item = state['group_probs'].get(key, 0)
        # cajas que llevaba ese item sin caer, en el momento del drop
        last_seen_item = state['group_last_seen'].get(key, state['base_counter'])
        n_item = max(0, counter - last_seen_item)
        # cajas sin caer ningún raro, en el momento del drop
        last_any = state['last_any_rare'] or state['base_counter']
        n_any = max(0, counter - last_any)
        d['pressure_individual'] = round(prob_acum(p_item, n_item), 1)
        d['pressure_combined']   = round(combined_prob(total_p_now, n_any), 1)
        d['boxes_individual']    = n_item
        d['boxes_combined']      = n_any

    for d in new_rares:
        state['group_last_seen'][d['key']] = counter
        state['drop_history'].append(d)
    if new_rares:
        state['last_any_rare'] = counter

    state['drop_history'].sort(key=lambda x: x.get('created_at', ''))
    if len(state['drop_history']) > 300:
        state['drop_history'] = state['drop_history'][-300:]

    # ── Presiones ────────────────────────────────────────────────────────────
    hot_items = []
    for g in RARE_GROUPS:
        p = state['group_probs'].get(g['key'], 0)
        last_seen = state['group_last_seen'].get(g['key'], state['base_counter'])
        n = max(0, counter - last_seen)
        hot_items.append({**g, 'prob': p, 'n': n, 'pct': prob_acum(p, n)})

    total_p = sum(state['group_probs'].values())
    boxes_since_any = max(0, counter - (state['last_any_rare'] or state['base_counter']))
    combined_pct = combined_prob(total_p, boxes_since_any)
    expected = round(1 / total_p) if total_p > 0 else 0

    # ── Snapshot de presión para la gráfica (cada check) ────────────────────
    state['pressure_snapshots'].append({
        't': now_dt.isoformat() + 'Z', 'counter': counter,
        'combined_pct': round(combined_pct, 2),
    })
    # mantener ~24h de snapshots (a 60s = 1440 puntos); cap a 2000
    if len(state['pressure_snapshots']) > 2000:
        state['pressure_snapshots'] = state['pressure_snapshots'][-2000:]

    # ── KPIs ─────────────────────────────────────────────────────────────────
    hist_stats, timeline = compute_history(now_dt)
    case_price = data.get('case_price', 0)
    ev = data.get('ev', 0)
    kpis = {
        'case_price': round(case_price, 2),
        'ev': round(ev, 4),
        'ev_ratio': round(ev / case_price * 100, 1) if case_price else 0,
        'total_prob_pct': round(total_p * 100, 4),
        'boxes_since_start': counter - state['base_counter'] if state['base_counter'] else 0,
    }

    dashboard.update({
        'counter': counter,
        'updated': now_dt.isoformat() + 'Z',
        'combined_pct': round(combined_pct, 2),
        'boxes_since_any': boxes_since_any,
        'expected_every': expected,
        'hot_items': [{'key': i['key'], 'name': i['name'], 'short': i['short'],
                       'n': i['n'], 'pct': round(i['pct'], 2),
                       'prob': round(i['prob']*100, 4),
                       'expected_every': round(1/i['prob']) if i['prob'] > 0 else 0} for i in hot_items],
        'history_24h': timeline,
        'history_stats': hist_stats,
        'pressure_series': state['pressure_snapshots'][-300:],  # últimos 300 puntos a la gráfica
        'kpis': kpis,
        'status': 'activo' if not dead_now else 'caja muerta',
        'dead': dead_now,
    })

    items_summary = ', '.join(i['short'] + ':' + str(round(i['pct'],1)) + '%' for i in hot_items)
    log.info(f"Counter: {counter} | Comb: {combined_pct:.1f}% ({boxes_since_any}) | {items_summary}")

    if state['initialized']:
        # Alertas individuales
        for item in hot_items:
            if item['pct'] >= THRESHOLD_IND:
                last = state['last_alert_ind'].get(item['key'], 0)
                if now - last > ALERT_COOLDOWN:
                    state['last_alert_ind'][item['key']] = now
                    send_telegram(
                        f"🔥 <b>MOMENTO CALIENTE — {item['name']}</b>\n\n"
                        f"Probabilidad acumulada: <b>{item['pct']:.1f}%</b>\n"
                        f"Cajas sin caer: <code>{item['n']:,}</code>\n"
                        f"Counter: <code>{counter:,}</code>"
                    )
        # Sequía global
        if combined_pct >= THRESHOLD_COMB and now - state['last_alert_comb'] > ALERT_COOLDOWN:
            state['last_alert_comb'] = now
            bars = ''.join(
                f"{'🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'} {i['short']}: {i['pct']:.1f}%\n"
                for i in hot_items
            )
            send_telegram(
                f"⚠️ <b>SEQUÍA GLOBAL — {combined_pct:.1f}%</b>\n\n"
                f"<code>{boxes_since_any:,}</code> cajas sin ningún raro\n"
                f"(esperado cada ~{expected:,})\n\n{bars}"
            )
        # Drops nuevos
        for d in new_rares:
            pi = d.get('pressure_individual')
            pc = d.get('pressure_combined')
            press_line = ""
            if pi is not None or pc is not None:
                press_line = (f"Presión al caer — global: <b>{pc}%</b> · "
                              f"item: <b>{pi}%</b>\n")
            send_telegram(
                f"✅ <b>DROP — {d['name']}</b>\n"
                f"Precio: ${d['price']:.2f} · Counter: <code>{counter:,}</code>\n"
                f"{press_line}"
                f"<i>Contador de {group_name(d['key'])} reseteado</i>"
            )

    save_state()

# ── COMANDOS DE TELEGRAM (long polling en hilo aparte) ────────────────────────
def telegram_status_text():
    d = dashboard
    if not d.get('counter'):
        return "⏳ Aún sin datos. Espera al primer check."
    lines = [f"📊 <b>Estado actual</b>",
             f"Counter: <code>{d['counter']:,}</code>",
             f"Sequía global: <b>{d['combined_pct']:.1f}%</b> ({d['boxes_since_any']:,} cajas)",
             ""]
    for i in d['hot_items']:
        emoji = '🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'
        lines.append(f"{emoji} {i['short']}: {i['pct']:.1f}% · {i['n']:,} cajas")
    hs = d.get('history_stats', {})
    if hs:
        lines.append("")
        lines.append(f"24h: {hs.get('count_24h',0)} raros · último hace {hs.get('hours_since_last','?')}h")
    return '\n'.join(lines)

def control_keyboard():
    return {'inline_keyboard': [
        [{'text': '📊 Estado', 'callback_data': 'status'},
         {'text': '🔄 Check ahora', 'callback_data': 'check'}],
        [{'text': '📜 Histórico', 'callback_data': 'history'},
         {'text': '🌐 Ir a la caja', 'url': CASE_WEB_URL}],
        [{'text': '♻️ Reset (confirmar)', 'callback_data': 'reset_confirm'}],
    ]}

def telegram_history_text():
    d = dashboard
    hs = d.get('history_stats', {})
    tl = d.get('history_24h', [])[:12]
    if not tl:
        return "Sin histórico aún."
    lines = [f"📜 <b>Histórico de drops</b>",
             f"Total registrados: {hs.get('total_tracked',0)} · 24h: {hs.get('count_24h',0)}",
             f"Intervalo medio: {hs.get('avg_gap_h','?')}h · último hace {hs.get('hours_since_last','?')}h", ""]
    for x in tl:
        lines.append(f"• {x['created_at']} — {x['name']} ({x['price']})")
    return '\n'.join(lines)

def answer_callback(cb_id, text=None):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={'callback_query_id': cb_id, 'text': text or ''}, timeout=10)
    except Exception as e:
        log.error(f"answerCallback error: {e}")

def handle_command(text, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        return  # solo responde al dueño
    cmd = text.strip().lower().lstrip('/')
    if cmd in ('start', 'menu', 'help', 'ayuda'):
        send_telegram("🎛 <b>Panel de control</b>\nUsa los botones:", control_keyboard())
    elif cmd in ('status', 'estado'):
        send_telegram(telegram_status_text(), control_keyboard())
    elif cmd in ('check', 'forzar'):
        control['force_check'] = True
        send_telegram("🔄 Check forzado en marcha...")
    elif cmd in ('history', 'historico', 'histórico'):
        send_telegram(telegram_history_text())
    elif cmd == 'reset':
        send_telegram("⚠️ ¿Seguro que quieres resetear el conteo? Esto borra el histórico acumulado.",
                      {'inline_keyboard': [[
                          {'text': '✅ Sí, resetear', 'callback_data': 'reset_yes'},
                          {'text': '❌ Cancelar', 'callback_data': 'reset_no'}]]})
    else:
        send_telegram("No reconozco ese comando. Pulsa /menu para ver las opciones.")

def handle_callback(data, cb_id, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        answer_callback(cb_id)
        return
    if data == 'status':
        answer_callback(cb_id)
        send_telegram(telegram_status_text(), control_keyboard())
    elif data == 'check':
        control['force_check'] = True
        answer_callback(cb_id, "Check forzado")
        send_telegram("🔄 Check forzado en marcha...")
    elif data == 'history':
        answer_callback(cb_id)
        send_telegram(telegram_history_text())
    elif data == 'reset_confirm':
        answer_callback(cb_id)
        send_telegram("⚠️ ¿Seguro que quieres resetear el conteo? Borra el histórico acumulado.",
                      {'inline_keyboard': [[
                          {'text': '✅ Sí, resetear', 'callback_data': 'reset_yes'},
                          {'text': '❌ Cancelar', 'callback_data': 'reset_no'}]]})
    elif data == 'reset_yes':
        control['reset_requested'] = True
        answer_callback(cb_id, "Reseteando...")
        send_telegram("♻️ Reset solicitado. Se aplicará en el próximo check.")
    elif data == 'reset_no':
        answer_callback(cb_id, "Cancelado")
        send_telegram("❌ Reset cancelado.")

def telegram_poll_loop():
    offset = None
    # purgar updates antiguos al arrancar
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                         params={'timeout': 0}, timeout=15).json()
        if r.get('result'):
            offset = r['result'][-1]['update_id'] + 1
    except Exception:
        pass
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params={'timeout': 25, 'offset': offset}, timeout=30).json()
            for upd in r.get('result', []):
                offset = upd['update_id'] + 1
                if 'message' in upd and 'text' in upd['message']:
                    handle_command(upd['message']['text'], upd['message']['chat']['id'])
                elif 'callback_query' in upd:
                    cq = upd['callback_query']
                    handle_callback(cq.get('data',''), cq['id'], cq['message']['chat']['id'])
        except Exception as e:
            log.error(f"Telegram poll error: {e}")
            time.sleep(5)

# ── PANEL WEB ─────────────────────────────────────────────────────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer

DASHBOARD_HTML = r"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CS2 Monitor</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;500;600;700&display=swap');
:root{--bg:#0a0c10;--bg2:#0f1318;--bg3:#161b24;--bd:#1e2a3a;--cy:#4af3ff;--or:#ff6b35;--gold:#f0a500;--red:#ff3b5c;--grn:#39e58c;--tx:#c8d8e8;--mut:#5a7088;--mut2:#4a6070}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Rajdhani',sans-serif;padding:14px;max-width:780px;margin:0 auto;padding-bottom:40px}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.05) 2px,rgba(0,0,0,.05) 4px);pointer-events:none;z-index:999}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
h1{font-family:'Share Tech Mono',monospace;font-size:15px;color:var(--cy);letter-spacing:3px}
.live{display:flex;align-items:center;gap:6px;font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--mut2);text-transform:uppercase}
.live .d{width:7px;height:7px;border-radius:50%;background:var(--grn);box-shadow:0 0 6px var(--grn);animation:pulse 2s infinite}
.live.dead .d{background:var(--red);box-shadow:0 0 6px var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.sub{font-size:11px;color:var(--mut2);font-family:'Share Tech Mono',monospace;margin-bottom:14px}

/* combined hero */
.combined{background:linear-gradient(135deg,var(--bg2),var(--bg3));border:1px solid var(--bd);border-radius:8px;padding:16px;margin-bottom:14px;position:relative;overflow:hidden}
.combined.warm{border-color:var(--or)}.combined.hot{border-color:var(--red);box-shadow:0 0 20px rgba(255,59,92,.35)}
.combined::after{content:'';position:absolute;top:0;right:0;width:120px;height:120px;background:radial-gradient(circle,rgba(74,243,255,.06),transparent 70%);pointer-events:none}
.cmb-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.cmb-lbl{font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:2px;color:var(--mut2);text-transform:uppercase}
.cmb-big{font-family:'Share Tech Mono',monospace;font-size:38px;font-weight:700;line-height:1;color:var(--cy)}
.cmb-big.warm{color:var(--or)}.cmb-big.hot{color:var(--red)}
.track{height:10px;background:var(--bg);border-radius:5px;overflow:hidden;margin-bottom:8px;border:1px solid var(--bd)}
.fill{height:100%;border-radius:5px;background:linear-gradient(90deg,#1e4060,var(--cy));transition:width .8s cubic-bezier(.4,0,.2,1)}
.fill.warm{background:linear-gradient(90deg,#4a2010,var(--or))}.fill.hot{background:linear-gradient(90deg,#6a0000,var(--red))}
.cmb-foot{display:flex;justify-content:space-between;align-items:center}
.cmb-boxes{font-size:15px;font-weight:600}
.cmb-boxes b{font-family:'Share Tech Mono',monospace;color:var(--cy);font-size:17px}
.cmb-exp{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--mut2)}

/* KPI strip */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.kpi{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:9px 6px;text-align:center}
.kpi-v{font-family:'Share Tech Mono',monospace;font-size:15px;color:var(--cy)}
.kpi-v.neg{color:var(--red)}.kpi-v.pos{color:var(--grn)}
.kpi-l{font-size:9px;color:var(--mut2);text-transform:uppercase;letter-spacing:.5px;margin-top:3px;line-height:1.1}

/* section */
.sec{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--mut2);letter-spacing:2px;text-transform:uppercase;margin:18px 0 10px;display:flex;align-items:center;gap:8px}
.sec::after{content:'';flex:1;height:1px;background:var(--bd)}

/* item cards — REDISEÑO legibilidad */
.item{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:11px 12px;margin-bottom:8px;transition:border-color .3s}
.item.warm{border-color:rgba(255,107,53,.5)}.item.hot{border-color:rgba(255,59,92,.6);box-shadow:0 0 10px rgba(255,59,92,.15)}
.item-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.item-name{font-size:14px;font-weight:600;display:flex;align-items:center;gap:7px}
.item-prob{font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--mut2);background:var(--bg);padding:2px 6px;border-radius:3px;border:1px solid var(--bd)}
.item-pct{font-family:'Share Tech Mono',monospace;font-size:18px;font-weight:700;color:var(--mut2)}
.item-pct.warm{color:var(--or)}.item-pct.hot{color:var(--red)}
.itrack{height:6px;background:var(--bg);border-radius:3px;overflow:hidden;margin-bottom:8px;border:1px solid var(--bd)}
.ifill{height:100%;border-radius:3px;background:linear-gradient(90deg,#1e4060,var(--cy));transition:width .6s}
.ifill.warm{background:linear-gradient(90deg,#4a2010,var(--or))}.ifill.hot{background:linear-gradient(90deg,#6a0000,var(--red))}
.item-foot{display:flex;justify-content:space-between;align-items:center;font-size:12px}
.item-stat{display:flex;align-items:center;gap:5px;color:var(--mut2)}
.item-stat b{font-family:'Share Tech Mono',monospace;color:var(--tx);font-size:14px;font-weight:600}
.item-stat .u{font-size:10px;text-transform:uppercase;letter-spacing:.5px}

.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.covert{background:#eb4b4b}.classified{background:#8847ff}.milspec{background:#5e98d9}.consumer{background:#b0b0b0}

/* chart */
.chart-wrap{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:12px}
#chart{width:100%;height:140px;display:block}
.chart-legend{display:flex;justify-content:space-between;font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--mut2);margin-top:6px}

/* history */
.hstats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.hstat{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:9px;text-align:center}
.hstat-v{font-family:'Share Tech Mono',monospace;font-size:16px;color:var(--cy)}
.hstat-l{font-size:9px;color:var(--mut2);text-transform:uppercase;margin-top:2px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{font-family:'Share Tech Mono',monospace;font-size:10px;padding:4px 9px;border:1px solid var(--bd);border-radius:4px;display:flex;align-items:center;gap:5px;background:var(--bg2)}
.tl-toggle{width:100%;padding:11px;border-radius:6px;cursor:pointer;font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;border:1px solid var(--bd);background:var(--bg2);color:var(--cy);transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px}
.tl-toggle:active{background:var(--bg3)}
.tl-toggle .arrow{transition:transform .3s;font-size:9px}
.tl-toggle.open .arrow{transform:rotate(180deg)}
.tl-wrap{max-height:0;overflow:hidden;transition:max-height .35s ease}
.tl-wrap.open{max-height:520px;margin-top:8px}
.tl{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;overflow-y:auto;max-height:520px}
.tl::-webkit-scrollbar{width:6px}.tl::-webkit-scrollbar-track{background:var(--bg)}.tl::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
.drop{display:flex;align-items:center;gap:9px;padding:9px 11px;border-bottom:1px solid rgba(30,42,58,.5)}
.drop:last-child{border-bottom:none}
.drop-i{flex:1;min-width:0}.drop-n{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.drop-t{font-size:10px;color:var(--mut2);font-family:'Share Tech Mono',monospace}
.drop-press{display:flex;gap:5px;margin-top:3px;flex-wrap:wrap}
.pbadge{font-family:'Share Tech Mono',monospace;font-size:9px;padding:1px 5px;border-radius:3px;border:1px solid var(--bd);color:var(--mut2)}
.pbadge.hot{color:var(--red);border-color:rgba(255,59,92,.4)}
.pbadge.warm{color:var(--or);border-color:rgba(255,107,53,.4)}
.pbadge b{color:var(--tx)}
.drop-p{font-family:'Share Tech Mono',monospace;font-size:13px;color:var(--gold);flex-shrink:0;align-self:flex-start}

/* actions */
.actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:16px 0}
.btn{padding:11px 8px;border-radius:6px;cursor:pointer;font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;border:1px solid;background:transparent;transition:all .2s;text-align:center;text-decoration:none;display:flex;align-items:center;justify-content:center;gap:5px}
.btn-check{border-color:var(--cy);color:var(--cy)}.btn-check:active{background:rgba(74,243,255,.15)}
.btn-web{border-color:var(--gold);color:var(--gold)}.btn-web:active{background:rgba(240,165,0,.15)}
.btn-reset{border-color:var(--red);color:var(--red)}.btn-reset:active{background:rgba(255,59,92,.15)}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--bg3);border:1px solid var(--cy);color:var(--cy);font-family:'Share Tech Mono',monospace;font-size:12px;padding:10px 18px;border-radius:6px;transition:transform .3s;z-index:1000}
.toast.show{transform:translateX(-50%) translateY(0)}
/* modal */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:1001;padding:20px}
.modal.show{display:flex}
.modal-box{background:var(--bg2);border:1px solid var(--red);border-radius:8px;padding:20px;max-width:340px;text-align:center}
.modal-box h3{font-family:'Share Tech Mono',monospace;font-size:13px;color:var(--red);letter-spacing:1px;margin-bottom:10px}
.modal-box p{font-size:13px;color:var(--tx);margin-bottom:16px;line-height:1.4}
.modal-btns{display:flex;gap:10px}
.modal-btns .btn{flex:1}
.btn-cancel{border-color:var(--mut2);color:var(--mut2)}
.dead-banner{display:none;background:rgba(255,59,92,.12);border:1px solid var(--red);border-radius:6px;padding:10px 14px;margin-bottom:14px;font-family:'Share Tech Mono',monospace;font-size:12px;color:var(--red);letter-spacing:1px}
.dead-banner.show{display:block}
</style></head><body>
<div class="hdr"><h1>CS2 CASE MONITOR</h1><div class="live" id="live"><span class="d"></span><span id="liveTxt">EN VIVO</span></div></div>
<div class="sub" id="sub">cargando...</div>

<div class="dead-banner" id="deadBanner">💀 CAJA MUERTA — el contador no sube. Posible retirada de la caja.</div>

<div class="combined" id="cCard">
  <div class="cmb-top"><span class="cmb-lbl">Sequía global · cualquier raro</span><span class="cmb-big" id="cPct">—</span></div>
  <div class="track"><div class="fill" id="cFill" style="width:0%"></div></div>
  <div class="cmb-foot"><span class="cmb-boxes"><b id="cBoxes">—</b> cajas sin caer ningún raro</span><span class="cmb-exp" id="cExp"></span></div>
</div>

<div class="kpis">
  <div class="kpi"><div class="kpi-v" id="kCounter">—</div><div class="kpi-l">Total cajas</div></div>
  <div class="kpi"><div class="kpi-v" id="kStart">—</div><div class="kpi-l">Desde inicio</div></div>
  <div class="kpi"><div class="kpi-v" id="kEv">—</div><div class="kpi-l">Valor esper.</div></div>
  <div class="kpi"><div class="kpi-v" id="kRoi">—</div><div class="kpi-l">Retorno</div></div>
</div>

<div class="actions">
  <button class="btn btn-check" id="btnCheck">🔄 Check ya</button>
  <a class="btn btn-web" id="btnWeb" href="#" target="_blank">🌐 Ir a caja</a>
  <button class="btn btn-reset" id="btnReset">♻️ Reset</button>
</div>

<div class="sec">Presión individual por item</div>
<div id="items"></div>

<div class="sec">Evolución de la presión combinada</div>
<div class="chart-wrap"><canvas id="chart"></canvas><div class="chart-legend"><span id="chartFrom">—</span><span>presión combinada %</span><span id="chartTo">ahora</span></div></div>

<div class="sec">Histórico de drops raros</div>
<div class="hstats">
  <div class="hstat"><div class="hstat-v" id="h24">—</div><div class="hstat-l">Raros 24h</div></div>
  <div class="hstat"><div class="hstat-v" id="hGap">—</div><div class="hstat-l">Intervalo medio</div></div>
  <div class="hstat"><div class="hstat-v" id="hLast">—</div><div class="hstat-l">Último hace</div></div>
</div>
<div class="chips" id="chips"></div>
<button class="tl-toggle" id="tlToggle">📜 Ver histórico de drops <span class="arrow">▼</span></button>
<div class="tl-wrap" id="tlWrap"><div class="tl" id="timeline"></div></div>

<div class="toast" id="toast"></div>
<div class="modal" id="modal"><div class="modal-box">
  <h3>⚠️ Confirmar reset</h3>
  <p>Esto borra el conteo y el histórico acumulado, empezando de cero desde el contador actual. No se puede deshacer.</p>
  <div class="modal-btns">
    <button class="btn btn-cancel" id="mCancel">Cancelar</button>
    <button class="btn btn-reset" id="mConfirm">Sí, resetear</button>
  </div>
</div></div>

<script>
const RAR={butterfly:'covert',spec_gloves:'covert',awp:'covert',sport_gloves:'covert',ak47:'classified'};
const NAMES={butterfly:'Butterfly',spec_gloves:'Spec Gloves',awp:'AWP QG',sport_gloves:'Sport Gloves',ak47:'AK Vulcan'};
function zone(p){return p>=90?'hot':p>=70?'warm':''}
function fmtDrop(n){const p=n.split('|');return p.length>1?`${p[0].trim()} ${p[1].trim()}`:n}
function nf(n){return (n||0).toLocaleString('es-ES')}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}

function drawChart(series){
  const cv=document.getElementById('chart');const dpr=window.devicePixelRatio||1;
  const w=cv.clientWidth,h=140;cv.width=w*dpr;cv.height=h*dpr;
  const ctx=cv.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
  if(!series||series.length<2){ctx.fillStyle='#4a6070';ctx.font="11px 'Share Tech Mono'";ctx.fillText('acumulando datos...',10,h/2);return}
  const pad=4;const vals=series.map(s=>s.combined_pct);
  const maxV=Math.max(100,...vals);const minV=0;
  const xs=(i)=>pad+(i/(series.length-1))*(w-2*pad);
  const ys=(v)=>h-pad-((v-minV)/(maxV-minV))*(h-2*pad);
  // grid 90% line
  ctx.strokeStyle='rgba(255,59,92,.25)';ctx.lineWidth=1;ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(pad,ys(90));ctx.lineTo(w-pad,ys(90));ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='rgba(255,59,92,.5)';ctx.font="9px 'Share Tech Mono'";ctx.fillText('90%',w-30,ys(90)-3);
  // area
  const grad=ctx.createLinearGradient(0,0,0,h);grad.addColorStop(0,'rgba(74,243,255,.25)');grad.addColorStop(1,'rgba(74,243,255,0)');
  ctx.beginPath();ctx.moveTo(xs(0),ys(vals[0]));
  for(let i=1;i<vals.length;i++)ctx.lineTo(xs(i),ys(vals[i]));
  ctx.lineTo(xs(vals.length-1),h-pad);ctx.lineTo(xs(0),h-pad);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  // line
  ctx.beginPath();ctx.moveTo(xs(0),ys(vals[0]));
  for(let i=1;i<vals.length;i++)ctx.lineTo(xs(i),ys(vals[i]));
  const last=vals[vals.length-1];ctx.strokeStyle=last>=90?'#ff3b5c':last>=70?'#ff6b35':'#4af3ff';ctx.lineWidth=2;ctx.stroke();
  // last point
  ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.arc(xs(vals.length-1),ys(last),3,0,7);ctx.fill();
}

async function load(){
  try{
    const d=await(await fetch('/data')).json();
    const cls=zone(d.combined_pct);
    document.getElementById('cCard').className='combined '+cls;
    document.getElementById('cPct').className='cmb-big '+cls;
    document.getElementById('cPct').textContent=d.combined_pct.toFixed(1)+'%';
    document.getElementById('cFill').className='fill '+cls;
    document.getElementById('cFill').style.width=Math.min(d.combined_pct,100)+'%';
    document.getElementById('cBoxes').textContent=nf(d.boxes_since_any);
    document.getElementById('cExp').textContent='media: 1 raro cada '+nf(d.expected_every)+' cajas';
    document.getElementById('cExp').title='Sale de sumar las probabilidades de todos los raros ('+(d.kpis&&d.kpis.total_prob_pct||'')+'%) e invertir. Es fijo salvo que la caja cambie.';
    // KPIs
    const k=d.kpis||{};
    document.getElementById('kCounter').textContent=d.counter?nf(d.counter):'—';
    document.getElementById('kStart').textContent='+'+nf(k.boxes_since_start);
    document.getElementById('kEv').textContent=k.ev!=null?'$'+k.ev.toFixed(2):'—';
    const roiEl=document.getElementById('kRoi');roiEl.textContent=k.ev_ratio!=null?k.ev_ratio+'%':'—';
    roiEl.className='kpi-v '+(k.ev_ratio>=100?'pos':'neg');
    // status
    document.getElementById('btnWeb').href=d.case_url||'#';
    const upd=d.updated?new Date(d.updated):null;
    document.getElementById('sub').textContent=upd?('Actualizado '+upd.toLocaleTimeString('es-ES')+' · check cada '+d.check_interval+'s'):'sin datos';
    const live=document.getElementById('live');
    document.getElementById('deadBanner').classList.toggle('show',!!d.dead);
    live.className='live'+(d.dead?' dead':'');
    document.getElementById('liveTxt').textContent=d.dead?'CAJA MUERTA':'EN VIVO';
    // items
    document.getElementById('items').innerHTML=(d.hot_items||[]).map(i=>{
      const c=zone(i.pct);
      return `<div class="item ${c}">
        <div class="item-top">
          <span class="item-name"><span class="dot ${RAR[i.key]}"></span>${i.name}<span class="item-prob">${i.prob}%</span></span>
          <span class="item-pct ${c}">${i.pct.toFixed(1)}%</span>
        </div>
        <div class="itrack"><div class="ifill ${c}" style="width:${Math.min(i.pct,100)}%"></div></div>
        <div class="item-foot">
          <span class="item-stat"><b>${nf(i.n)}</b><span class="u">cajas desde su último drop</span></span>
          <span class="item-stat"><span class="u">esperado cada</span><b>${nf(i.expected_every)}</b></span>
        </div>
      </div>`;
    }).join('');
    // chart
    drawChart(d.pressure_series||[]);
    const ps=d.pressure_series||[];
    if(ps.length){document.getElementById('chartFrom').textContent=new Date(ps[0].t).toLocaleTimeString('es-ES',{hour:'2-digit',minute:'2-digit'})}
    // history
    const hs=d.history_stats||{};
    document.getElementById('h24').textContent=hs.count_24h!=null?hs.count_24h:'—';
    document.getElementById('hGap').textContent=hs.avg_gap_h!=null?hs.avg_gap_h+'h':'—';
    document.getElementById('hLast').textContent=hs.hours_since_last!=null?hs.hours_since_last+'h':'—';
    const byItem=hs.by_item_24h||{};
    document.getElementById('chips').innerHTML=Object.keys(NAMES).map(key=>{
      const n=byItem[key]||0;const col=n>0?'var(--cy)':'var(--mut2)';
      return `<span class="chip" style="color:${col}"><span class="dot ${RAR[key]}"></span>${NAMES[key]}: ${n}</span>`;
    }).join('');
    document.getElementById('timeline').innerHTML=(d.history_24h||[]).map(x=>{
      let press='';
      if(x.pressure_combined!=null||x.pressure_individual!=null){
        const zc=x.pressure_combined>=90?'hot':x.pressure_combined>=70?'warm':'';
        const zi=x.pressure_individual>=90?'hot':x.pressure_individual>=70?'warm':'';
        const cc=x.pressure_combined!=null?`<span class="pbadge ${zc}" title="presión global al caer">GLOBAL <b>${x.pressure_combined}%</b></span>`:'';
        const ci=x.pressure_individual!=null?`<span class="pbadge ${zi}" title="presión de este item al caer">ITEM <b>${x.pressure_individual}%</b></span>`:'';
        press=`<div class="drop-press">${cc}${ci}</div>`;
      } else {
        press=`<div class="drop-press"><span class="pbadge">sin datos de presión</span></div>`;
      }
      return `<div class="drop"><span class="dot ${RAR[x.key]||'milspec'}" style="margin-top:3px"></span><div class="drop-i"><div class="drop-n">${fmtDrop(x.name)}</div><div class="drop-t">${x.created_at||''} UTC</div>${press}</div><div class="drop-p">${x.price}</div></div>`;
    }).join('')||'<div style="padding:12px;font-size:11px;color:var(--mut2);font-family:monospace">acumulando histórico...</div>';
  }catch(e){document.getElementById('sub').textContent='error cargando'}
}

document.getElementById('btnCheck').addEventListener('click',async()=>{
  toast('Forzando check...');try{await fetch('/action/check',{method:'POST'})}catch(e){}
  setTimeout(load,3000);
});
document.getElementById('btnReset').addEventListener('click',()=>document.getElementById('modal').classList.add('show'));
document.getElementById('mCancel').addEventListener('click',()=>document.getElementById('modal').classList.remove('show'));
document.getElementById('mConfirm').addEventListener('click',async()=>{
  document.getElementById('modal').classList.remove('show');toast('Reseteando...');
  try{await fetch('/action/reset',{method:'POST'})}catch(e){}setTimeout(load,2000);
});
document.getElementById('tlToggle').addEventListener('click',()=>{
  const w=document.getElementById('tlWrap');const b=document.getElementById('tlToggle');
  const open=w.classList.toggle('open');b.classList.toggle('open',open);
  b.firstChild.textContent=open?'📜 Ocultar histórico ':'📜 Ver histórico de drops ';
});
load();setInterval(load,5000);
window.addEventListener('resize',load);
</script></body></html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        if self.path == '/action/check':
            control['force_check'] = True
            self._json({'ok': True})
        elif self.path == '/action/reset':
            control['reset_requested'] = True
            self._json({'ok': True})
        else:
            self._json({'ok': False}, 404)
    def do_GET(self):
        if self.path == '/data':
            self._json(dashboard)
        else:
            body = DASHBOARD_HTML.encode()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)

def start_web():
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()

# ── LOOP ──────────────────────────────────────────────────────────────────────
def do_check():
    data = fetch_data()
    if data and data.get('counter'):
        if control['reset_requested']:
            control['reset_requested'] = False
            reset_state(data['counter'])
            send_telegram(f"♻️ <b>Conteo reseteado</b>\nNueva base: <code>{data['counter']:,}</code>")
        process(data)
        return True
    return False

def main():
    log.info("CS2 Monitor (API) arrancando...")
    threading.Thread(target=start_web, daemon=True).start()
    threading.Thread(target=telegram_poll_loop, daemon=True).start()

    recovered = load_state()
    if recovered and state.get('base_counter'):
        send_telegram(
            f"♻️ <b>Monitor reanudado</b>\n"
            f"Counter base: <code>{state['base_counter']:,}</code>\n"
            f"Drops en histórico: {len(state.get('drop_history', []))}\n"
            f"<i>No se ha perdido el conteo.</i>", control_keyboard()
        )
    else:
        send_telegram("⏳ <b>CS2 Monitor desplegado</b> — conectando...")

    fails = 0
    last_check = 0
    while True:
        now = time.time()
        due = (now - last_check) >= CHECK_INTERVAL
        forced = control['force_check']
        if due or forced:
            if forced:
                control['force_check'] = False
                log.info("Check forzado")
            ok = do_check()
            last_check = time.time()
            if ok:
                fails = 0
            else:
                fails += 1
                dashboard['status'] = 'sin datos'
                if fails == 3:
                    send_telegram("⚠️ <b>Aviso:</b> 3 fallos seguidos leyendo la API.")
        time.sleep(1)

if __name__ == '__main__':
    main()

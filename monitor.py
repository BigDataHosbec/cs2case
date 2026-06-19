import os, time, logging, json, threading
from datetime import datetime
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
CASE_ID          = os.environ.get('CASE_ID', 'cc-bf4940a7e')
API_URL          = f"https://gate.skin.club/apiv2/cases/{CASE_ID}"
CHECK_INTERVAL   = int(os.environ.get('CHECK_INTERVAL', '60'))
THRESHOLD_IND    = float(os.environ.get('THRESHOLD_IND', '90'))
THRESHOLD_COMB   = float(os.environ.get('THRESHOLD_COMB', '90'))
ALERT_COOLDOWN   = int(os.environ.get('ALERT_COOLDOWN', '300'))
PORT             = int(os.environ.get('PORT', '8080'))

# ── ITEMS RAROS (se identifican por market_hash_name de la API) ───────────────
# La API da las probabilidades exactas; aquí agrupamos por "item lógico"
# (varias variantes de wear cuentan como el mismo item para el tracking)
RARE_GROUPS = [
    {'key': 'butterfly',    'name': '★ Butterfly Knife Case Hardened', 'match': ['butterfly knife']},
    {'key': 'spec_gloves',  'name': '★ Specialist Gloves Blackbook',   'match': ['specialist gloves']},
    {'key': 'awp',          'name': 'AWP Queen\'s Gambit',             'match': ["queen's gambit"]},
    {'key': 'sport_gloves', 'name': '★ Sport Gloves Frosty',           'match': ['sport gloves']},
    {'key': 'ak47',         'name': 'AK-47 Vulcan',                    'match': ['vulcan']},
]

def match_group(name):
    low = name.lower()
    for g in RARE_GROUPS:
        if any(m in low for m in g['match']):
            return g['key']
    return None

# ── MATH ──────────────────────────────────────────────────────────────────────
def prob_acum(p, n):
    if p <= 0 or n <= 0: return 0.0
    return (1 - (1 - p) ** n) * 100

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
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

        counter = d['stats']['opening_count']

        # Probabilidades exactas por grupo (sumando variantes de wear)
        group_probs = {g['key']: 0.0 for g in RARE_GROUPS}
        contents = (d.get('last_successful_generation') or {}).get('contents', [])
        for c in contents:
            name = c['item']['market_hash_name']
            key = match_group(name)
            if key:
                group_probs[key] += float(c['chance_percent']) / 100.0

        # Feed de drops (top_drops) con timestamp e item
        drops = []
        for t in d.get('top_drops', []):
            item = (t.get('reason') or {}).get('item') or t.get('item') or {}
            name = item.get('market_hash_name', '')
            if not name:
                continue
            drops.append({
                'name': name,
                'price': t['price'] / 100.0,
                'created_at': t.get('created_at', ''),
                'drop_id': t.get('id'),
                'key': match_group(name),
            })

        return {'counter': counter, 'group_probs': group_probs, 'drops': drops}
    except Exception as e:
        log.error(f"Fetch error: {e}")
        return None

# ── ESTADO ────────────────────────────────────────────────────────────────────
state = {
    'base_counter':     None,
    'group_last_seen':  {},   # key -> counter en que se vio por última vez ese item
    'last_any_rare':    None, # counter del último drop raro cualquiera
    'seen_drop_ids':    set(),# ids de drops ya procesados
    'last_alert_ind':   {},
    'last_alert_comb':  0,
    'initialized':      False,
    'group_probs':      {},
    'drop_history':     [],   # histórico acumulado de drops raros
}

dashboard = {
    'counter': None, 'updated': None, 'combined_pct': 0, 'boxes_since_any': 0,
    'hot_items': [], 'last_drops': [], 'expected_every': 0,
    'check_interval': CHECK_INTERVAL, 'status': 'arrancando',
    'history_24h': [], 'history_stats': {},
}

def combined_prob(probs_sum, n):
    # P(al menos 1) = 1 - prod (1-pi)^n  ≈  1 - (1 - sum_pi)^n para p pequeñas
    if n <= 0: return 0.0
    return (1 - (1 - probs_sum) ** n) * 100

def parse_dt(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None

def compute_history(now_dt):
    """Estadísticas del histórico de drops raros."""
    hist = state['drop_history']
    parsed = []
    for d in hist:
        dt = parse_dt(d.get('created_at', ''))
        if dt:
            parsed.append((dt, d))
    parsed.sort(key=lambda x: x[0])

    def within(hours):
        cutoff = now_dt.timestamp() - hours * 3600
        return [d for (dt, d) in parsed if dt.timestamp() >= cutoff]

    last24 = within(24)
    last48 = within(48)

    by_item_24 = {}
    for d in last24:
        by_item_24[d['key']] = by_item_24.get(d['key'], 0) + 1

    intervals = []
    for i in range(1, len(parsed)):
        intervals.append((parsed[i][0] - parsed[i-1][0]).total_seconds() / 3600)
    avg_gap_h = round(sum(intervals) / len(intervals), 1) if intervals else None

    hours_since_last = None
    if parsed:
        hours_since_last = round((now_dt.timestamp() - parsed[-1][0].timestamp()) / 3600, 1)

    span_h = None
    if len(parsed) >= 2:
        span_h = round((parsed[-1][0].timestamp() - parsed[0][0].timestamp()) / 3600, 1)

    timeline = [{
        'name': d['name'], 'price': f"${d['price']:.2f}",
        'created_at': d['created_at'], 'key': d['key']
    } for (dt, d) in reversed(parsed)]

    stats = {
        'count_24h': len(last24),
        'count_48h': len(last48),
        'by_item_24h': by_item_24,
        'avg_gap_h': avg_gap_h,
        'hours_since_last': hours_since_last,
        'span_h': span_h,
        'total_tracked': len(parsed),
    }
    return stats, timeline

def initialize(data):
    counter = data['counter']
    state['base_counter']   = counter
    state['last_any_rare']  = counter
    state['group_probs']    = data['group_probs']
    for g in RARE_GROUPS:
        state['group_last_seen'][g['key']] = counter
    state['seen_drop_ids']  = set(d['drop_id'] for d in data['drops'])
    state['initialized']    = True

    # Sembrar el histórico con los drops raros que ya vienen en el feed (~49h reales)
    seed = [d for d in data['drops'] if d['key'] is not None]
    seed.sort(key=lambda x: x.get('created_at', ''))
    state['drop_history'] = seed

    total_p = sum(data['group_probs'].values())
    expected = round(1 / total_p) if total_p > 0 else 0
    dashboard['expected_every'] = expected

    log.info(f"Inicializado — counter: {counter}, prob total: {total_p*100:.4f}%")
    lines = '\n'.join(f"• {g['name']}: {state['group_probs'][g['key']]*100:.3f}%" for g in RARE_GROUPS)
    send_telegram(
        f"🟢 <b>CS2 Monitor iniciado (API)</b>\n"
        f"Caja: DONT TRUST\n"
        f"Counter base: <code>{counter:,}</code>\n"
        f"Drop raro esperado cada ~{expected:,} cajas\n\n"
        f"<b>Probabilidades:</b>\n{lines}\n\n"
        f"Chequeando cada {CHECK_INTERVAL}s vía API pública"
    )

def process(data):
    counter = data['counter']
    drops   = data['drops']
    now     = time.time()

    if not state['initialized']:
        initialize(data)

    # Actualizar probabilidades por si cambian
    if data['group_probs']:
        state['group_probs'] = data['group_probs']

    # Detectar drops nuevos por id
    new_drops = [d for d in drops if d['drop_id'] not in state['seen_drop_ids']]
    new_rares = [d for d in new_drops if d['key'] is not None]
    for d in drops:
        state['seen_drop_ids'].add(d['drop_id'])
    # Limitar tamaño del set
    if len(state['seen_drop_ids']) > 500:
        state['seen_drop_ids'] = set(d['drop_id'] for d in drops)

    if new_rares:
        log.info(f"Nuevos raros: {[d['name'] for d in new_rares]}")
    for d in new_rares:
        state['group_last_seen'][d['key']] = counter
        state['drop_history'].append(d)
    if new_rares:
        state['last_any_rare'] = counter

    # Mantener histórico ordenado y acotado (últimos 7 días / 200 entradas)
    state['drop_history'].sort(key=lambda x: x.get('created_at', ''))
    if len(state['drop_history']) > 200:
        state['drop_history'] = state['drop_history'][-200:]

    # Presión individual
    hot_items = []
    for g in RARE_GROUPS:
        p = state['group_probs'].get(g['key'], 0)
        last_seen = state['group_last_seen'].get(g['key'], state['base_counter'])
        n = max(0, counter - last_seen)
        hot_items.append({**g, 'prob': p, 'n': n, 'pct': prob_acum(p, n)})

    # Presión combinada
    total_p = sum(state['group_probs'].values())
    boxes_since_any = max(0, counter - (state['last_any_rare'] or state['base_counter']))
    combined_pct = combined_prob(total_p, boxes_since_any)
    expected = round(1 / total_p) if total_p > 0 else 0

    # Histórico
    hist_stats, timeline = compute_history(datetime.utcnow())

    # Dashboard
    dashboard.update({
        'counter': counter,
        'updated': datetime.utcnow().isoformat() + 'Z',
        'combined_pct': round(combined_pct, 2),
        'boxes_since_any': boxes_since_any,
        'expected_every': expected,
        'hot_items': [{'key': i['key'], 'name': i['name'], 'n': i['n'],
                       'pct': round(i['pct'], 2), 'prob': round(i['prob']*100, 4)} for i in hot_items],
        'last_drops': [{'name': d['name'], 'price': f"${d['price']:.2f}",
                        'created_at': d['created_at'], 'key': d['key']} for d in drops[:15]],
        'history_24h': timeline,
        'history_stats': hist_stats,
        'status': 'activo',
    })

    items_summary = ', '.join(i['key'] + ':' + str(round(i['pct'],1)) + '%' for i in hot_items)
    log.info(f"Counter: {counter} | Combined: {combined_pct:.1f}% ({boxes_since_any}) | {items_summary}")

    if not state['initialized']:
        return

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
                    f"Counter global: <code>{counter:,}</code>\n\n"
                    f"⚡ <i>Estadísticamente ya debería haber caído</i>"
                )

    # Alerta sequía global
    if combined_pct >= THRESHOLD_COMB and now - state['last_alert_comb'] > ALERT_COOLDOWN:
        state['last_alert_comb'] = now
        bars = ''.join(
            f"{'🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'} {i['name']}: {i['pct']:.1f}%\n"
            for i in hot_items
        )
        send_telegram(
            f"⚠️ <b>SEQUÍA GLOBAL — {combined_pct:.1f}%</b>\n\n"
            f"<code>{boxes_since_any:,}</code> cajas sin ningún drop raro\n"
            f"(esperado cada ~{expected:,})\n\n{bars}\n"
            f"Counter: <code>{counter:,}</code>"
        )

    # Drops nuevos informativos
    for d in new_rares:
        g = next((x for x in RARE_GROUPS if x['key'] == d['key']), None)
        if g:
            send_telegram(
                f"✅ <b>DROP DETECTADO</b> — {d['name']}\n"
                f"Precio: ${d['price']:.2f}\n"
                f"Counter: <code>{counter:,}</code>\n"
                f"<i>Contador de {g['name']} reseteado</i>"
            )

# ── PANEL WEB ─────────────────────────────────────────────────────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer

DASHBOARD_HTML = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CS2 Monitor</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0c10;color:#c8d8e8;font-family:'Rajdhani',sans-serif;padding:16px;max-width:760px;margin:0 auto}
h1{font-family:'Share Tech Mono',monospace;font-size:14px;color:#4af3ff;letter-spacing:2px;margin-bottom:4px}
.sub{font-size:11px;color:#4a6070;font-family:'Share Tech Mono',monospace;margin-bottom:16px}
.combined{background:#0f1318;border:1px solid #1e2a3a;border-radius:6px;padding:14px;margin-bottom:16px}
.combined.warm{border-color:#ff6b35}.combined.hot{border-color:#ff3b5c;box-shadow:0 0 16px rgba(255,59,92,.4)}
.combined-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.combined-lbl{font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:2px;color:#4a6070;text-transform:uppercase}
.big{font-family:'Share Tech Mono',monospace;font-size:30px;font-weight:700;color:#4af3ff}
.big.warm{color:#ff6b35}.big.hot{color:#ff3b5c}
.track{height:8px;background:#161b24;border-radius:4px;overflow:hidden;margin-bottom:6px}
.fill{height:100%;border-radius:4px;background:linear-gradient(90deg,#1e4060,#4af3ff);transition:width .6s}
.fill.warm{background:linear-gradient(90deg,#4a2010,#ff6b35)}.fill.hot{background:linear-gradient(90deg,#6a0000,#ff3b5c)}
.csub{display:flex;justify-content:space-between;font-size:11px;color:#4a6070;font-family:'Share Tech Mono',monospace}
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:16px}
.stat{background:#0f1318;border:1px solid #1e2a3a;border-radius:4px;padding:10px;text-align:center}
.stat-v{font-family:'Share Tech Mono',monospace;font-size:18px;color:#4af3ff}
.stat-l{font-size:9px;color:#4a6070;text-transform:uppercase;letter-spacing:1px;margin-top:2px}
.section-t{font-family:'Share Tech Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;margin:16px 0 8px}
.item{margin-bottom:10px}
.item-h{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:3px}
.item-n{font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px}
.item-p{font-family:'Share Tech Mono',monospace;font-size:12px;color:#4a6070}
.item-p.warm{color:#ff6b35}.item-p.hot{color:#ff3b5c}
.itrack{height:5px;background:#161b24;border-radius:3px;overflow:hidden}
.ifill{height:100%;border-radius:3px;background:linear-gradient(90deg,#1e4060,#4af3ff)}
.ifill.warm{background:linear-gradient(90deg,#4a2010,#ff6b35)}.ifill.hot{background:linear-gradient(90deg,#6a0000,#ff3b5c)}
.isub{font-size:10px;color:#4a6070;margin-top:2px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.covert{background:#eb4b4b}.classified{background:#8847ff}.milspec{background:#5e98d9}.consumer{background:#b0b0b0}
.drop{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid rgba(30,42,58,.4)}
.drop-i{flex:1;min-width:0}.drop-n{font-size:12px;font-weight:600}.drop-w{font-size:10px;color:#4a6070}
.drop-pr{font-family:'Share Tech Mono',monospace;font-size:12px;color:#f0a500}
</style></head><body>
<h1>CS2 CASE MONITOR</h1><div class="sub" id="sub">cargando...</div>
<div class="combined" id="cCard">
  <div class="combined-top"><span class="combined-lbl">// Sequía global — cualquier raro</span><span class="big" id="cPct">—</span></div>
  <div class="track"><div class="fill" id="cFill" style="width:0%"></div></div>
  <div class="csub"><span id="cBoxes">—</span><span id="cExp">—</span></div>
</div>
<div class="stats">
  <div class="stat"><div class="stat-v" id="sCounter">—</div><div class="stat-l">Total cajas</div></div>
  <div class="stat"><div class="stat-v" id="sInterval">—</div><div class="stat-l">Check cada</div></div>
  <div class="stat"><div class="stat-v" id="sStatus">—</div><div class="stat-l">Estado</div></div>
</div>
<div class="section-t">Histórico de drops raros</div>
<div class="stats">
  <div class="stat"><div class="stat-v" id="h24">—</div><div class="stat-l">Raros 24h</div></div>
  <div class="stat"><div class="stat-v" id="hGap">—</div><div class="stat-l">Cada (h media)</div></div>
  <div class="stat"><div class="stat-v" id="hLast">—</div><div class="stat-l">Hace (h últim.)</div></div>
</div>
<div id="hByItem" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px"></div>
<div id="timeline"></div>
<div class="section-t">Presión individual por item</div><div id="items"></div>
<script>
const RAR={butterfly:'covert',spec_gloves:'covert',awp:'covert',sport_gloves:'covert',ak47:'classified'};
function zone(p){return p>=90?'hot':p>=70?'warm':''}
function fmtDrop(name){const parts=name.split('|');return parts.length>1?`${parts[0].trim()} ${parts[1].trim()}`:name}
async function load(){
  try{
    const r=await fetch('/data');const d=await r.json();
    const cls=zone(d.combined_pct);
    document.getElementById('cCard').className='combined '+cls;
    document.getElementById('cPct').className='big '+cls;
    document.getElementById('cPct').textContent=d.combined_pct.toFixed(1)+'%';
    document.getElementById('cFill').className='fill '+cls;
    document.getElementById('cFill').style.width=Math.min(d.combined_pct,100)+'%';
    document.getElementById('cBoxes').textContent=(d.boxes_since_any||0).toLocaleString('es-ES')+' cajas sin raro';
    document.getElementById('cExp').textContent='cae cada ~'+(d.expected_every||0).toLocaleString('es-ES');
    document.getElementById('sCounter').textContent=d.counter?d.counter.toLocaleString('es-ES'):'—';
    document.getElementById('sInterval').textContent=(d.check_interval||0)+'s';
    document.getElementById('sStatus').textContent=d.status||'—';
    const upd=d.updated?new Date(d.updated):null;
    document.getElementById('sub').textContent=upd?('Última actualización: '+upd.toLocaleTimeString('es-ES')):'sin datos';
    document.getElementById('items').innerHTML=(d.hot_items||[]).map(i=>{
      const c=zone(i.pct);return `<div class="item"><div class="item-h"><span class="item-n"><span class="dot ${RAR[i.key]||'milspec'}"></span>${i.name} <span style="color:#4a6070;font-size:10px">${i.prob}%</span></span><span class="item-p ${c}">${i.pct.toFixed(1)}%</span></div><div class="itrack"><div class="ifill ${c}" style="width:${Math.min(i.pct,100)}%"></div></div><div class="isub">${(i.n||0).toLocaleString('es-ES')} cajas desde último drop</div></div>`;
    }).join('');
    // Histórico
    const hs=d.history_stats||{};
    document.getElementById('h24').textContent=hs.count_24h!=null?hs.count_24h:'—';
    document.getElementById('hGap').textContent=hs.avg_gap_h!=null?hs.avg_gap_h+'h':'—';
    document.getElementById('hLast').textContent=hs.hours_since_last!=null?hs.hours_since_last+'h':'—';
    const NAMES={butterfly:'Butterfly',spec_gloves:'Spec Gloves',awp:'AWP',sport_gloves:'Sport Gloves',ak47:'AK Vulcan'};
    const byItem=hs.by_item_24h||{};
    document.getElementById('hByItem').innerHTML=Object.keys(NAMES).map(k=>{
      const n=byItem[k]||0;const col=n>0?'#4af3ff':'#4a6070';
      return `<span style="font-family:'Share Tech Mono',monospace;font-size:10px;padding:3px 8px;border:1px solid #1e2a3a;border-radius:3px;color:${col}"><span class="dot ${RAR[k]}"></span> ${NAMES[k]}: ${n}</span>`;
    }).join('');
    document.getElementById('timeline').innerHTML=(d.history_24h||[]).slice(0,30).map(x=>`<div class="drop"><span class="dot ${RAR[x.key]||'milspec'}"></span><div class="drop-i"><div class="drop-n">${fmtDrop(x.name)}</div><div class="drop-w">${x.created_at||''}</div></div><div class="drop-pr">${x.price}</div></div>`).join('')||'<div style="font-size:11px;color:#4a6070;font-family:monospace">acumulando histórico...</div>';
  }catch(e){document.getElementById('sub').textContent='error cargando'}
}
load();setInterval(load,5000);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == '/data':
            body = json.dumps(dashboard).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        else:
            body = DASHBOARD_HTML.encode()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)

def start_web():
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()

# ── LOOP ──────────────────────────────────────────────────────────────────────
def main():
    log.info("CS2 Monitor (API) arrancando...")
    threading.Thread(target=start_web, daemon=True).start()
    send_telegram("⏳ <b>CS2 Monitor desplegado</b> — conectando a la API de skin.club...")
    fails = 0
    while True:
        data = fetch_data()
        if data and data.get('counter'):
            fails = 0
            process(data)
        else:
            fails += 1
            dashboard['status'] = 'sin datos'
            if fails == 3:
                send_telegram("⚠️ <b>Aviso:</b> 3 fallos seguidos al leer la API. ¿Bloqueo de IP? Revisa logs.")
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    main()

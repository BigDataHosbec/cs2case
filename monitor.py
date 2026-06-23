import os, time, logging, json, threading, math
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
try:
    from zoneinfo import ZoneInfo
    MADRID_TZ = ZoneInfo('Europe/Madrid')
except Exception:
    MADRID_TZ = None
import requests

_handlers = [logging.StreamHandler()]
# Log rotativo en el Volume para poder investigar incidencias a posteriori
try:
    _log_dir = os.environ.get('DATA_DIR', '/data')
    os.makedirs(_log_dir, exist_ok=True)
    _fh = RotatingFileHandler(os.path.join(_log_dir, 'monitor.log'),
                              maxBytes=2_000_000, backupCount=3)
    _handlers.append(_fh)
except Exception:
    pass  # si no se puede escribir el log, seguimos solo con stdout
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', handlers=_handlers)
log = logging.getLogger(__name__)

def to_madrid(utc_str):
    """Convierte 'YYYY-MM-DD HH:MM:SS' (UTC) a hora de España peninsular."""
    if not utc_str:
        return ''
    try:
        dt = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        if MADRID_TZ:
            dt = dt.astimezone(MADRID_TZ)
        return dt.strftime('%d/%m %H:%M')
    except Exception:
        return utc_str

# ── CONFIG GLOBAL ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = str(os.environ['TELEGRAM_CHAT_ID'])
CHECK_INTERVAL   = int(os.environ.get('CHECK_INTERVAL', '60'))
ALERT_LEVELS     = [float(x) for x in os.environ.get('ALERT_LEVELS', '90,95').split(',')]
DEAD_AFTER_MIN   = int(os.environ.get('DEAD_AFTER_MIN', '60'))
PORT             = int(os.environ.get('PORT', '8080'))
DATA_DIR         = os.environ.get('DATA_DIR', '/data')
# Umbral de rareza: items con chance <= este % se consideran "raros" y se siguen.
RARE_MAX_CHANCE  = float(os.environ.get('RARE_MAX_CHANCE', '0.01'))
# Factor de racha anómala: avisa si la sequía supera N veces lo esperado.
ANOMALY_FACTOR   = float(os.environ.get('ANOMALY_FACTOR', '3'))

# ── DEFINICIÓN DE CAJAS ───────────────────────────────────────────────────────
# Cada caja: id, nombre visible, y archivo de estado.
# La primera mantiene el state.json original para no perder el estado sembrado.
CASES = [
    {'id': 'cc-bf4940a7e', 'name': 'DONT TRUST',          'state_file': os.path.join(DATA_DIR, 'state.json')},
    {'id': 'cc-08e7b18f7', 'name': '1º NO PAIN 85%',       'state_file': os.path.join(DATA_DIR, 'state_cc-08e7b18f7.json')},
    # En esta caja seguimos SOLO la M4A4 Hellfire (no el resto de raros).
    {'id': 'cc-c81e43fb8', 'name': 'NO PAIN 84% PROFIT',   'state_file': os.path.join(DATA_DIR, 'state_cc-c81e43fb8.json'),
     'only_items': ['m4a4 | hellfire']},
]
# Permite override por env (JSON) para añadir/quitar cajas sin tocar código.
_cases_env = os.environ.get('CASES_JSON')
if _cases_env:
    try:
        CASES = json.loads(_cases_env)
        for c in CASES:
            c.setdefault('state_file', os.path.join(DATA_DIR, f"state_{c['id']}.json"))
    except Exception as e:
        log.error(f"CASES_JSON inválido, usando cajas por defecto: {e}")

def case_web_url(case_id):
    return f'https://skin.club/es/cases/open/{case_id}'

def api_url(case_id):
    return f'https://gate.skin.club/apiv2/cases/{case_id}'

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

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Origin': 'https://skin.club',
    'Referer': 'https://skin.club/',
    'Accept-Language': 'es-ES,es;q=0.9',
}

# ── MONITOR DE UNA CAJA ───────────────────────────────────────────────────────
class CaseMonitor:
    def __init__(self, case_id, name, state_file, only_items=None):
        self.case_id = case_id
        self.name = name
        self.state_file = state_file
        self.api = api_url(case_id)
        self.web = case_web_url(case_id)
        # Si se define, SOLO se siguen los items cuyo nombre contenga uno de estos
        # fragmentos (en minúsculas). Ignora el umbral de probabilidad.
        self.only_items = [s.lower() for s in only_items] if only_items else None
        self.lock = threading.Lock()
        self.state = {
            'base_counter': None, 'group_last_seen': {}, 'last_any_rare': None,
            'seen_drop_ids': set(), 'alerted_levels_ind': {}, 'alerted_levels_comb': [],
            'initialized': False, 'group_probs': {}, 'rare_meta': {}, 'drop_history': [],
            'pressure_snapshots': [], 'last_counter': None,
            'last_counter_change_ts': None, 'dead_alerted': False, 'api_warned': False,
            # ── Análisis de tasa observada vs declarada ──
            'observed_start_counter': None,   # counter cuando empezó la observación fiable
            'observed_drops': {},             # key -> nº de drops observados (detectados en vivo)
            'observed_value': 0.0,            # valor $ total de los drops raros observados
            'intervals': [],                  # cajas entre drops raros consecutivos (cualquiera)
            'last_rare_counter': None,        # counter del último raro (para medir intervalos)
            'missed_gaps': 0,                 # nº de veces que un reinicio pudo perder drops
            'rate_limited_until': None,       # epoch hasta el que estamos rate-limited
            'rate_warned': False,
            'last_ok_check_ts': None,         # último check exitoso (para health)
            'total_checks': 0, 'total_fails': 0,
            'anomaly_alerted': False,         # si ya avisamos de racha anómala
        }
        self.dashboard = {
            'id': case_id, 'name': name, 'counter': None, 'updated': None,
            'combined_pct': 0, 'boxes_since_any': 0, 'hot_items': [], 'expected_every': 0,
            'check_interval': CHECK_INTERVAL, 'status': 'arrancando', 'history_24h': [],
            'history_stats': {}, 'pressure_series': [], 'kpis': {}, 'case_url': self.web,
            'dead': False, 'analysis': {},
        }
        self.control = {'force_check': False}

    # ── identificación de raros (automática por probabilidad) ──
    def rare_key(self, name):
        """Agrupa por nombre base del item (sin desgaste) si es raro."""
        meta = self.state.get('rare_meta', {})
        base = self._base_name(name)
        return base if base in meta else None

    @staticmethod
    def _base_name(market_hash_name):
        # Quita el desgaste entre paréntesis y el prefijo StatTrak/★ para agrupar variantes
        n = market_hash_name
        if '(' in n:
            n = n[:n.rfind('(')].strip()
        n = n.replace('StatTrak™', '').replace('★', '').strip()
        return n

    @staticmethod
    def _short_name(base):
        # Versión corta para chips/resúmenes
        parts = base.split('|')
        if len(parts) == 2:
            return parts[1].strip()[:16]
        return base[:16]

    def group_name(self, key):
        meta = self.state.get('rare_meta', {})
        return meta.get(key, {}).get('name', key)

    # ── persistencia con backup ──
    @staticmethod
    def _sane(data):
        if not isinstance(data, dict): return False
        if 'base_counter' not in data: return False
        bc = data.get('base_counter')
        if bc is not None and (not isinstance(bc, int) or bc < 0): return False
        if not isinstance(data.get('group_last_seen', {}), dict): return False
        if not isinstance(data.get('drop_history', []), list): return False
        return True

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            s = dict(self.state)
            s['seen_drop_ids'] = list(self.state['seen_drop_ids'])
            if s.get('base_counter') is None:
                return
            if os.path.exists(self.state_file):
                try:
                    os.replace(self.state_file, self.state_file + '.bak')
                except Exception as e:
                    log.error(f"[{self.case_id}] backup falló: {e}")
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(s, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            log.error(f"[{self.case_id}] no se pudo guardar: {e}")

    def _load_file(self, path):
        with open(path) as f:
            data = json.load(f)
        if not self._sane(data):
            raise ValueError("estado no válido")
        return data

    # Mapa de claves viejas (código mono-caja) → nombre base nuevo, solo caja original
    LEGACY_KEY_MAP = {
        'butterfly':    'Butterfly Knife | Case Hardened',
        'spec_gloves':  'Specialist Gloves | Blackbook',
        'awp':          "AWP | Queen's Gambit",
        'sport_gloves': 'Sport Gloves | Frosty',
        'ak47':         'AK-47 | Vulcan',
    }

    def _migrate_legacy_keys(self):
        """Si el estado usa las claves viejas (ak47, butterfly...), las traduce a
        los nombres base nuevos para no perder el conteo al cambiar de formato."""
        gls = self.state.get('group_last_seen', {})
        if not gls:
            return
        # ¿Tiene claves viejas?
        if not any(k in self.LEGACY_KEY_MAP for k in gls):
            return
        new_gls, new_probs, new_alerts = {}, {}, {}
        old_probs = self.state.get('group_probs', {})
        old_alerts = self.state.get('alerted_levels_ind', {})
        for old_k, new_k in self.LEGACY_KEY_MAP.items():
            if old_k in gls:
                new_gls[new_k] = gls[old_k]
                if old_k in old_probs:
                    new_probs[new_k] = old_probs[old_k]
                if old_k in old_alerts:
                    new_alerts[new_k] = old_alerts[old_k]
        # conservar cualquier clave que ya estuviera en formato nuevo
        for k, v in gls.items():
            if k not in self.LEGACY_KEY_MAP:
                new_gls[k] = v
        self.state['group_last_seen'] = new_gls
        if new_probs:
            self.state['group_probs'] = new_probs
        self.state['alerted_levels_ind'] = new_alerts
        # traducir las claves en el histórico de drops
        for d in self.state.get('drop_history', []):
            if d.get('key') in self.LEGACY_KEY_MAP:
                d['key'] = self.LEGACY_KEY_MAP[d['key']]
        log.info(f"[{self.case_id}] migradas claves viejas → nombres base")

    def load(self):
        for path, label in ((self.state_file, 'principal'), (self.state_file + '.bak', 'backup')):
            try:
                if not os.path.exists(path):
                    continue
                data = self._load_file(path)
                data['seen_drop_ids'] = set(data.get('seen_drop_ids', []))
                for k in self.state.keys():
                    if k in data:
                        self.state[k] = data[k]
                self._migrate_legacy_keys()
                # Si el estado viejo no tenía campos de análisis, inicializarlos ahora
                # (la observación fiable empieza desde este momento)
                if self.state.get('observed_start_counter') is None and self.state.get('last_counter'):
                    self.state['observed_start_counter'] = self.state['last_counter']
                    self.state['last_rare_counter'] = self.state.get('last_any_rare') or self.state['last_counter']
                    if not self.state.get('observed_drops'):
                        self.state['observed_drops'] = {}
                    self.state.setdefault('observed_value', 0.0)
                    self.state.setdefault('intervals', [])
                    log.info(f"[{self.case_id}] análisis inicializado desde estado existente")
                log.info(f"[{self.case_id}] estado recuperado ({label}) — base: {self.state.get('base_counter')}")
                if label == 'backup':
                    send_telegram(f"⚠️ <b>{self.name}: estado principal corrupto</b> — recuperado desde backup.")
                    self.save()
                return True
            except Exception as e:
                log.error(f"[{self.case_id}] fallo cargando {label}: {e}")
                continue
        log.info(f"[{self.case_id}] sin estado previo válido — arranque limpio")
        return False

    def apply_seed_if_present(self):
        seed_path = os.path.join(os.path.dirname(self.state_file),
                                 f'seed_{self.case_id}.json')
        # compat: la primera caja puede usar el seed.json clásico
        legacy = os.path.join(os.path.dirname(self.state_file), 'seed.json')
        if not os.path.exists(seed_path) and self.state_file.endswith('state.json') and os.path.exists(legacy):
            seed_path = legacy
        try:
            if not os.path.exists(seed_path):
                return False
            with open(seed_path) as f:
                data = json.load(f)
            data['seen_drop_ids'] = set(data.get('seen_drop_ids', []))
            for k in self.state.keys():
                if k in data:
                    self.state[k] = data[k]
            self.state['initialized'] = True
            self.save()
            os.replace(seed_path, seed_path + '.applied')
            log.info(f"[{self.case_id}] SEED aplicado — base: {self.state.get('base_counter')}")
            return True
        except Exception as e:
            log.error(f"[{self.case_id}] no se pudo aplicar seed: {e}")
            return False

    # ── fetch con validación de la API ──
    def warn_api(self, detail):
        log.error(f"[{self.case_id}] posible cambio API: {detail}")
        if not self.state.get('api_warned'):
            self.state['api_warned'] = True
            send_telegram(f"⚠️ <b>{self.name}: posible cambio en la API</b>\nDetalle: {detail}\n"
                          f"<i>El monitor sigue intentándolo.</i>")

    def fetch(self):
        try:
            r = requests.get(self.api, headers=HEADERS, timeout=20)
            # Manejo de rate-limiting: si la API nos limita (429), respetamos el backoff
            if r.status_code == 429:
                retry_after = r.headers.get('Retry-After')
                wait = int(retry_after) if (retry_after and retry_after.isdigit()) else 120
                self.state['rate_limited_until'] = time.time() + wait
                log.error(f"[{self.case_id}] rate-limited (429), esperando {wait}s")
                if not self.state.get('rate_warned'):
                    self.state['rate_warned'] = True
                    send_telegram(f"⏳ <b>{self.name}: la API limitó peticiones (429)</b>\n"
                                  f"Espaciando checks {wait}s automáticamente.")
                return None
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            log.error(f"[{self.case_id}] fetch HTTP error: {e}")
            return None
        except Exception as e:
            log.error(f"[{self.case_id}] fetch error (red): {e}")
            return None
        # si veníamos rate-limited y ahora funciona, limpiar aviso
        if self.state.get('rate_warned'):
            self.state['rate_warned'] = False
            self.state['rate_limited_until'] = None
        try:
            payload = r.json()
        except Exception as e:
            self.warn_api("respuesta no es JSON")
            return None
        d = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(d, dict):
            self.warn_api("falta 'data'"); return None
        stats = d.get('stats')
        if not isinstance(stats, dict) or 'opening_count' not in stats:
            self.warn_api("falta 'stats.opening_count'"); return None
        counter = stats.get('opening_count')
        if not isinstance(counter, int) or counter <= 0:
            self.warn_api(f"opening_count inválido ({counter!r})"); return None

        try:
            contents = (d.get('last_successful_generation') or {}).get('contents', [])
            # Detectar raros. Si hay only_items, solo esos; si no, por umbral de prob.
            rare_meta = {}   # base_name -> {name, short, prob}
            for c in contents:
                try:
                    nm = c['item']['market_hash_name']
                    chance = float(c['chance_percent'])
                except (KeyError, TypeError, ValueError):
                    continue
                if self.only_items is not None:
                    is_rare = any(frag in nm.lower() for frag in self.only_items)
                else:
                    is_rare = chance <= RARE_MAX_CHANCE
                if is_rare:
                    base = self._base_name(nm)
                    p = chance / 100.0
                    if base in rare_meta:
                        rare_meta[base]['prob'] += p
                    else:
                        rare_meta[base] = {'name': base, 'short': self._short_name(base), 'prob': p}

            if contents and not rare_meta:
                self.warn_api("no se detecta ningún item raro")

            group_probs = {k: v['prob'] for k, v in rare_meta.items()}

            drops = []
            for t in d.get('top_drops', []):
                try:
                    item = (t.get('reason') or {}).get('item') or t.get('item') or {}
                    nm = item.get('market_hash_name', '')
                    if not nm:
                        continue
                    base = self._base_name(nm)
                    drops.append({
                        'name': nm, 'price': t['price'] / 100.0,
                        'created_at': t.get('created_at', ''), 'drop_id': t.get('id'),
                        'key': base if base in rare_meta else None,
                    })
                except (KeyError, TypeError):
                    continue

            if self.state.get('api_warned'):
                self.state['api_warned'] = False
                send_telegram(f"✅ <b>{self.name}: API normalizada</b>.")

            return {'counter': counter, 'group_probs': group_probs,
                    'rare_meta': rare_meta, 'drops': drops}
        except Exception as e:
            self.warn_api(f"error procesando: {e}")
            return None

    def initialize(self, data):
        counter = data['counter']
        self.state['base_counter'] = counter
        self.state['last_any_rare'] = counter
        self.state['group_probs'] = data['group_probs']
        self.state['rare_meta'] = data['rare_meta']
        for key in data['rare_meta']:
            self.state['group_last_seen'][key] = counter
        self.state['seen_drop_ids'] = set(d['drop_id'] for d in data['drops'])
        self.state['last_counter'] = counter
        self.state['last_counter_change_ts'] = time.time()
        self.state['initialized'] = True
        # Inicio de la observación fiable (denominador para tasa observada vs declarada)
        self.state['observed_start_counter'] = counter
        self.state['observed_drops'] = {key: 0 for key in data['rare_meta']}
        self.state['observed_value'] = 0.0
        self.state['intervals'] = []
        self.state['last_rare_counter'] = counter
        seed = [d for d in data['drops'] if d['key'] is not None]
        seed.sort(key=lambda x: x.get('created_at', ''))
        self.state['drop_history'] = seed
        total_p = sum(data['group_probs'].values())
        expected = round(1 / total_p) if total_p > 0 else 0
        self.dashboard['expected_every'] = expected
        log.info(f"[{self.case_id}] inicializado — counter: {counter}, raros: {len(data['rare_meta'])}, prob: {total_p*100:.4f}%")
        lines = '\n'.join(f"• {m['name']}: {m['prob']*100:.3f}%" for m in data['rare_meta'].values())
        send_telegram(
            f"🟢 <b>{self.name} — monitor iniciado</b>\n"
            f"Counter base: <code>{counter:,}</code>\n"
            f"Raro esperado cada ~{expected:,} cajas\n\n"
            f"<b>Items raros detectados:</b>\n{lines}"
        )

    def compute_history(self, now_dt):
        hist = self.state['drop_history']
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
        intervals = [(parsed[i][0]-parsed[i-1][0]).total_seconds()/3600 for i in range(1, len(parsed))]
        avg_gap_h = round(sum(intervals)/len(intervals), 1) if intervals else None
        hours_since_last = round((now_dt.timestamp()-parsed[-1][0].timestamp())/3600, 1) if parsed else None
        span_h = round((parsed[-1][0].timestamp()-parsed[0][0].timestamp())/3600, 1) if len(parsed) >= 2 else None
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

    def process(self, data):
        counter = data['counter']
        drops = data['drops']
        now = time.time()
        now_dt = datetime.utcnow()
        st = self.state

        if not st['initialized']:
            self.initialize(data)

        # guardia anti-retroceso
        if st.get('last_counter') is not None and counter < st['last_counter']:
            drop_amount = st['last_counter'] - counter
            log.error(f"[{self.case_id}] counter retrocedió -{drop_amount}, ignorando ciclo")
            if drop_amount > 100 and not st.get('api_warned'):
                self.warn_api(f"el contador retrocedió {drop_amount:,} cajas")
            return

        # actualizar probabilidades y meta de raros
        if data['group_probs']:
            st['group_probs'] = data['group_probs']
        if data.get('rare_meta'):
            st['rare_meta'] = data['rare_meta']

        # caja muerta
        if st['last_counter'] is None:
            st['last_counter'] = counter; st['last_counter_change_ts'] = now
        elif counter > st['last_counter']:
            st['last_counter'] = counter; st['last_counter_change_ts'] = now
            st['dead_alerted'] = False
        else:
            stalled_min = (now - (st['last_counter_change_ts'] or now)) / 60
            if stalled_min >= DEAD_AFTER_MIN and not st['dead_alerted']:
                st['dead_alerted'] = True
                send_telegram(f"💀 <b>{self.name}: POSIBLE CAJA MUERTA</b>\n"
                              f"El contador no sube desde hace {int(stalled_min)} min.\n"
                              f"Counter: <code>{counter:,}</code>")
        dead_now = st['dead_alerted']

        # drops nuevos
        new_drops = [d for d in drops if d['drop_id'] not in st['seen_drop_ids']]
        new_rares = [d for d in new_drops if d['key'] is not None]
        for d in drops:
            st['seen_drop_ids'].add(d['drop_id'])
        if len(st['seen_drop_ids']) > 500:
            st['seen_drop_ids'] = set(d['drop_id'] for d in drops)

        if new_rares:
            log.info(f"[{self.case_id}] nuevos raros: {[d['name'] for d in new_rares]}")

        # presión en el momento del drop
        total_p_now = sum(st['group_probs'].values())
        for d in new_rares:
            key = d['key']
            p_item = st['group_probs'].get(key, 0)
            last_seen_item = st['group_last_seen'].get(key, st['base_counter'])
            n_item = max(0, counter - last_seen_item)
            last_any = st['last_any_rare'] or st['base_counter']
            n_any = max(0, counter - last_any)
            d['pressure_individual'] = round(prob_acum(p_item, n_item), 1)
            d['pressure_combined'] = round(combined_prob(total_p_now, n_any), 1)
            d['boxes_individual'] = n_item
            d['boxes_combined'] = n_any

        for d in new_rares:
            key = d['key']
            st['group_last_seen'][key] = counter
            st['drop_history'].append(d)
            # ── Registro para análisis de tasa observada ──
            st['observed_drops'][key] = st['observed_drops'].get(key, 0) + 1
            st['observed_value'] = st.get('observed_value', 0.0) + d.get('price', 0.0)
            # intervalo en cajas desde el último raro (cualquiera)
            if st.get('last_rare_counter') is not None:
                gap = counter - st['last_rare_counter']
                if gap > 0:
                    st.setdefault('intervals', []).append(gap)
                    if len(st['intervals']) > 500:
                        st['intervals'] = st['intervals'][-500:]
            st['last_rare_counter'] = counter
        if new_rares:
            st['last_any_rare'] = counter
        st['drop_history'].sort(key=lambda x: x.get('created_at', ''))
        if len(st['drop_history']) > 300:
            st['drop_history'] = st['drop_history'][-300:]

        # presiones por item raro
        hot_items = []
        for key, meta in st['rare_meta'].items():
            p = st['group_probs'].get(key, 0)
            last_seen = st['group_last_seen'].get(key, st['base_counter'])
            n = max(0, counter - last_seen)
            hot_items.append({'key': key, 'name': meta['name'], 'short': meta['short'],
                              'prob': p, 'n': n, 'pct': prob_acum(p, n)})
        hot_items.sort(key=lambda x: -x['pct'])

        total_p = sum(st['group_probs'].values())
        boxes_since_any = max(0, counter - (st['last_any_rare'] or st['base_counter']))
        combined_pct = combined_prob(total_p, boxes_since_any)
        expected = round(1 / total_p) if total_p > 0 else 0

        # snapshot gráfica (1 cada 30 min, ventana 14 días)
        SNAP_MIN, SNAP_DAYS = 30, 14
        snaps = st['pressure_snapshots']
        now_iso = now_dt.replace(tzinfo=timezone.utc).isoformat()
        add = True
        if snaps:
            try:
                last_t = datetime.fromisoformat(snaps[-1]['t'].replace('Z', '+00:00'))
                if (now_dt.replace(tzinfo=timezone.utc) - last_t).total_seconds() < SNAP_MIN*60:
                    add = False
            except Exception:
                add = True
        if add:
            snaps.append({'t': now_iso, 'counter': counter, 'combined_pct': round(combined_pct, 2)})
            cutoff = now_dt.replace(tzinfo=timezone.utc).timestamp() - SNAP_DAYS*86400
            kept = []
            for s in snaps:
                try:
                    if datetime.fromisoformat(s['t'].replace('Z', '+00:00')).timestamp() >= cutoff:
                        kept.append(s)
                except Exception:
                    kept.append(s)
            st['pressure_snapshots'] = kept[-1500:]

        hist_stats, timeline = self.compute_history(now_dt)
        analysis = self.compute_analysis(counter, hot_items, total_p, expected, boxes_since_any)
        self.dashboard.update({
            'counter': counter, 'updated': now_dt.isoformat() + 'Z',
            'combined_pct': round(combined_pct, 2), 'boxes_since_any': boxes_since_any,
            'expected_every': expected,
            'hot_items': [{'key': i['key'], 'name': i['name'], 'short': i['short'],
                           'n': i['n'], 'pct': round(i['pct'], 2),
                           'prob': round(i['prob']*100, 4),
                           'expected_every': round(1/i['prob']) if i['prob'] > 0 else 0} for i in hot_items],
            'history_24h': timeline, 'history_stats': hist_stats,
            'pressure_series': st['pressure_snapshots'],
            'kpis': {'total_prob_pct': round(total_p*100, 4)},
            'analysis': analysis,
            'status': 'activo' if not dead_now else 'caja muerta', 'dead': dead_now,
        })

        if st['initialized']:
            self._alerts(hot_items, combined_pct, boxes_since_any, expected, counter, new_rares)
        self.save()

    def compute_analysis(self, counter, hot_items, total_p, expected, boxes_since_any=0):
        """Tasa observada vs declarada, honestidad, valor real, intervalos, anomalía.
        Solo es fiable cuando se han observado suficientes cajas/drops."""
        st = self.state
        start = st.get('observed_start_counter')
        if start is None:
            return {}
        boxes_observed = max(0, counter - start)
        obs_drops = st.get('observed_drops', {})
        total_obs = sum(obs_drops.values())

        # Factor de anomalía: cuántas veces el nº esperado de cajas lleva la sequía actual.
        # 1.0 = justo lo esperado · 2.0 = el doble de seco · 3+ = racha muy anómala.
        anomaly = round(boxes_since_any / expected, 2) if expected > 0 else None

        # Tasa combinada observada vs declarada
        declared_rate = total_p  # prob por caja de cualquier raro
        observed_rate = (total_obs / boxes_observed) if boxes_observed > 0 else 0
        # Ratio honestidad: observado/declarado (1.0 = exacto, <1 paga menos, >1 más)
        honesty = (observed_rate / declared_rate) if declared_rate > 0 else None

        # Fiabilidad estadística: cuántos drops esperaríamos con las cajas observadas
        expected_drops = boxes_observed * declared_rate
        # margen de error aproximado (Poisson): ±1.96·sqrt(esperados)/esperados
        if expected_drops >= 1:
            rel_margin = 1.96 * math.sqrt(expected_drops) / expected_drops
        else:
            rel_margin = None

        # Por item: observado vs esperado
        per_item = []
        for it in hot_items:
            key = it['key']
            od = obs_drops.get(key, 0)
            exp_d = boxes_observed * it['prob']
            per_item.append({
                'key': key, 'short': it['short'],
                'observed': od,
                'expected': round(exp_d, 2),
                'declared_prob': round(it['prob']*100, 4),
                'observed_prob': round(od / boxes_observed * 100, 4) if boxes_observed > 0 else None,
            })

        # Distribución de intervalos
        intervals = st.get('intervals', [])
        interval_stats = None
        if intervals:
            srt = sorted(intervals)
            n = len(srt)
            median = srt[n//2]
            interval_stats = {
                'count': n, 'min': min(srt), 'max': max(srt),
                'mean': round(sum(srt)/n), 'median': median,
                'declared_mean': expected,  # lo que debería ser de media
            }

        # Confianza estadística: depende de cuántos drops se han OBSERVADO de verdad
        # (con pocos drops reales no se puede concluir nada, aunque se esperaran muchos).
        # Usamos el menor de observados y esperados como tamaño de muestra efectivo.
        sample = min(total_obs, expected_drops)
        if sample < 5:
            confidence = 'baja'      # insuficiente para juzgar honestidad
        elif sample < 20:
            confidence = 'media'
        else:
            confidence = 'alta'

        # Margen de error real basado en lo OBSERVADO (Poisson sobre el conteo real)
        if total_obs >= 1:
            obs_margin = 1.96 * math.sqrt(total_obs) / total_obs
        else:
            obs_margin = None

        # Veredicto de honestidad solo si hay confianza suficiente
        verdict = None
        if confidence != 'baja' and honesty is not None:
            if honesty >= 0.85:
                verdict = 'justa'
            elif honesty >= 0.6:
                verdict = 'algo por debajo'
            else:
                verdict = 'paga menos de lo declarado'

        return {
            'boxes_observed': boxes_observed,
            'total_observed': total_obs,
            'expected_drops': round(expected_drops, 1),
            'observed_value': round(st.get('observed_value', 0.0), 2),
            'honesty_ratio': round(honesty, 2) if honesty is not None else None,
            'rel_margin_pct': round(obs_margin*100, 1) if obs_margin is not None else None,
            'confidence': confidence,
            'verdict': verdict,
            'anomaly': anomaly,
            'per_item': per_item,
            'intervals': interval_stats,
        }


    def _alerts(self, hot_items, combined_pct, boxes_since_any, expected, counter, new_rares):
        st = self.state
        for item in hot_items:
            key, pct = item['key'], item['pct']
            fired = st['alerted_levels_ind'].get(key, [])
            if pct < ALERT_LEVELS[0]:
                if fired:
                    st['alerted_levels_ind'][key] = []
                continue
            for lvl in ALERT_LEVELS:
                if pct >= lvl and lvl not in fired:
                    fired.append(lvl)
                    st['alerted_levels_ind'][key] = fired
                    send_telegram(f"🔥 <b>{self.name} · {item['name']} — supera {lvl:.0f}%</b>\n\n"
                                  f"Probabilidad acumulada: <b>{pct:.1f}%</b>\n"
                                  f"Cajas sin caer: <code>{item['n']:,}</code>\n"
                                  f"Counter: <code>{counter:,}</code>")
        fired_c = st['alerted_levels_comb']
        if combined_pct < ALERT_LEVELS[0]:
            if fired_c:
                st['alerted_levels_comb'] = []
        else:
            for lvl in ALERT_LEVELS:
                if combined_pct >= lvl and lvl not in fired_c:
                    fired_c.append(lvl)
                    st['alerted_levels_comb'] = fired_c
                    bars = ''.join(f"{'🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'} {i['short']}: {i['pct']:.1f}%\n" for i in hot_items[:8])
                    send_telegram(f"⚠️ <b>{self.name}: SEQUÍA GLOBAL — supera {lvl:.0f}%</b>\n\n"
                                  f"Presión combinada: <b>{combined_pct:.1f}%</b>\n"
                                  f"<code>{boxes_since_any:,}</code> cajas sin ningún raro\n"
                                  f"(esperado cada ~{expected:,})\n\n{bars}")
        for d in new_rares:
            pi, pc = d.get('pressure_individual'), d.get('pressure_combined')
            press = f"Presión al caer — global: <b>{pc}%</b> · item: <b>{pi}%</b>\n" if (pi is not None or pc is not None) else ""
            send_telegram(f"✅ <b>{self.name}: DROP — {d['name']}</b>\n"
                          f"Precio: ${d['price']:.2f} · Counter: <code>{counter:,}</code>\n"
                          f"{press}<i>Contador de {self.group_name(d['key'])} reseteado</i>")

        # Alerta de racha anómala: sequía supera ANOMALY_FACTOR veces lo esperado.
        # Es la señal más interesante de observar (la caja lleva "demasiado" sin pagar).
        if expected > 0:
            anomaly = boxes_since_any / expected
            if anomaly >= ANOMALY_FACTOR and not st.get('anomaly_alerted'):
                st['anomaly_alerted'] = True
                send_telegram(f"📈 <b>{self.name}: RACHA ANÓMALA</b>\n\n"
                              f"Lleva <b>{anomaly:.1f}×</b> lo esperado sin ningún raro.\n"
                              f"<code>{boxes_since_any:,}</code> cajas (esperado cada ~{expected:,}).\n"
                              f"<i>Estadísticamente inusual, aunque cada caja sigue siendo independiente.</i>")
            elif anomaly < ANOMALY_FACTOR and st.get('anomaly_alerted'):
                st['anomaly_alerted'] = False  # rearmar cuando vuelve a la normalidad

    def do_check(self):
        self.state['total_checks'] = self.state.get('total_checks', 0) + 1
        data = self.fetch()
        if data and data.get('counter'):
            with self.lock:
                self.process(data)
            self.state['last_ok_check_ts'] = time.time()
            return True
        self.state['total_fails'] = self.state.get('total_fails', 0) + 1
        return False

    def is_rate_limited(self):
        until = self.state.get('rate_limited_until')
        return until is not None and time.time() < until

    def health(self):
        """Estado de salud de esta caja para el endpoint /health."""
        last_ok = self.state.get('last_ok_check_ts')
        age = (time.time() - last_ok) if last_ok else None
        # sano si tuvo un check OK en los últimos 5 intervalos
        healthy = age is not None and age < CHECK_INTERVAL * 5
        return {
            'id': self.case_id, 'name': self.name,
            'healthy': healthy,
            'seconds_since_ok': round(age) if age is not None else None,
            'total_checks': self.state.get('total_checks', 0),
            'total_fails': self.state.get('total_fails', 0),
            'rate_limited': self.is_rate_limited(),
            'initialized': self.state.get('initialized', False),
            'counter': self.dashboard.get('counter'),
        }

# ── HELPERS GLOBALES ──────────────────────────────────────────────────────────
def parse_dt(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None

# Registro de monitores (uno por caja)
MONITORS = {}   # case_id -> CaseMonitor
def build_monitors():
    for c in CASES:
        MONITORS[c['id']] = CaseMonitor(c['id'], c['name'], c['state_file'],
                                        only_items=c.get('only_items'))

def get_monitor(case_id):
    return MONITORS.get(case_id)

# ── TELEGRAM: comandos con selección de caja ──────────────────────────────────
def cases_keyboard(action):
    # Un botón por caja para un comando dado (estado/historico)
    rows = [[{'text': m.name, 'callback_data': f'{action}:{cid}'}] for cid, m in MONITORS.items()]
    return {'inline_keyboard': rows}

def menu_keyboard():
    rows = []
    for cid, m in MONITORS.items():
        rows.append([
            {'text': f'📊 {m.name}', 'callback_data': f'status:{cid}'},
            {'text': '🌐', 'url': m.web},
        ])
    rows.append([{'text': '🔄 Check todas', 'callback_data': 'checkall'}])
    return {'inline_keyboard': rows}

def status_text(m):
    d = m.dashboard
    if not d.get('counter'):
        return f"⏳ {m.name}: aún sin datos."
    lines = [f"📊 <b>{m.name}</b>",
             f"Counter: <code>{d['counter']:,}</code>",
             f"Sequía global: <b>{d['combined_pct']:.1f}%</b> ({d['boxes_since_any']:,} cajas)", ""]
    for i in d['hot_items'][:8]:
        emoji = '🔴' if i['pct']>=90 else '🟡' if i['pct']>=70 else '⚪'
        lines.append(f"{emoji} {i['short']}: {i['pct']:.1f}% · {i['n']:,}")
    hs = d.get('history_stats', {})
    if hs:
        lines.append("")
        lines.append(f"24h: {hs.get('count_24h',0)} raros · último hace {hs.get('hours_since_last','?')}h")
    return '\n'.join(lines)

def history_text(m):
    d = m.dashboard
    hs = d.get('history_stats', {})
    tl = d.get('history_24h', [])[:12]
    if not tl:
        return f"{m.name}: sin histórico aún."
    lines = [f"📜 <b>{m.name} — histórico</b>",
             f"Total: {hs.get('total_tracked',0)} · 24h: {hs.get('count_24h',0)} · medio {hs.get('avg_gap_h','?')}h", ""]
    for x in tl:
        lines.append(f"• {to_madrid(x['created_at'])} — {x['name']} ({x['price']})")
    return '\n'.join(lines)

def answer_callback(cb_id, text=None):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={'callback_query_id': cb_id, 'text': text or ''}, timeout=10)
    except Exception as e:
        log.error(f"answerCallback error: {e}")

def handle_command(text, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        return
    cmd = text.strip().lower().lstrip('/')
    if cmd in ('start', 'menu', 'help', 'ayuda'):
        send_telegram("🎛 <b>Panel de control</b>\nElige una caja:", menu_keyboard())
    elif cmd in ('estado', 'status'):
        send_telegram("¿De qué caja quieres el estado?", cases_keyboard('status'))
    elif cmd in ('historico', 'histórico', 'history'):
        send_telegram("¿De qué caja quieres el histórico?", cases_keyboard('history'))
    elif cmd in ('check', 'forzar'):
        for m in MONITORS.values():
            m.control['force_check'] = True
        send_telegram("🔄 Check forzado en todas las cajas...")
    else:
        send_telegram("No reconozco ese comando. Pulsa /menu.")

def handle_callback(data, cb_id, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        answer_callback(cb_id); return
    if data == 'checkall':
        for m in MONITORS.values():
            m.control['force_check'] = True
        answer_callback(cb_id, "Check en todas")
        send_telegram("🔄 Check forzado en todas las cajas...")
        return
    if ':' in data:
        action, cid = data.split(':', 1)
        m = get_monitor(cid)
        if not m:
            answer_callback(cb_id); return
        if action == 'status':
            answer_callback(cb_id); send_telegram(status_text(m), menu_keyboard())
        elif action == 'history':
            answer_callback(cb_id); send_telegram(history_text(m))
        else:
            answer_callback(cb_id)
    else:
        answer_callback(cb_id)

def telegram_poll_loop():
    offset = None
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
                    handle_callback(cq.get('data', ''), cq['id'], cq['message']['chat']['id'])
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
.anomaly-row{margin-top:10px;padding-top:10px;border-top:1px solid var(--bd);font-family:'Share Tech Mono',monospace;font-size:11px;display:none}
.anomaly-row.show{display:block}
.anomaly-row .lbl{color:var(--mut2)}
.anomaly-row .val{font-weight:700}
.anomaly-row .val.norm{color:var(--cy)}.anomaly-row .val.warn{color:var(--or)}.anomaly-row .val.hot{color:var(--red)}
.cmb-boxes{font-size:15px;font-weight:600}
.cmb-boxes b{font-family:'Share Tech Mono',monospace;color:var(--cy);font-size:17px}
.cmb-exp{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--mut2)}

/* KPI strip */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.kpis-1{grid-template-columns:1fr}
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

/* home / selector de cajas */
.home-title{font-family:'Share Tech Mono',monospace;font-size:12px;color:var(--mut2);letter-spacing:2px;text-transform:uppercase;margin:8px 0 14px}
.case-card{background:linear-gradient(135deg,var(--bg2),var(--bg3));border:1px solid var(--bd);border-radius:8px;padding:16px;margin-bottom:10px;cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:space-between;gap:12px}
.case-card:hover{border-color:var(--cy);transform:translateY(-1px)}
.case-card:active{transform:translateY(0)}
.case-info{flex:1;min-width:0}
.case-name{font-size:16px;font-weight:700;color:var(--tx);margin-bottom:4px}
.case-meta{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--mut2)}
.case-pct{font-family:'Share Tech Mono',monospace;font-size:22px;font-weight:700;flex-shrink:0}
.case-arrow{color:var(--cy);font-size:18px;flex-shrink:0}
.back-btn{display:inline-flex;align-items:center;gap:6px;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--mut2);cursor:pointer;margin-bottom:12px;padding:6px 0;background:none;border:none;letter-spacing:1px}
.back-btn:hover{color:var(--cy)}
.hidden{display:none!important}

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
.an-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.an-card{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:10px;text-align:center}
.an-v{font-family:'Share Tech Mono',monospace;font-size:18px;color:var(--cy);font-weight:700}
.an-v.good{color:var(--grn)}.an-v.warn{color:var(--or)}.an-v.bad{color:var(--red)}
.an-l{font-size:9px;color:var(--mut2);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.an-sub{font-size:10px;color:var(--mut2);font-family:'Share Tech Mono',monospace;margin-top:3px}
.an-item{display:flex;align-items:center;justify-content:space-between;padding:7px 10px;background:var(--bg2);border:1px solid var(--bd);border-radius:5px;margin-bottom:5px;font-size:12px}
.an-item-n{display:flex;align-items:center;gap:6px;flex:1;min-width:0}
.an-item-v{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--mut2)}
.an-item-v b{color:var(--tx)}
.an-intervals{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--mut2);padding:8px 10px;background:var(--bg2);border:1px solid var(--bd);border-radius:5px;margin-top:6px;line-height:1.6}
.an-note{font-size:10px;color:var(--mut2);font-style:italic;margin-top:6px;line-height:1.4}
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
.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:16px 0}
.btn{padding:11px 8px;border-radius:6px;cursor:pointer;font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;border:1px solid;background:transparent;transition:all .2s;text-align:center;text-decoration:none;display:flex;align-items:center;justify-content:center;gap:5px}
.btn-check{border-color:var(--cy);color:var(--cy)}.btn-check:active{background:rgba(74,243,255,.15)}
.btn-web{border-color:var(--gold);color:var(--gold)}.btn-web:active{background:rgba(240,165,0,.15)}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--bg3);border:1px solid var(--cy);color:var(--cy);font-family:'Share Tech Mono',monospace;font-size:12px;padding:10px 18px;border-radius:6px;transition:transform .3s;z-index:1000}
.toast.show{transform:translateX(-50%) translateY(0)}
.dead-banner{display:none;background:rgba(255,59,92,.12);border:1px solid var(--red);border-radius:6px;padding:10px 14px;margin-bottom:14px;font-family:'Share Tech Mono',monospace;font-size:12px;color:var(--red);letter-spacing:1px}
.dead-banner.show{display:block}
</style></head><body>
<div class="hdr"><h1 id="mainTitle">CS2 CASE MONITOR</h1><div class="live" id="live"><span class="d"></span><span id="liveTxt">EN VIVO</span></div></div>

<!-- ── HOME: selección de caja ── -->
<div id="homeView">
  <div class="home-title">// Selecciona una caja para monitorizar</div>
  <div id="caseList"></div>
</div>

<!-- ── DETALLE de una caja ── -->
<div id="detailView" class="hidden">
<button class="back-btn" id="backBtn">◄ Volver a la lista de cajas</button>
<div class="sub" id="sub">cargando...</div>

<div class="dead-banner" id="deadBanner">💀 CAJA MUERTA — el contador no sube. Posible retirada de la caja.</div>

<div class="combined" id="cCard">
  <div class="cmb-top"><span class="cmb-lbl">Sequía global · cualquier raro</span><span class="cmb-big" id="cPct">—</span></div>
  <div class="track"><div class="fill" id="cFill" style="width:0%"></div></div>
  <div class="cmb-foot"><span class="cmb-boxes"><b id="cBoxes">—</b> cajas sin caer ningún raro</span><span class="cmb-exp" id="cExp"></span></div>
  <div class="anomaly-row" id="anomalyRow"></div>
</div>

<div class="kpis kpis-1">
  <div class="kpi"><div class="kpi-v" id="kCounter">—</div><div class="kpi-l">Total cajas abiertas (control)</div></div>
</div>

<div class="actions actions-2">
  <button class="btn btn-check" id="btnCheck">🔄 Check ya</button>
  <a class="btn btn-web" id="btnWeb" href="#" target="_blank">🌐 Ir a la caja</a>
</div>

<div class="sec">Presión individual por item</div>
<div id="items"></div>

<div class="sec">Análisis · observado vs declarado</div>
<div id="analysisWrap">
  <div class="an-cards">
    <div class="an-card"><div class="an-v" id="anHonesty">—</div><div class="an-l">Índice honestidad</div><div class="an-sub" id="anHonestySub"></div></div>
    <div class="an-card"><div class="an-v" id="anObs">—</div><div class="an-l">Raros observados</div><div class="an-sub" id="anObsSub"></div></div>
    <div class="an-card"><div class="an-v" id="anConf">—</div><div class="an-l">Fiabilidad</div><div class="an-sub" id="anConfSub"></div></div>
  </div>
  <div id="anItems"></div>
  <div id="anIntervals" class="an-intervals"></div>
</div>

<div class="sec">Evolución de la presión combinada · 14 días</div>
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
</div><!-- /detailView -->

<div class="toast" id="toast"></div>

<script>
const RAR={butterfly:'covert',spec_gloves:'covert',awp:'covert',sport_gloves:'covert',ak47:'classified'};
const NAMES={butterfly:'Butterfly',spec_gloves:'Spec Gloves',awp:'AWP QG',sport_gloves:'Sport Gloves',ak47:'AK Vulcan'};
function zone(p){return p>=90?'hot':p>=70?'warm':''}
function fmtDrop(n){const p=n.split('|');return p.length>1?`${p[0].trim()} ${p[1].trim()}`:n}
function nf(n){return (n||0).toLocaleString('es-ES')}
function fmtTime(utcStr){
  // La API da "2026-06-22 04:04:01" en UTC. Convertir a hora de España peninsular.
  if(!utcStr) return '';
  const iso=utcStr.replace(' ','T')+'Z';
  const d=new Date(iso);
  if(isNaN(d)) return utcStr;
  return d.toLocaleString('es-ES',{timeZone:'Europe/Madrid',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}

// ── Navegación Home / Detalle ──
let currentView='home';   // 'home' | 'detail'
let selectedCaseId=null;
function showHome(){
  currentView='home';selectedCaseId=null;
  document.getElementById('homeView').classList.remove('hidden');
  document.getElementById('detailView').classList.add('hidden');
  document.getElementById('mainTitle').textContent='CS2 CASE MONITOR';
  document.getElementById('live').style.visibility='hidden';
}
function showDetail(caseId){
  currentView='detail';selectedCaseId=caseId;
  document.getElementById('homeView').classList.add('hidden');
  document.getElementById('detailView').classList.remove('hidden');
  document.getElementById('live').style.visibility='visible';
  load();
}
function renderHome(payload){
  const cases=payload.cases||[];
  document.getElementById('caseList').innerHTML=cases.map(c=>{
    const pct=(c.combined_pct!=null)?c.combined_pct:null;
    const z=pct!=null?zone(pct):'';
    const col=z==='hot'?'var(--red)':z==='warm'?'var(--or)':'var(--cy)';
    const pctTxt=pct!=null?pct.toFixed(1)+'%':'—';
    const dead=c.dead?' 💀':'';
    const meta=(c.counter)?(nf(c.boxes_since_any||0)+' cajas sin raro · '+nf(c.counter)+' abiertas'+dead):'arrancando...';
    return `<div class="case-card" data-case="${c.id}">
      <div class="case-info"><div class="case-name">${c.name}</div><div class="case-meta">${meta}</div></div>
      <div class="case-pct" style="color:${col}">${pctTxt}</div>
      <div class="case-arrow">►</div>
    </div>`;
  }).join('');
  document.querySelectorAll('.case-card').forEach(el=>{
    el.addEventListener('click',()=>showDetail(el.dataset.case));
  });
}

function renderAnalysis(a){
  const wrap=document.getElementById('analysisWrap');
  if(!a||a.boxes_observed==null||a.boxes_observed<1){
    wrap.style.opacity='0.5';
    document.getElementById('anHonesty').textContent='—';
    document.getElementById('anHonestySub').textContent='sin datos aún';
    document.getElementById('anObs').textContent='0';
    document.getElementById('anObsSub').textContent='';
    document.getElementById('anConf').textContent='—';
    document.getElementById('anConfSub').textContent='';
    document.getElementById('anItems').innerHTML='';
    document.getElementById('anIntervals').innerHTML='<span style="font-style:italic">El análisis se construye observando drops en vivo. Necesita tiempo para ser fiable.</span>';
    return;
  }
  wrap.style.opacity='1';
  // Índice de honestidad — solo se interpreta con confianza suficiente
  const h=a.honesty_ratio;
  const hEl=document.getElementById('anHonesty');
  if(h!=null){
    hEl.textContent=h.toFixed(2)+'×';
    if(a.confidence==='baja'){
      // datos insuficientes: mostrar el número pero en gris, sin veredicto
      hEl.className='an-v';
      document.getElementById('anHonestySub').textContent='datos insuficientes aún';
    }else{
      const cls=h>=0.85?'good':h>=0.6?'warn':'bad';
      hEl.className='an-v '+cls;
      document.getElementById('anHonestySub').textContent=a.verdict||'';
    }
  }else{hEl.textContent='—';document.getElementById('anHonestySub').textContent='';}
  // Observados
  document.getElementById('anObs').textContent=a.total_observed;
  document.getElementById('anObsSub').textContent='esperados: '+a.expected_drops;
  // Fiabilidad
  const conf=a.confidence||'baja';
  const cEl=document.getElementById('anConf');
  cEl.textContent=conf;
  cEl.className='an-v '+(conf==='alta'?'good':conf==='media'?'warn':'bad');
  document.getElementById('anConfSub').textContent=nf(a.boxes_observed)+' cajas obs.';
  // Por item
  document.getElementById('anItems').innerHTML=(a.per_item||[]).map(it=>{
    const obsP=it.observed_prob!=null?it.observed_prob+'%':'—';
    return `<div class="an-item"><span class="an-item-n"><span class="dot ${RAR[it.key]||'milspec'}"></span>${it.short}</span>`+
      `<span class="an-item-v">obs <b>${it.observed}</b> / esp <b>${it.expected}</b> · real <b>${obsP}</b> vs <b>${it.declared_prob}%</b></span></div>`;
  }).join('');
  // Intervalos
  const iv=a.intervals;
  if(iv){
    document.getElementById('anIntervals').innerHTML=
      `Intervalos entre raros (cajas): media observada <b style="color:var(--tx)">${nf(iv.mean)}</b> · mediana ${nf(iv.median)} · `+
      `min ${nf(iv.min)} / max ${nf(iv.max)} · declarada ${nf(iv.declared_mean)}`+
      `<div class="an-note">Si la caja es justa, la media observada debería acercarse a la declarada a medida que se acumulan datos.</div>`;
  }else{
    document.getElementById('anIntervals').innerHTML='<span style="font-style:italic">Aún no hay intervalos entre drops registrados.</span>';
  }
}

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
    const payload=await(await fetch('/data')).json();
    // La Home siempre se mantiene actualizada con todas las cajas
    renderHome(payload);
    // Si estamos en la Home, no hace falta pintar el detalle
    if(currentView!=='detail'){return;}
    // Buscar la caja seleccionada dentro del payload
    const d=(payload.cases||[]).find(c=>c.id===selectedCaseId);
    if(!d){return;}
    document.getElementById('mainTitle').textContent=(d.name||'CS2 CASE MONITOR').toUpperCase();
    const cls=zone(d.combined_pct);
    document.getElementById('cCard').className='combined '+cls;
    document.getElementById('cPct').className='cmb-big '+cls;
    document.getElementById('cPct').textContent=d.combined_pct.toFixed(1)+'%';
    document.getElementById('cFill').className='fill '+cls;
    document.getElementById('cFill').style.width=Math.min(d.combined_pct,100)+'%';
    document.getElementById('cBoxes').textContent=nf(d.boxes_since_any);
    document.getElementById('cExp').textContent='media: 1 raro cada '+nf(d.expected_every)+' cajas';
    document.getElementById('cExp').title='Sale de sumar las probabilidades de todos los raros ('+(d.kpis&&d.kpis.total_prob_pct||'')+'%) e invertir. Es fijo salvo que la caja cambie.';
    // Factor de anomalía (racha)
    const aRow=document.getElementById('anomalyRow');
    const an=d.analysis&&d.analysis.anomaly;
    if(an!=null){
      aRow.classList.add('show');
      const c=an>=3?'hot':an>=2?'warn':'norm';
      const txt=an>=3?'racha muy anómala':an>=2?'más seca de lo normal':'dentro de lo esperado';
      aRow.innerHTML=`<span class="lbl">Racha actual: </span><span class="val ${c}">${an.toFixed(1)}× lo esperado</span> <span class="lbl">· ${txt}</span>`;
    }else{aRow.classList.remove('show');}
    // KPIs
    const k=d.kpis||{};
    const hsk=d.history_stats||{};
    document.getElementById('kCounter').textContent=d.counter?nf(d.counter):'—';
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
    // ── Análisis observado vs declarado ──
    renderAnalysis(d.analysis||{});
    // chart
    drawChart(d.pressure_series||[]);
    const ps=d.pressure_series||[];
    if(ps.length){document.getElementById('chartFrom').textContent=fmtTime((ps[0].t||'').replace('T',' ').replace(/(\+.*|Z)$/,''))}
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
      return `<div class="drop"><span class="dot ${RAR[x.key]||'milspec'}" style="margin-top:3px"></span><div class="drop-i"><div class="drop-n">${fmtDrop(x.name)}</div><div class="drop-t">${fmtTime(x.created_at)}</div>${press}</div><div class="drop-p">${x.price}</div></div>`;
    }).join('')||'<div style="padding:12px;font-size:11px;color:var(--mut2);font-family:monospace">acumulando histórico...</div>';
  }catch(e){document.getElementById('sub').textContent='error cargando'}
}

document.getElementById('btnCheck').addEventListener('click',async()=>{
  toast('Forzando check...');try{await fetch('/action/check',{method:'POST'})}catch(e){}
  setTimeout(load,3000);
});
document.getElementById('tlToggle').addEventListener('click',()=>{
  const w=document.getElementById('tlWrap');const b=document.getElementById('tlToggle');
  const open=w.classList.toggle('open');b.classList.toggle('open',open);
  b.firstChild.textContent=open?'📜 Ocultar histórico ':'📜 Ver histórico de drops ';
});
document.getElementById('backBtn').addEventListener('click',showHome);
showHome();           // arrancar en la Home
load();setInterval(load,5000);
window.addEventListener('resize',()=>{if(currentView==='detail')load();});
</script></body></html>
"""

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass

    def _safe_write(self, body):
        # Escribir ignorando desconexiones del cliente (polling, recargas, móvil)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            log.error(f"web write error: {e}")

    def _json(self, obj, code=200):
        try:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:
            log.error(f"web header error: {e}")
            return
        self._safe_write(body)

    def do_POST(self):
        try:
            if self.path.startswith('/action/check'):
                parts = self.path.split('/')
                cid = parts[3] if len(parts) > 3 and parts[3] else None
                if cid and get_monitor(cid):
                    get_monitor(cid).control['force_check'] = True
                else:
                    for m in MONITORS.values():
                        m.control['force_check'] = True
                self._json({'ok': True})
            else:
                self._json({'ok': False}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        try:
            if self.path == '/data' or self.path.startswith('/data?'):
                self._json({'cases': [m.dashboard for m in MONITORS.values()]})
            elif self.path == '/health':
                cases_health = [m.health() for m in MONITORS.values()]
                all_ok = all(c['healthy'] for c in cases_health) if cases_health else False
                self._json({'ok': all_ok, 'cases': cases_health,
                            'ts': datetime.utcnow().isoformat() + 'Z'},
                           200 if all_ok else 503)
            else:
                body = DASHBOARD_HTML.encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                self._safe_write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

from socketserver import ThreadingMixIn
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # Evita que un error en un hilo de petición tumbe el servidor
    def handle_error(self, request, client_address):
        pass

def start_web():
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()

# ── LOOP PRINCIPAL ────────────────────────────────────────────────────────────
def case_loop(m):
    """Hilo por caja: chequea cada CHECK_INTERVAL, respeta force_check y rate-limit."""
    last_check = 0
    fails = 0
    while True:
        try:
            now = time.time()
            # Respetar rate-limit: si la API nos limitó, esperar
            if m.is_rate_limited():
                time.sleep(2)
                continue
            due = (now - last_check) >= CHECK_INTERVAL
            forced = m.control['force_check']
            if due or forced:
                if forced:
                    m.control['force_check'] = False
                    log.info(f"[{m.case_id}] check forzado")
                ok = m.do_check()
                last_check = time.time()
                if ok:
                    if fails >= 3:
                        send_telegram(f"✅ <b>{m.name}:</b> conexión restablecida con la API.")
                    fails = 0
                else:
                    fails += 1
                    m.dashboard['status'] = 'sin datos'
                    if fails == 3:
                        send_telegram(f"⚠️ <b>{m.name}:</b> 3 fallos seguidos leyendo la API.")
            time.sleep(1)
        except Exception as e:
            # Una excepción inesperada NO debe matar el hilo de la caja
            log.error(f"[{m.case_id}] error en case_loop: {e}")
            time.sleep(5)

def main():
    log.info(f"CS2 Multi-Monitor arrancando con {len(CASES)} cajas...")
    build_monitors()
    threading.Thread(target=start_web, daemon=True).start()
    threading.Thread(target=telegram_poll_loop, daemon=True).start()

    # Cargar estado de cada caja (seed > state.json > backup)
    resumed = []
    for m in MONITORS.values():
        seeded = m.apply_seed_if_present()
        recovered = seeded or m.load()
        if recovered and m.state.get('base_counter'):
            resumed.append((m, seeded))

    if resumed:
        lines = '\n'.join(f"• {m.name}: base <code>{m.state['base_counter']:,}</code>"
                          + (" (seed)" if seeded else "") for m, seeded in resumed)
        send_telegram(f"♻️ <b>Multi-Monitor reanudado</b>\n{len(resumed)} de {len(CASES)} cajas "
                      f"con estado previo:\n{lines}")
    else:
        send_telegram(f"⏳ <b>CS2 Multi-Monitor desplegado</b> — {len(CASES)} cajas, conectando...")

    # Un hilo por caja, con supervisión
    threads = {}
    for m in MONITORS.values():
        t = threading.Thread(target=case_loop, args=(m,), daemon=True)
        t.start()
        threads[m.case_id] = t

    # Watchdog: si un hilo de caja muere, lo reinicia y avisa
    while True:
        time.sleep(30)
        for cid, m in MONITORS.items():
            t = threads.get(cid)
            if t is None or not t.is_alive():
                log.error(f"[{cid}] hilo caído — reiniciando")
                send_telegram(f"🔄 <b>{m.name}:</b> el hilo se detuvo, reiniciándolo automáticamente.")
                nt = threading.Thread(target=case_loop, args=(m,), daemon=True)
                nt.start()
                threads[cid] = nt

def self_test():
    """Valida la lógica clave sin red. Útil antes de desplegar: python3 monitor.py --test"""
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond: ok = False

    print("── SELF-TEST ──")
    # Math
    check("prob_acum(0.0001, 10000) ≈ 63.2%", abs(prob_acum(0.0001, 10000) - 63.21) < 1)
    check("combined_prob(0, n) = 0", combined_prob(0, 100) == 0)
    check("prob_acum con n=0 es 0", prob_acum(0.01, 0) == 0)
    # Nombre base
    m = CaseMonitor('cc-test', 'Test', '/tmp/_selftest.json')
    check("_base_name quita desgaste", m._base_name("AK-47 | Vulcan (Field-Tested)") == "AK-47 | Vulcan")
    check("_base_name quita StatTrak", m._base_name("StatTrak™ AK-47 | Vulcan (FT)") == "AK-47 | Vulcan")
    check("_base_name quita estrella", m._base_name("★ Butterfly Knife | Fade (FN)") == "Butterfly Knife | Fade")
    # Estado sano
    check("_sane rechaza no-dict", not m._sane("x"))
    check("_sane acepta dict válido", m._sane({'base_counter': 100}))
    check("_sane rechaza base negativo", not m._sane({'base_counter': -5}))
    # Migración de claves viejas
    m.state['group_last_seen'] = {'ak47': 500, 'awp': 600}
    m.state['group_probs'] = {'ak47': 0.0001, 'awp': 0.00002}
    m._migrate_legacy_keys()
    check("migración traduce ak47", 'AK-47 | Vulcan' in m.state['group_last_seen'])
    check("migración elimina clave vieja", 'ak47' not in m.state['group_last_seen'])
    # only_items
    m2 = CaseMonitor('cc-test2', 'Test2', '/tmp/_selftest2.json', only_items=['m4a4 | hellfire'])
    check("only_items en minúsculas", m2.only_items == ['m4a4 | hellfire'])

    # limpiar
    for f in ('/tmp/_selftest.json', '/tmp/_selftest2.json'):
        try: os.remove(f)
        except Exception: pass

    print("── RESULTADO:", "TODO OK ✅" if ok else "HAY FALLOS ❌", "──")
    return ok

if __name__ == '__main__':
    import sys
    if '--test' in sys.argv:
        sys.exit(0 if self_test() else 1)
    main()

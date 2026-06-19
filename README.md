# CS2 Case Monitor — versión API (definitiva)

Monitor autónomo para skin.club usando su API pública. Sin login, sin navegador,
sin proxy. Alertas por Telegram + panel web en vivo.

## Cómo funciona

skin.club expone una API pública (sin autenticación) que devuelve TODO:
  https://gate.skin.club/apiv2/cases/cc-bf4940a7e

De ahí sacamos:
- stats.opening_count → contador exacto de aperturas de la caja (tiempo real)
- last_successful_generation.contents → probabilidades exactas de cada item
- top_drops → feed de últimos drops con timestamp

Es solo un GET de JSON, así que un requests.get() basta. Gasto de datos mínimo.

## Despliegue en Railway

1. Sube a GitHub: Dockerfile, monitor.py, requirements.txt, railway.toml
2. Railway → New Project → Deploy from GitHub repo
3. Variables de entorno:

| Variable | Valor |
|----------|-------|
| TELEGRAM_TOKEN | tu token |
| TELEGRAM_CHAT_ID | tu chat ID |
| CASE_ID | cc-bf4940a7e |
| CHECK_INTERVAL | 60 |
| THRESHOLD_IND | 90 |
| THRESHOLD_COMB | 90 |
| ALERT_COOLDOWN | 300 |

4. Settings → Networking → Generate Domain → esa URL es tu panel web en vivo

## Si la API bloquea la IP de Railway

Recibirás "⚠️ 3 fallos seguidos". En ese caso (poco probable con una API),
añade proxy o usa una Raspberry. Pero las APIs raramente bloquean por IP.

## Mensajes Telegram

- ⏳ Deploy correcto
- 🟢 Iniciado (con probabilidades reales de la API)
- ✅ Drop raro detectado
- 🔥 Alerta individual
- ⚠️ Alerta sequía global

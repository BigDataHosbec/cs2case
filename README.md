# CS2 Case Monitor — versión completa

Monitor autónomo para skin.club vía API pública. Alertas Telegram + panel web
en vivo con gráfica + control total desde panel y Telegram + estado persistente.

## Funciones

- Lee la API pública de skin.club (contador exacto, probabilidades, drops)
- Presión acumulada individual por item + sequía global combinada
- Alertas Telegram: momento caliente, sequía global, drop detectado, caja muerta
- Panel web en vivo: KPIs, gráfica de evolución de presión, presión por item,
  histórico de drops con estadísticas
- Detección de "caja muerta" (contador sin subir en DEAD_AFTER_MIN minutos)
- Botones en panel: forzar check, ir a la caja, reset (con confirmación)
- Control desde Telegram: /menu, /estado, /check, /historico, /reset (con confirmación)
- Persistencia: el estado sobrevive a reinicios y redeploys (Volume de Railway)

## Despliegue en Railway

### 1. Sube a GitHub: Dockerfile, monitor.py, requirements.txt, railway.toml

### 2. Crea un Volume en Railway → mount path: /data

### 3. Variables de entorno

| Variable | Valor | Descripción |
|----------|-------|-------------|
| TELEGRAM_TOKEN | tu token | bot de Telegram |
| TELEGRAM_CHAT_ID | tu chat ID | tu chat |
| CASE_ID | cc-bf4940a7e | id de la caja |
| CHECK_INTERVAL | 60 | segundos entre checks |
| THRESHOLD_IND | 90 | % alerta individual |
| THRESHOLD_COMB | 90 | % alerta sequía global |
| ALERT_COOLDOWN | 300 | seg entre alertas repetidas |
| DEAD_AFTER_MIN | 60 | min sin subir = caja muerta |
| STATE_FILE | /data/state.json | archivo de estado persistente |

### 4. Settings → Networking → Generate Domain → URL del panel

## Comandos de Telegram

- /menu — abre el panel de botones
- /estado — estado actual completo
- /check — fuerza un check inmediato
- /historico — últimos drops y estadísticas
- /reset — resetea el conteo (pide confirmación)

## Panel web

KPIs (total cajas, desde inicio, valor esperado, retorno), gráfica de evolución
de la presión combinada con línea del 90%, presión individual por item (rediseñada
para legibilidad), gráfica de histórico y timeline de drops. Botones de check,
ir a la caja y reset con confirmación.

## Nota sobre el primer deploy

La primera vez se pierde el conteo en memoria anterior (aún no había archivo en
disco). A partir de ahí queda blindado: cualquier reinicio recupera el estado.

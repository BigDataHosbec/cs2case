# CS2 Case Monitor — Servidor 24/7 (Playwright)

Monitor automático para skin.club con alertas por Telegram.

## ⚠️ IMPORTANTE — Por qué Playwright y no requests

skin.club genera los drops y el contador mediante JavaScript en el navegador.
El HTML que devuelve el servidor está VACÍO de datos. Por eso un scraper normal
con requests/BeautifulSoup no funciona: hace falta un navegador real headless
(Chromium vía Playwright) que ejecute ese JavaScript.

## Despliegue en Railway

### 1. Sube estos archivos a un repo de GitHub
- Dockerfile
- monitor.py
- requirements.txt
- railway.toml

### 2. En Railway: New Project → Deploy from GitHub repo

### 3. Variables de entorno (Settings → Variables)

| Variable | Valor |
|----------|-------|
| TELEGRAM_TOKEN | tu token del bot |
| TELEGRAM_CHAT_ID | tu chat ID |
| CASE_URL | https://skin.club/es/cases/open/cc-bf4940a7e |
| CHECK_INTERVAL | 60 |
| THRESHOLD_IND | 90 |
| THRESHOLD_COMB | 90 |
| ALERT_COOLDOWN | 300 |

### 4. Deploy
Railway detecta el Dockerfile (imagen oficial de Playwright con Chromium ya
instalado) y despliega. El primer build tarda ~3-4 min porque la imagen es grande.

Al arrancar recibirás en Telegram:
1. "⏳ CS2 Monitor desplegado" — confirma que el deploy funciona
2. "🟢 CS2 Monitor iniciado" — confirma que el scrapeo funciona (con counter base)

Si recibes el (1) pero NO el (2), significa que skin.club está bloqueando la IP
del datacenter de Railway. En ese caso la alternativa es la Raspberry Pi en casa
(IP residencial) con el mismo código.

## Mensajes

- ⏳ Deploy correcto
- 🟢 Scrapeo funcionando (counter base)
- ✅ Drop raro detectado (resetea su contador)
- 🔥 Alerta individual (item supera umbral)
- ⚠️ Alerta sequía global (X cajas sin ningún raro)

# CS2 Case Monitor — Servidor 24/7

Monitor automático para skin.club que envía alertas por Telegram.

## Despliegue en Railway (gratis)

### 1. Crear cuenta en Railway
- Ve a https://railway.app y regístrate con GitHub

### 2. Subir el código a GitHub
- Crea un repo nuevo en https://github.com/new (puede ser privado)
- Sube estos archivos: monitor.py, requirements.txt, Procfile, railway.toml

### 3. Crear proyecto en Railway
- En Railway: New Project → Deploy from GitHub repo
- Selecciona tu repo

### 4. Configurar variables de entorno
En Railway → tu proyecto → Variables, añade:

| Variable | Valor |
|----------|-------|
| TELEGRAM_TOKEN | tu token del bot |
| TELEGRAM_CHAT_ID | tu chat ID |
| CASE_URL | https://skin.club/es/cases/open/cc-bf4940a7e |
| CHECK_INTERVAL | 60 |
| THRESHOLD_IND | 90 |
| THRESHOLD_COMB | 90 |
| ALERT_COOLDOWN | 300 |

### 5. Deploy
Railway despliega automáticamente. Ve a Logs para ver que funciona.
Deberías recibir un mensaje de Telegram "🟢 CS2 Monitor iniciado".

## Mensajes que recibirás

- 🟢 Al arrancar: confirmación con counter base
- ✅ Cuando cae un drop raro: nombre, wear, precio, counter
- 🔥 Alerta individual: cuando un item supera el umbral (ej: Butterfly al 90%)
- ⚠️ Alerta sequía global: cuando llevan X cajas sin ningún drop raro

## Variables opcionales

- CHECK_INTERVAL: segundos entre checks (default 60, mínimo recomendado 30)
- THRESHOLD_IND: % para alerta individual (default 90)
- THRESHOLD_COMB: % para alerta sequía global (default 90)
- ALERT_COOLDOWN: segundos entre alertas repetidas del mismo tipo (default 300)

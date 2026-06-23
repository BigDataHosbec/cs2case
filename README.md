# CS2 Multi-Case Monitor — edición robusta

Monitor autónomo multi-caja para skin.club. Alertas Telegram + panel web con
análisis estadístico + health check + estado persistente con backup.

## Funciones principales
- Multi-caja en paralelo (hilo + estado independiente por caja)
- Detección automática de raros por probabilidad (<= RARE_MAX_CHANCE)
- Filtro only_items por caja (seguir solo items concretos)
- Presión acumulada individual y combinada, alertas por niveles (90/95%)
- Análisis tasa observada vs declarada (honestidad de la caja) con confianza
- Factor de anomalía: avisa si una caja lleva N× lo esperado sin pagar
- Histórico de drops con presión al caer, gráfica de 14 días
- Caja muerta, racha anómala, cambios de API, rate-limit: todo con avisos
- Persistencia con backup rotativo + recuperación ante corrupción
- Watchdog que reinicia hilos caídos · health endpoint /health
- Logging rotativo a /data/monitor.log
- Horarios en hora de España peninsular

## Endpoints
- /        panel web
- /data    JSON de todas las cajas
- /health  estado de salud (200 si OK, 503 si alguna caja no responde)

## Validar antes de desplegar
    python3 monitor.py --test

## Variables de entorno
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (obligatorias). Opcionales: CHECK_INTERVAL (60),
ALERT_LEVELS (90,95), DEAD_AFTER_MIN (60), RARE_MAX_CHANCE (0.01),
ANOMALY_FACTOR (3), DATA_DIR (/data), CASES_JSON (override de cajas).

## Monitorización externa recomendada
Apunta UptimeRobot (gratis) a https://<tu-dominio>/health cada 5 min para que te
avise por email/push si el sistema entero cae (cuando Telegram tampoco respondería).

## Cajas por defecto
- DONT TRUST (cc-bf4940a7e)
- 1º NO PAIN 85% (cc-08e7b18f7)
- NO PAIN 84% PROFIT (cc-c81e43fb8) — solo M4A4 Hellfire

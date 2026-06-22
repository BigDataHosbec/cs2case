# CS2 Multi-Case Monitor

Monitor autónomo para varias cajas de skin.club vía API pública. Alertas Telegram
+ panel web con Home de selección + gráfica 14 días + estado persistente por caja.

## Cajas monitorizadas (por defecto)
- DONT TRUST (cc-bf4940a7e)
- 1º NO PAIN 85% (cc-08e7b18f7)
- NO PAIN 84% PROFIT (cc-c81e43fb8)

## Cómo funciona
Cada caja se monitoriza en su propio hilo, con estado independiente en
/data/state_<id>.json (la caja original usa /data/state.json por compatibilidad).
Los items raros se detectan AUTOMÁTICAMENTE: cualquier item con probabilidad
<= RARE_MAX_CHANCE (0.01% por defecto) se sigue como raro y se agrupa por nombre.

## Variables de entorno

| Variable | Defecto | Descripción |
|----------|---------|-------------|
| TELEGRAM_TOKEN | (obligatoria) | token del bot |
| TELEGRAM_CHAT_ID | (obligatoria) | tu chat ID |
| CHECK_INTERVAL | 60 | segundos entre checks |
| ALERT_LEVELS | 90,95 | niveles % de alerta |
| DEAD_AFTER_MIN | 60 | min sin subir = caja muerta |
| RARE_MAX_CHANCE | 0.01 | % máximo para considerar un item "raro" |
| DATA_DIR | /data | carpeta de estados (Volume) |
| CASES_JSON | (opcional) | lista de cajas en JSON para override |

## Añadir/quitar cajas
Edita la lista CASES en el código, o usa la variable CASES_JSON, ej:
[{"id":"cc-xxx","name":"Mi Caja"}]

## Panel web
Home con las cajas (cada una muestra su % de sequía y datos). Pulsa una para
ver su monitor completo. "◄ Volver" regresa a la lista.

## Comandos de Telegram
/menu (botones por caja) · /estado · /historico · /check (todas)

## Migración automática
Al actualizar desde la versión mono-caja, el estado existente (claves ak47,
butterfly...) se traduce automáticamente a los nombres nuevos sin perder el conteo.

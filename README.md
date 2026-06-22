# CS2 Case Monitor — versión multi-caja

Monitor autónomo para skin.club vía API pública. Alertas Telegram + panel web
con Home de selección de cajas, gráfica de 14 días + estado persistente.

## Novedades de esta versión
- Panel con "Home": pantalla de selección de caja (preparada para varias cajas)
- KPI simplificado: solo total de cajas abiertas (control)
- Gráfica de presión combinada con ventana de 14 días
- Horarios de drops en hora de España peninsular (Europe/Madrid, auto verano/invierno)
- Alertas por niveles: avisa 1 vez al superar 90% y 1 vez al superar 95%
  (se rearma cuando cae un drop y la presión baja)
- Sin botón Reset (eliminado para evitar borrados accidentales)

## Cómo funciona
Lee la API pública: https://gate.skin.club/apiv2/cases/<CASE_ID>
Saca contador exacto, probabilidades y feed de drops. Calcula presión
acumulada individual y combinada. Estado persistente en Volume de Railway.

## Variables de entorno

| Variable | Valor por defecto | Descripción |
|----------|-------------------|-------------|
| TELEGRAM_TOKEN | (obligatoria) | token del bot |
| TELEGRAM_CHAT_ID | (obligatoria) | tu chat ID |
| CASE_ID | cc-bf4940a7e | id de la caja |
| CASE_NAME | DONT TRUST | nombre visible en la Home |
| CHECK_INTERVAL | 60 | segundos entre checks |
| ALERT_LEVELS | 90,95 | niveles % de alerta |
| DEAD_AFTER_MIN | 60 | min sin subir = caja muerta |
| STATE_FILE | /data/state.json | estado persistente (Volume) |

Solo TELEGRAM_TOKEN y TELEGRAM_CHAT_ID son obligatorias; el resto tiene default.

## Comandos de Telegram
/menu · /estado · /check · /historico

## Añadir más cajas en el futuro
La Home ya está preparada para listar varias cajas. Actualmente el backend
monitoriza la caja de CASE_ID. Para multi-caja real se ampliará el backend
para seguir varias en paralelo (estructura ya contemplada en el dashboard).

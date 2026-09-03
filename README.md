Radar de Podbike Frikar en kleinanzeigen.de → Telegram
Vigila queries en kleinanzeigen.de cada hora desde GitHub Actions (sin servidor,sin máquina encendida) y avisa a Telegram de: 🆕 nuevos, 📉/📈 cambios de precio(con gráfica), 💰 precio aparecido, 🗑️ retirados, 🔁 republicaciones, ✏️ ediciones,⚠️ fallos y 📋 latido dominical. Ahora mismo está en modo piloto con la queryalleweder (~4 anuncios, la misma escala que tendrá el Frikar); al final de esteREADME se explica el cambio a producción (podbike + frikar).

Cómo funciona (30 segundos)
cron horario (GitHub Actions) → página 1 de cada query, ordenada por más recientes  → compara tarjetas con state.pilot.json → abre la ficha SOLO si hay novedad  → alerta a Telegram → commitea state + informe (el historial de git es la auditoría)
Cortesía: sin login, solo páginas públicas, peticiones secuenciales con pausas de2–5 s, típicamente 2–6 peticiones por hora. Ante 403/429/captcha la ejecución seaborta (nunca reintenta en bucle): 2.º bloqueo → ⚠️, 3.º → pausa de 12 h automática.

Estructura del repo
.github/workflows/radar.yml   workflow (cron, jitter, timeout, commits)src/radar.py                  todo el sistemaconfig.yaml                   queries, alertas, pausas, umbral opcionalrequirements.txt              dependencias con versión fijadastate.pilot.json              estado del piloto (lo crea la 1.ª ejecución)PILOT_REPORT.md               informe autogeneradokeepalive.txt                 latido dominical (evita desactivación por inactividad)
Montaje paso a paso
Crea un repo PÚBLICO vacío en GitHub y sube estos archivos a la raíz(sin state: lo crea la primera ejecución). En repos públicos Actions es gratis.
Crea el bot: habla con @BotFather → /newbot →guarda el token (123456789:ABC-...).
Obtén tu chat_id: manda cualquier mensaje a tu bot y abrehttps://api.telegram.org/bot<TU_TOKEN>/getUpdates en el navegador →copia "chat":{"id":123456789}. (Si lo añades a un grupo, el id es negativo.)
Crea los Secrets: repo → Settings → Secrets and variables → Actions →TELEGRAM_TOKEN y TELEGRAM_CHAT_ID. Nunca van al código ni a los commits.
Prueba manual: pestaña Actions → radar-kleinanzeigen → Run workflow(los runs manuales no esperan el jitter). En el log verás el diagnóstico(robots, orden, tarjetas parseadas, códigos HTTP de cada petición).Minutos después: mensaje en Telegram “✅ Radar activado — N anuncios enseguimiento”, y el workflow commitea state.pilot.json + PILOT_REPORT.md.
Listo. El cron 17 * * * * (UTC) corre cada hora; el script espera 0–10 minaleatorios antes de la primera petición. Dos ejecuciones nunca se solapan.
Del piloto a la producción
En config.yaml: comenta el bloque del piloto, descomenta el de producción,borra state.pilot.json (su historial queda en git) y haz Run workflow.Habrá una segunda baseline silenciosa y un nuevo “✅ Radar activado”. Nada másque reconfigurar: workflow, secrets y bot son los mismos.

Probar los escenarios (Fase A)
Edita state.pilot.json, commitea (o ejecuta en local), dispara el workflow ycomprueba el mensaje en Telegram:

Escenario	Edición en state	Esperado
🆕 + 🗑️ a la vez	cambia el id (y detail_url, últimos dígitos) de un anuncio activo	el fantasma se retira, el real aparece como nuevo (con foto y extracto)
📉 con gráfica	pon "value": 6000 en price y "history": [["<ayer>", 6000]]	Bajada −X € + gráfica PNG (2 precios distintos)
📈	igual, con valor menor al real	Subida +X €
💰	"value": null y "history": [["<ayer>", null]]	Precio aparecido
✏️ editado	añade texto al final de "description" y pon "last_detail_check" 2 días atrás	Editado con fragmento del cambio (re-check de ficha)
Reserviert desaparece	pon "reserved": true en un anuncio que no lo esté	✏️ “ya no está Reserviert ✅”
♻️ reaparición	pon "status": "retired" en un anuncio que sigue en la web	🆕 con histórico previo enlazado
🔁 republicación	crea una entrada "status": "retired" con tu mismo seller_name y un título casi igual al de un anuncio real, luego cambia el id del real	bloque 🔁 dentro del mensaje 🆕
⚠️ fallo	pon temporalmente base_url: "https://www.kleinanzeigen.invalid"	aviso de error inesperado en Telegram
📋 latido	en local: RADAR_FORCE_HEARTBEAT=1 python src/radar.py	mensaje de latido
Ejecutar en local (depuración)
pip install -r requirements.txtRADAR_DRY_RUN=1 python src/radar.py
Sin token imprime los mensajes en consola en vez de enviarlos. Consejo: apuntastate_file a state.local.json (está en .gitignore) para no tocar el del repo.La IP de tu casa no es la de Actions: el piloto real debe hacerse en Actions.

Ajustes frecuentes
Frecuencia: .github/workflows/radar.yml → cron (¡en UTC!: 12:17 Madrid= 10:17 UTC en verano, 11:17 UTC en invierno).
Umbral 🔥: price_threshold_eur: 5000 en config.yaml (null = verlo todo).
Alertas on/off, pausas, proxy: todo en config.yaml.
Los cambios de config deben estar en la rama por defecto (el cron solofunciona ahí).
Problemas
No llegan mensajes → revisa token y chat_id (paso 3-4); en el log deActions busca líneas [TG].
403/429 → la escalera es automática (2.º ⚠️, 3.º pausa 12 h). Si es crónico,configura proxy_url (proxy residencial) en config.yaml.
“estructura no reconocida” → el sitio ha rediseñado: manda el log(líneas [search]/[parse]); los selectores del radar son multi-fallback yse corrigen rápido.
Workflow desactivado por inactividad (GitHub, ~60 días sin commits) →pestaña Actions → Enable workflow. El latido dominical commitea cada semanaprecisamente para que no ocurra.
state corrupto → git checkout <commit> -- state.pilot.json restaura elúltimo válido; el radar también se recupera solo (baseline silenciosa).
Privacidad y cortesía
El repo es público pero solo contiene datos ya públicos de los anuncios (títulos,precios, IDs). El token de Telegram y tu chat_id viven en Secrets. Sin login, sincookies persistentes, sin automatizar mensajes ni compras: solo lectura depáginas públicas con volumen minúsculo.

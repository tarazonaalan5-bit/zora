# Zora — Backend + Agente de laptop (prototipo)

Todo corre con Python puro — no necesitas instalar nada (`pip install` no
hace falta para este prototipo).

## 1. Backend (el "cerebro")

```bash
python3 zora_backend.py
```

Levanta el servidor en `http://localhost:8000`.

Para conectar comandos complejos a Claude de verdad:
```bash
export ANTHROPIC_API_KEY="tu-api-key"
python3 zora_backend.py
```

## 2. Registrar la laptop y obtener su device_token

Con el backend corriendo, desde cualquier cliente HTTP (o con el script
`test_agente.py` de ejemplo):

```bash
curl -X POST http://localhost:8000/register -d '{"username":"papa","password":"1234"}'
TOKEN=$(curl -s -X POST http://localhost:8000/login -d '{"username":"papa","password":"1234"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s -X POST http://localhost:8000/devices \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"Laptop de papá","type":"laptop"}'
```

Esto te devuelve un `device_token` — **guárdalo**, es lo que usa el agente
para autenticarse (nunca uses tu password de usuario dentro del agente).

## 3. Arrancar el agente en la laptop

```bash
export ZORA_DEVICE_TOKEN="el-device-token-de-arriba"
export ZORA_BACKEND_URL="http://localhost:8000"
python3 zora_laptop_agent.py
```

El agente queda corriendo, preguntando cada 3 segundos si hay comandos
pendientes para esta laptop (poll — la laptop siempre inicia la conexión
hacia el backend, así no necesitas abrir puertos en tu router).

## 4. Mandar un comando a la laptop

```bash
curl -X POST http://localhost:8000/command \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text":"sube el volumen","device_id":"<el device_id de la laptop>"}'
```

El agente lo recoge en su próximo poll, lo ejecuta y reporta el resultado.
Puedes consultar el resultado con:
```bash
curl "http://localhost:8000/command/status?command_id=<command_id>"
```

`test_agente.py` automatiza los pasos 2 y 4 como ejemplo.

## 5. Conector de voz (hablar en vez de escribir, y que Zora responda hablando)

```bash
export ZORA_USER_TOKEN="tu-token-de-login"
python3 zora_voice_connector.py grabacion.wav [device_id opcional]
```

Convierte el audio a texto y lo manda directo a `/command`, y **además
convierte la respuesta de Zora de vuelta a voz (femenina)**.

### Voz → texto (lo que tú dices), 3 modos en este orden:
1. **Whisper local** (gratis, sin internet, si instalas `openai-whisper`).
2. **API de Whisper de OpenAI** (si defines `OPENAI_API_KEY`).
3. **Modo demo**: si ninguna de las dos está disponible, busca un `.txt`
   con el mismo nombre que el audio (ej. `grabacion.wav` → `grabacion.txt`)
   y usa ese texto como transcripción simulada — así puedes probar todo el
   pipeline (voz → texto → backend → respuesta) sin depender de audio real.

### Texto → voz (lo que Zora responde), mismo orden de 3 modos:
1. **pyttsx3 local** (gratis, offline): instálalo con
   `pip install pyttsx3 --break-system-packages`. El script busca
   automáticamente una voz femenina entre las instaladas en tu sistema
   (ej. "Zira"/"Helena" en Windows, "Mónica"/"Paulina" en macOS, o fuerza
   la variante femenina de espeak en Linux). Genera un `.wav`.
2. **API de OpenAI TTS** (si defines `OPENAI_API_KEY`): usa el modelo
   `tts-1` con la voz `nova` (femenina), genera un `.mp3`.
3. **Modo demo**: si no hay ni pyttsx3 ni API key (o no hay salida de
   audio real, como en este sandbox), guarda el texto que Zora "diría"
   en un `..._respuesta.txt`, para poder revisar el flujo completo sin
   depender de un motor de audio real.

**Nota sobre voces del sistema operativo**: en Windows y macOS ya vienen
voces femeninas en español preinstaladas (Windows: "Helena"/"Sabina";
macOS: "Mónica"/"Paulina"), así que en tu laptop real probablemente no
necesites instalar nada extra para el modo 1 — solo `pip install pyttsx3`.

## 6. Cliente web (interfaz de chat real, en el navegador)

```bash
python3 zora_backend.py   # déjalo corriendo
```

Abre `zora_web_client.html` directo en tu navegador (doble clic, o
`open zora_web_client.html` / arrastrarlo a Chrome). No necesita servidor
propio — es un solo archivo HTML.

Qué hace:
- Pantalla de login/registro contra tu backend (URL configurable, por
  defecto `http://localhost:8000`).
- Chat con burbujas, igual al diseño que definimos (fondo oscuro, acento
  rosa, header "Zora").
- Selector de dispositivo: puedes dirigir el comando a una laptop
  registrada o dejarlo "sin dispositivo" (va directo al cerebro en la
  nube o se ejecuta como intención).
- **Micrófono real**: usa el reconocimiento de voz nativo del navegador
  (Web Speech API) — no necesita Whisper ni Python, funciona directo en
  Chrome/Edge. Habla y el texto se transcribe y se manda solo.
- **Voz de mujer real**: usa la síntesis de voz nativa del navegador
  (Speech Synthesis API) y elige automáticamente una voz femenina en
  español entre las que tenga instaladas tu sistema operativo — el
  navegador SÍ puede reproducir audio real (a diferencia del sandbox
  donde se probó `zora_voice_connector.py`, que no tiene salida de audio).

Nota importante: el backend necesitó agregar cabeceras CORS
(`Access-Control-Allow-Origin`) para que el navegador le permita al
cliente web hacerle peticiones — sin esto, el navegador bloquea las
llamadas por política de seguridad. Ya está incluido en `zora_backend.py`.

## Seguridad ya incluida en el prototipo

- Contraseñas nunca en texto plano (hash con salt).
- Tokens de sesión (login) separados de los **device tokens** (agente) —
  si se filtra uno, no compromete el otro.
- Reglas de permisos: la TV es compartida, la laptop/celular son personales
  (solo el dueño puede mandarles comandos).
- El agente NUNCA ejecuta texto/código arbitrario — solo sabe hacer las
  acciones que están explícitamente en `ALLOWED_ACTIONS` dentro de
  `zora_laptop_agent.py`. Aunque alguien comprometiera el backend, lo
  máximo que podría hacer es invocar una de esas acciones ya definidas.

## Sobre las acciones del agente (importante)

Las funciones `action_*` en `zora_laptop_agent.py` están en modo DEMO —
simulan la acción (o hacen algo inofensivo, como comprobar si un programa
existe con `which`) para que puedas probar todo el flujo en cualquier
máquina sin que "abra Spotify" de verdad en este sandbox. Cuando lo
instales en tu laptop real, reemplaza el contenido de cada función por el
comando real de tu sistema operativo:

- **Windows**: `subprocess.Popen(["cmd", "/c", "start", "spotify:"])`,
  `nircmd` para volumen, `rundll32.exe user32.dll,LockWorkStation` para
  bloquear pantalla.
- **macOS**: `osascript` para volumen/apps, `pmset displaysleepnow` para
  bloquear pantalla.

La estructura (poll → ejecutar → reportar) no cambia entre sistemas, solo
el contenido de cada acción.

## Qué se probó y funciona (verificado en este sandbox)

- Registro/login con tokens.
- Comandos rápidos (regex) sin pasar por IA.
- Cola de comandos: el backend encola, el agente hace poll y ejecuta.
- Reporte de resultados de vuelta al backend, consultable por `command_id`.
- Reglas de permiso por tipo de dispositivo (TV compartida vs. laptop personal).
- Voz → texto → backend → texto → voz de mujer, para los 3 tipos de
  respuesta (`cloud_brain`, `quick_command`, `queued_for_agent`), con la
  lógica de selección de voz femenina verificada contra voces típicas de
  Windows, macOS y espeak/Linux.
- Cliente web completo probado con navegador real (Playwright): registro,
  login, selector de dispositivos, comando que va al cerebro en la nube,
  comando rápido reconocido, y comando encolado hacia una laptop
  registrada — los tres tipos de respuesta del backend funcionando en
  la interfaz de principio a fin.

## 7. Emergencias, geocercas, contactos de confianza y actividad (nuevo)

Todo esto ya vive en `zora_backend.py`, guardado en SQLite (`zora.db`,
se crea solo al arrancar el backend por primera vez):

- **Contactos de confianza**: `POST /contacts` (nombre, correo, teléfono),
  `GET /contacts`, `DELETE /contacts?id=...`.
- **Geocercas**: `POST /geofences` (nombre, lat, lon, radio en metros,
  opcionalmente atada a un solo dispositivo), `GET /geofences`,
  `DELETE /geofences?id=...`.
- **Ubicación**: `POST /location` (con `device_token` del agente, o con tu
  token de usuario + `device_id`). Cada vez que llega una ubicación nueva,
  el backend revisa TODAS tus geocercas y genera una alerta automática si
  el dispositivo entró o salió de alguna (`GET /alerts` para ver el
  historial).
- **SOS**: decir "ayuda", "auxilio", "sos" o "emergencia" en el chat
  dispara la alerta automáticamente (sin pasar por el cerebro en la nube,
  para que sea instantáneo); también hay un botón dedicado "🚨 ENVIAR SOS
  AHORA" en la pestaña "Emergencia" del cliente web/apps. El SOS manda un
  correo real a cada contacto que tenga email guardado **si configuras
  las variables `ZORA_SMTP_*`** (ver encabezado de `zora_backend.py`); si
  no las configuras, sigue funcionando en modo demo (se guarda todo y se
  puede consultar con `GET /sos/history`, pero no sale ningún correo real).
- **Actividad física**: `POST /activity` (pasos, distancia) y
  `GET /activity/today?device_id=...` — el comando de voz "cuántos pasos
  llevo" ahora sí devuelve datos reales guardados, ya no un simulacro.

El cliente web (y por lo tanto las 4 apps, que reusan el mismo HTML) tiene
una pestaña nueva "Emergencia" con el botón de SOS, el formulario de
contactos, el de geocercas (con botón "usar mi ubicación actual", vía
geolocalización del navegador) y el historial de alertas. Probado de
punta a punta con Playwright: registro, login, agregar contacto, agregar
geocerca con coordenadas reales, y disparo de SOS — los cuatro pasos
funcionan.

## 8. APIs de IA gratis, con aviso automático cuando se acaban los tokens (nuevo)

Conecté dos servicios de IA que dan tokens/créditos gratis (sin tarjeta de
crédito), y les agregué un sistema propio de control de cuota — Zora
lleva la cuenta de cuántos usos van, y cuando se acaban, en vez de fallar
feo, avisa con un mensaje claro diciendo cuándo se recargan:

- **Generación de imágenes** ("hazme una imagen de...", "dibuja...",
  "genera una imagen de..."): usa Google Gemini 2.5 Flash Image ("Nano
  Banana") vía Google AI Studio. Consigues la key gratis, sin tarjeta, en
  https://aistudio.google.com/ y la activas con
  `export GOOGLE_AI_API_KEY="tu-key"`. El tier gratis de Google es
  ~500 imágenes/día (yo dejé el límite propio en 480 como margen de
  seguridad) — se reinicia cada medianoche.
- **Cerebro de respaldo** (cuando no tienes `ANTHROPIC_API_KEY`, o se te
  acaba el saldo de Claude): usa NVIDIA Build, que da una API key gratis
  sin tarjeta con más de 80 modelos (Llama, DeepSeek, GLM...) en
  https://build.nvidia.com/ — la activas con
  `export NVIDIA_API_KEY="nvapi-..."`. Zora la usa automáticamente como
  respaldo; no reemplaza a Claude en calidad, pero evita que te quedes
  sin cerebro si no quieres pagar nada. Créditos gratis reportados por
  NVIDIA: del orden de 1000/mes (puede cambiar, no depende de mí — por
  eso Zora también detecta el error "429 - cuota agotada" que devuelve
  la API misma, no solo cuenta a ciegas con un número fijo).

Cómo se avisa cuando se acaban: cada vez que Zora usa una de estas dos
APIs, guarda el conteo en `zora.db`. Si pides algo y ya no queda cuota,
responde en el chat algo como *"Se me acabaron los tokens gratis de
imágenes por hoy (480/480) — se recargan mañana a medianoche"* — sin
intentar la llamada real (así no pierdes tiempo esperando un error).
También puedes preguntar directamente "cuántos tokens de imágenes me
quedan" en cualquier momento, o consultar `GET /usage` para ver las dos
cuotas juntas. En cuanto pasa la medianoche (o cambia el mes, para
NVIDIA), el contador se reinicia solo — no hace falta que hagas nada, ni
que me avises tú: la próxima vez que pidas algo, si ya tocó el reinicio,
simplemente vuelve a funcionar.

Probado en este sandbox (sin las API keys reales, porque no las tengo):
confirmé que sin key configurada Zora avisa con el mensaje correcto en
vez de fallar, que `/usage` reporta bien las dos cuotas, y que si se
fuerza el contador local al límite, Zora ni siquiera intenta llamar a la
API real — responde de inmediato con el aviso de cuándo se recarga. Lo
único que no pude probar aquí es la llamada real a Google/NVIDIA (necesito
que pongas tus propias keys, que son gratis, en los links de arriba).

## 9. Cómo llegan las actualizaciones futuras a las 6 plataformas (nuevo)

Pregunta importante que resolví: **¿los cambios futuros (nuevas APIs,
nuevas funciones) se ven solos en Windows/Mac/Linux/Android/Android TV/
iOS, o hay que reinstalar cada app?** Depende de qué tipo de cambio sea:

- **Cambios en el backend** (`zora_backend.py` — nuevas APIs como las de
  hoy, SMS, deportes, reglas de geocercas, lo que sea): **ya llegaban
  solos** desde el principio. Las 6 plataformas le hablan al mismo
  backend por red — apenas lo reinicias con el cambio, todas lo ven, sin
  tocar ninguna app.
- **Cambios en la interfaz** (`zora_web_client.html`): por defecto, cada
  app traía su propia copia pegada por dentro, así que NO se actualizaban
  solas. Esto lo resolví ahora: el backend también sirve la interfaz él
  mismo (como página web normal, en su propia URL — carpeta `static/`
  nueva), y las apps de escritorio y Android se pueden configurar para
  cargarla desde ahí en cada arranque en vez de la copia local. La app de
  iOS (PWA) ya funcionaba así desde que la armamos, sin que hiciera falta
  ningún cambio.

Instrucciones completas, con los archivos de configuración exactos de
cada plataforma, en la sección 5 de `COMO_COMPILAR.md` (dentro de
`zora-apps.zip`). Para que esto sea estable de verdad en las 6 (no solo
mientras tu celular y tu PC estén en la misma WiFi), conviene combinarlo
con `DESPLIEGUE_INTERNET.md` — un backend en una URL fija es lo que hace
que "se actualiza sola" signifique lo mismo estés donde estés.

Probado en este sandbox: confirmé que `GET /` del backend sirve la
interfaz completa (con los headers y content-type correctos para el
manifest y los íconos), y que abrir esa URL directamente en un navegador
(sin usar el archivo HTML local) funciona de punta a punta con
Playwright — login, comando de texto, respuesta — igual que el cliente
que ya veníamos probando.

## 10. Página de descargas — instalar con un link, sin USB ni ADB

Hay **3 paquetes** listos (regenerables con `python empaquetar_zora.py`),
dentro de `downloads/` al lado de `zora_backend.py`:

- `Zora_PC.zip` — backend + agente + web + lanzador para otra PC.
- `Zora_Celular.zip` — guía de instalación en Android/iPhone (PWA) y los
  archivos de la app por si algún día la publicas o usas PWABuilder.
- `Zora_TV.zip` — guía para smart TV / Android TV.

`http://tu-servidor:8000/descargas` los muestra listos para tocar y
descargar desde cualquier navegador — celular, PC, o TV. Aclaración
importante: esto no aparece solo "buscándolo en Google" (eso necesita que
Google indexe tu dominio, toma tiempo) — lo que sí funciona de inmediato
es mandar el link directo por WhatsApp/correo a cada familiar.

## 11. Modo estudio (nuevo) — Zora como compañero de estudio

Comandos por voz o texto (usados desde cualquier app, porque viven en el
backend):

- **Explicaciones simples**: `"explícame la fotosíntesis fácil"` → explicación
  corta, en español sencillo y con ejemplo cotidiano.
- **Resúmenes**: `"resume este texto: ..."` → resumen de máximo 6 líneas +
  3 ideas clave (acepta textos largos pegados).
- **Quizzes con corrección**: `"hazme un quiz de fracciones"` → 5 preguntas de
  opción múltiple generadas por el cerebro en la nube; respondes con la letra
  (`a`–`d`, también acepta `1`–`4`) y Zora corrige al instante con explicación.
  Al final da score (`📊 4/5 (80%)`). Puedes `"cancela el quiz"` cuando quieras.
  Cada sesión queda guardada en la tabla `quiz_sessions` de `zora.db`.
- **Flashcards persistentes**: `"crea flashcards de historia universal"`
  genera ~6 tarjetas y las guarda en la BD (`flashcard_decks`/`flashcards`);
  `"pruébame"` (o `"pruébame flashcards de historia"`) repasa tarjeta por
  tarjeta: ves el frente, respondes, Zora revela la respuesta y te pregunta si
  la sabías. Las tarjetas que más fallas vuelven primero en el próximo repaso.
  `"mis mazos"`, `"borra el mazo de historia"`, `"salir"` para cortar.

Las llamadas del modo estudio usan la misma cadena de respaldo que el cerebro
(Claude → OpenRouter → NVIDIA) pero SIN tocar tu historial de conversación.

## 12. Alarmas y recordatorios que SÍ avisan (arreglado)

Antes los timers disparaban en silencio (callbacks vacíos) y se perdían al
reiniciar el servidor. Ahora:

- Se guardan en SQLite (`tabla reminders`) — sobreviven reinicios.
- Un hilo revisa cada 2 segundos y, al vencer uno, lo marca, deja una alerta
  en el historial (`GET /alerts`, tipo `reminder`) y re-agenda el día
  siguiente si era diaria.
- El cliente consulta `GET /notifications` cada 8 segundos: recibe los
  recordatorios vencidos que aún NO le han sido entregados a ese usuario
  (entrega única garantizada por la tabla `reminder_deliveries`) y los muestra
  + los lee en voz alta.
- Frases nuevas: `"recuérdame llamar a mamá a las 18:00"` (guarda la tarea),
  `"qué alarmas tengo"`, `"cancela mis alarmas"`, `"timer 10 minutos"`.
  Si pides un recordatorio sin hora, Zora pregunta a qué hora.

## 13. Otras correcciones de esta versión

- **Lista de compras**: ya no imprime dicts internos; entiende
  "quita pan de la lista" (y sus variantes); "pon música rock" va a Spotify,
  NO a la lista; "pon X en la lista" sí va a la lista.
- **Calculadora**: un solo bloque sin duplicados; soporta `sqrt(16)`,
  `raiz(25)`, `15% de 230`.
- **Cotizaciones**: exchangerate.host ahora cobra key — cambiado a
  open.er-api.com (fiat, gratis sin key) + CoinGecko (cripto). Entiende
  nombres en español: `"100 dólares en euros"`, `"precio del bitcoin"`,
  `"cuánto vale ethereum"`.
- **Ruteo de comandos**: "cómo va el sistema" ya no se lo roba el módulo de
  deportes; "dime la hora" ya no cae en búsqueda web; los comandos rápidos
  se evalúan antes que las reglas genéricas.
- **OpenRouter** documentado en `zora.env`: es el 2° eslabón de la cadena
  Claude → OpenRouter → NVIDIA (pégalo con `OPENROUTER_API_KEY=...`).

## 14. Voces preinstaladas, interfaz nueva y OpenCode (nuevo)

- **Voces preinstaladas**: Zora ya no depende de las voces que traiga tu
  sistema. El backend genera su propia voz (`GET /tts?text=...&voice=...`,
  gratis y sin key, con caché en disco en `tts_cache/`). Presets:
  `clasica` (normal), `despacio` (para estudiar pronunciación) e
  `ingles` (voz en inglés para practicar listening). El cliente web las
  usa por defecto y, si el servicio fallara algún día, cae solo a las
  voces del navegador. Nota técnica honesta: se probó Pollinations audio
  (migró a API de pago), StreamElements/Polly (cerró el acceso libre) —
  la vía viva hoy es el TTS de Google Translate troceado por el backend.
- **Interfaz renovada estilo Gemini**: tema oscuro con acentos en
  gradiente azul-violeta-rosa, mensajes de Zora sin burbuja (con avatar),
  chips de sugerencias al entrar, composer flotante con brillo al enfocar,
  indicador "pensando" animado, pestañas con barra inferior translúcida.
  Mismo archivo de siempre (`static/index.html`) servido por el backend —
  las apps lo ven recargando, sin reinstalar nada.
- **OpenCode integrado**: dile `"opencode crea un script que ordene una
  lista"` o `"usa opencode para revisar este repositorio"` y la tarea viaja
  al agente de tu laptop (`zora_laptop_agent.py`), que corre
  `opencode run <tarea>` DE VERDAD y te trae la salida al chat. Requiere
  opencode instalado en esa laptop (`npm i -g opencode-ai`); opcionalmente
  define `ZORA_OPENCODE_BIN` (ruta exacta) y `ZORA_OPENCODE_TIMEOUT`
  (segundos). Sigue siendo lista blanca: el agente solo ejecuta esta acción,
  nunca comandos arbitrarios.

## 15. Temas a tu gusto y control de PC de verdad (nuevo)

- **Personalización**: panel 🎨 en el encabezado con 3 temas (Oscuro,
  Claro, Sepia), 5 colores de acento y 3 formas de burbujas. Cada usuario
  de la casa ve SU tema (se guarda por usuario, no por dispositivo).
- **Control real de la computadora**: botón **"+ PC"** en el encabezado
  genera un token de dispositivo; corres `zora_laptop_agent.py` en esa PC
  pegándolo, y desde cualquier app dices *"sube el volumen"*,
  *"abre spotify"*, *"bloquea la pantalla"* — el agente de Windows lo
  ejecuta DE VERDAD (volumen vía ctypes, bloqueo vía rundll32, apps vía
  startfile). Con una sola laptop conectada los comandos se enrutan solos;
  sin ninguna, Zora explica cómo conectarla en vez de fingir que hizo algo.
  El agente solo acepta acciones de una lista blanca: nunca ejecuta texto
  arbitrario.
- **Comandos rápidos sin tilde**: "pon musica rock" funciona igual que
  con tilde (la comparación ignora acentos).

## Qué queda pendiente, honestamente

- **Generación de video**: a diferencia de imagen y del cerebro de texto,
  no encontré ninguna API de video con tier gratis real — Sora, Runway y
  Veo cobran desde el primer clip. Si lo necesitas, tendría que ser con
  tu tarjeta.
- **Escuchar la clase completa por audio**: el modo estudio ya funciona por
  texto/voz corta (el `/transcribe` existe), pero grabar una clase entera
  (45+ min) puede exceder lo que acepta el transcriptor gratis. Se puede
  grabar por segmentos y luego pedir "resume este texto: ...". Dime si lo
  quieres como botón dedicado en la app.
- **SMS reales a los contactos**: YA está conectado textbee.dev en el backend
  (usa un Android tuyo como pasarela, 300 SMS/mes gratis) — solo falta que
  instales su app y pegues `TEXTBEE_API_KEY` en zora.env.
- **Deportes**: ya funciona con TheSportsDB ("cómo va el Barça"). Si la key
  pública compartida se satura algún día, pega tu propia key gratis en
  `THESPORTSDB_API_KEY`.
- **Base de datos en la nube**: YA soportada vía Turso (misma BD SQLite,
  hospedada, gratis) — pega `TURSO_DATABASE_URL` y `TURSO_AUTH_TOKEN` en
  zora.env si vas a hospedar el backend en Render.
- **Servidor accesible desde internet**: ver `DESPLIEGUE_INTERNET.md` —
  te dejo la guía paso a paso, pero el servidor en sí (una VPS de unos
  4-6 USD/mes) lo tienes que contratar tú; no puedo alojarlo yo.
- **Compilar los instalables finales** (.exe, .apk) y el proyecto de iOS:
  siguen necesitando que corras los comandos en tu propia PC (ver
  `zora-apps.zip` y `COMO_COMPILAR.md`) — este entorno no tiene internet
  ni los SDKs de compilación instalados.

## Cómo probar lo nuevo

```bash
# 1. Backend normal (con tus keys de zora.env):
python zora_backend.py

# 2. Pruebas automatizadas SIN gastar créditos reales (LLM simulado),
#    en otra terminal:
#    Windows PowerShell:
$env:PORT="8010"; $env:ZORA_DB_PATH="$env:TEMP\zora_test.db"; `
  $env:ZORA_FAKE_LLM="1"; $env:ANTHROPIC_API_KEY="PEGA-AQUI-X"; `
  $env:NVIDIA_API_KEY="PEGA-AQUI-X"; python zora_backend.py
#    y luego:
$env:ZORA_TEST_BASE="http://localhost:8010"; python test_estudio_alarmas.py
```

(38 verificaciones: calculadora, ruteo, lista de compras, quiz completo con
score 5/5, flashcards de punta a punta, timer que avisa por /notifications
con entrega única, recordatorios con/cancelación, cotización real USD→EUR.)

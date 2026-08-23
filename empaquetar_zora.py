"""Empaqueta Zora en 3 ZIPs descargables dentro de downloads/:

  - Zora_PC.zip       -> backend completo para Windows/Mac/Linux.
  - Zora_Celular.zip  -> guía de instalación en Android/iPhone (PWA) +
                         archivos de la app para quien quiera publicarla
                         (PWABuilder) sin depender del servidor.
  - Zora_TV.zip       -> guía para smart TV / Android TV.

El ZIP de PC incluye una PLANTILLA de zora.env con placeholders — JAMAS la
zora.env real de esta maquina (ahi viven tus API keys).
"""
import os
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "downloads")

ARCHIVOS = [
    "zora_backend.py",
    "zora_laptop_agent.py",
    "zora_voice_connector.py",
    "README.md",
    "DESPLIEGUE_INTERNET.md",
    "iniciar_zora_windows.bat",
]

STATIC = ["index.html", "manifest.webmanifest", "service-worker.js",
          "icon-192.png", "icon-512.png"]

LEEME = """\
ZORA - ASISTENTE FAMILIAR  |  LEEME PRIMERO
============================================

QUE ES ESTO
  Un asistente personal que corre EN TU PROPIA COMPUTADORA: chat con voz,
  modo estudio (quizzes, flashcards), alarmas reales, clima, deportes,
  imagenes gratis, SOS familiar y control real de tu PC.

REQUISITOS
  - Windows 10/11 con Python 3.10 o mas nuevo (gratis en python.org;
    al instalar marca "Add python.exe to PATH").
  - NO hace falta instalar ninguna libreria: todo usa lo incluido en Python.

COMO ARRANCAR (30 segundos)
  1. Descomprime este ZIP en una carpeta cualquiera.
  2. Doble clic a  iniciar_zora_windows.bat
  3. Se abre tu navegador en http://localhost:8000
     Crea tu usuario y listo: ya estas hablando con Zora.

EN EL CELULAR (sin APK, es una app instalable)
  1. Conecta el celular al MISMO WiFi de tu PC.
  2. Averigua la IP de tu PC (cmd -> ipconfig -> IPv4, ej. 192.168.1.20).
  3. En Chrome del celular abre  http://ESA-IP:8000
  4. Menu de Chrome -> "Agregar a pantalla de inicio" / "Instalar aplicacion".
     Queda como una app mas: icono propio, pantalla completa. Lo que hagas
     alli se guarda en el mismo servidor de tu casa (mismos datos).

EN EL TV
  Si tu smart TV tiene navegador, abre la misma direccion del paso anterior.
  Para Android TV como app nativa ver el final de este archivo.

CONTROLAR TU PC CON LA VOZ ("sube el volumen", "abre spotify"...)
  1. En la web de Zora pulsa el boton "+ PC" del encabezado: te da un token.
  2. En esa misma carpeta abre una terminal (cmd) y corre:
       set ZORA_BACKEND_URL=http://localhost:8000
       set ZORA_DEVICE_TOKEN=el-token-que-te-dieron
       python zora_laptop_agent.py
  3. Dejalo corriendo minimizado. Ahora pidele a Zora cosas de verdad.

HACERLA MAS LISTA (opcional, gratis)
  Abre el archivo zora.env con el Bloc de notas y pega tus llaves:
  - NVIDIA_API_KEY   (GRATIS: build.nvidia.com -> key "nvapi-...")
  - OPENROUTER_API_KEY (openrouter.ai/keys) y ANTHROPIC_API_KEY si tienes.
  Sin ninguna llave Zora igual funciona: responde con su cerebro simulado,
  comandos rapidos, clima, monedas, alarmas y estudio basico.

DONDE SE GUARDAN MIS DATOS
  En tu usuario de Windows: %LOCALAPPDATA%\\Zora  (base de datos, cache).
  Borrar esa carpeta = empezar de cero.

APK PARA ANDROID TV / CELULAR (opcional, avanzado)
  La web ya es instalable como app (PWA). Si quieres un APK clasico,
  PWABuilder.com convierte tu PWA publicada en internet a APK firmado sin
  instalar nada local; mira DESPLIEGUE_INTERNET.md para publicar Zora.

PROBLEMAS COMUNES
  - "Puerto ocupado": cierra otra copia de Zora, o define otro puerto con
    set PORT=8010 antes de arrancar.
  - El celular no conecta: revisa mismo WiFi y permite Python en el Firewall
    de Windows (red privada).
"""

ZORA_ENV_TEMPLATE = """\
# ============================================================
#  zora.env - TODAS las API keys de Zora, en un solo lugar
# ============================================================
#  Reemplaza cada PEGA-AQUI por tu llave real. Las lineas que
#  queden con PEGA-AQUI simplemente se tratan como vacias.
#  NUNCA compartas este archivo con keys reales dentro.

# --- Cerebro principal (opcional, de pago) ---
ANTHROPIC_API_KEY=PEGA-AQUI-TU-KEY-DE-CLAUDE

# --- Cerebro alternativo (opcional, de pago por uso) ---
OPENROUTER_API_KEY=PEGA-AQUI-TU-KEY-DE-OPENROUTER
# OPENROUTER_MODEL=anthropic/claude-3.5-sonnet

# --- Cerebro GRATIS de respaldo (recomendado) ---
# 1) https://build.nvidia.com -> crea cuenta gratis
# 2) abre cualquier modelo y saca tu key (empieza con nvapi-)
NVIDIA_API_KEY=PEGA-AQUI-TU-NVAPI

# --- SMS reales para SOS (opcional, gratis) ---
# textbee.dev -> genera tu key
TEXTBEE_API_KEY=PEGA-AQUI

# --- Deportes (opcional): https://www.thesportsdb.com/api.php ---
THESPORTSDB_API_KEY=PEGA-AQUI-3 (la clave gratuita "3" sirve)

# --- Correo para SOS (opcional) ---
ZORA_SMTP_HOST=
ZORA_SMTP_PORT=587
ZORA_SMTP_USER=
ZORA_SMTP_PASS=
ZORA_SMTP_FROM=
"""


LEEME_CELULAR = """\
ZORA EN TU CELULAR  |  LEEME PRIMERO (Android y iPhone)
========================================================

QUE ES
  La MISMA app de Zora que corre en la PC, instalada en tu celular como
  una aplicacion mas: icono propio, pantalla completa, funciona con voz.
  No ocupa casi nada y siempre muestra la version mas nueva (se actualiza
  sola al reconectar con el servidor de la casa).

REQUISITO UNICO
  Que la PC donde corre Zora este encendida con el servidor andando
  (doble clic a iniciar_zora_windows.bat en la PC).

ANDROID — INSTALAR (2 minutos)
  1. Conecta el celular al MISMO WiFi de la PC.
  2. En la PC: abre cmd y escribe ipconfig; anota la IPv4 (ej. 192.168.1.20).
  3. En Chrome del celular abre  http://ESA-IP:8000
  4. Menu de Chrome (los 3 puntos) -> "Agregar a pantalla de inicio" o
     "Instalar aplicacion". Listo: queda como app con su icono rosa.

IPHONE / IPAD — INSTALAR
  1. Mismo WiFi y misma direccion http://ESA-IP:8000 en Safari.
  2. Boton Compartir (el cuadrito con flecha) -> "Agregar a inicio".
  3. Se abre a pantalla completa como app.

MICROFONO Y VOZ
  La primera vez que toques el microfono, el navegador pedira permiso:
  acepta. Zora te escucha, transcribe y te responde HABLANDO.

USARLA FUERA DE CASA (opcional)
  Dentro de tu WiFi funciona tal cual. Para usarla desde la calle hace
  falta publicar el servidor en internet: ver DESPLIEGUE_INTERNET.md.

AVANZADO: APK CLASICO
  Si algun dia quieres un APK de verdad, PWABuilder.com convierte esta
  misma app (publicada en internet) en APK firmado, sin instalar nada.
"""

LEEME_TV = """\
ZORA EN TU TV  |  LEEME PRIMERO (Smart TV y Android TV)
========================================================

QUE ES
  Zora a pantalla de sala: la misma app, hecha para control por voz
  desde el sofa. Ideal para clima, musica, deportes y recordatorios
  familiares en pantalla grande.

SMART TV CON NAVEGADOR (LG, Samsung, etc.)
  1. PC encendida con el servidor corriendo (iniciar_zora_windows.bat).
  2. Misma WiFi que la PC. Abre el navegador de la TV.
  3. Escribe http://LA-IP-DE-TU-PC:8000 (la IPv4 sale de ipconfig en la PC).
  4. Inicia sesion o crea usuario CON EL CONTROL REMOTO (teclado en pantalla).

ANDROID TV (Chromecast, Xiaomi, TCL...)
  1. Instala un navegador si no hay: "TV Bro" o Chrome desde Play Store.
  2. Abre http://LA-IP-DE-TU-PC:8000 y entra.
  3. Opcional: en Google Play busca "Launcher para PWA" o crea acceso
     directo desde el navegador para dejarlo como canal propio.

CONTROL POR VOZ EN LA TV
  Muchos controles remotos tienen microfono (Google Assistant): abre la
  app de Zora y usa el boton de microfono de la propia pagina.

APK PARA ANDROID TV (avanzado, opcional)
  PWABuilder.com genera un APK de Android TV a partir de la app publicada
  en internet (ver DESPLIEGUE_INTERNET.md). Los archivos de esta app estan
  incluidos en la carpeta static/ de este ZIP por si los necesitas subir
  a tu propio hosting.

NOTA HONESTA
  El APK no viene en este ZIP porque compilarlo exige el kit de Android
  (SDK) instalado en una PC; con la web instalable ya tienes lo mismo sin
  instalar nada.
"""


def _zip(path, entries):
    """entries: lista de (nombre_en_zip, valor) donde valor es:
    - str  -> ruta de un archivo en disco
    - ("text", contenido) -> texto literal a incrustar"""
    if os.path.isfile(path):
        os.remove(path)
    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, src in entries:
            if isinstance(src, tuple):         # texto literal
                z.writestr(name, src[1])
            elif os.path.isfile(src):          # archivo individual
                z.write(src, name)
            else:                              # carpeta -> se copia completa
                for root, _, files in os.walk(src):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, os.path.dirname(src))
                        z.write(full, rel)
                        n += 1
                continue
            n += 1
    mb = os.path.getsize(path) / (1024 * 1024)
    print(f"Listo: {path}  ({n} archivos, {mb:.1f} MB)")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # --- 1) ZIP de PC (backend + agente + web + lanzador) ---
    pc_entries = [(name, os.path.join(BASE, name)) for name in ARCHIVOS]
    pc_entries += [(f"static/{name}", os.path.join(BASE, "static", name))
                   for name in STATIC]
    pc_entries += [("LEEME_PRIMERO.txt", ("text", LEEME)),
                   ("zora.env", ("text", ZORA_ENV_TEMPLATE))]
    _zip(os.path.join(OUT_DIR, "Zora_PC.zip"), pc_entries)

    # --- 2) ZIP de celular (guia + app web completa) ---
    cel_entries = [("LEEME_CELULAR.txt", ("text", LEEME_CELULAR))]
    cel_entries += [(f"app/{name}", os.path.join(BASE, "static", name))
                    for name in STATIC]
    cel_entries += [("DESPLIEGUE_INTERNET.txt",
                     os.path.join(BASE, "DESPLIEGUE_INTERNET.md"))]
    _zip(os.path.join(OUT_DIR, "Zora_Celular.zip"), cel_entries)

    # --- 3) ZIP de TV (guia + app web completa) ---
    tv_entries = [("LEEME_TV.txt", ("text", LEEME_TV))]
    tv_entries += [(f"app/{name}", os.path.join(BASE, "static", name))
                   for name in STATIC]
    _zip(os.path.join(OUT_DIR, "Zora_TV.zip"), tv_entries)


if __name__ == "__main__":
    main()

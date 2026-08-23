"""
ZORA LAPTOP AGENT - El "brazo ejecutor" en tu laptop
=======================================================

Este programa corre en segundo plano en tu laptop (NO en el celular) y:
1. Se conecta al backend de Zora usando su propio DEVICE TOKEN (no tu
   contraseña de usuario).
2. Pregunta periódicamente "¿tienes algo para mí?" (poll) — así la laptop
   siempre INICIA la conexión hacia el backend, y no necesitas abrir
   puertos ni configurar el router de tu casa.
3. Cuando llega un comando, lo ejecuta usando una lista blanca de acciones
   permitidas (nunca ejecuta código arbitrario que llegue del backend —
   esto es intencional, por seguridad: ver ALLOWED_ACTIONS más abajo).
4. Reporta el resultado de vuelta al backend.

Cómo usarlo
-----------
1. Primero registra la laptop (una sola vez) usando el backend:
     POST /devices  {"name": "Laptop de papá", "type": "laptop"}
   Eso te da un "device_token" — cópialo.

2. Corre el agente:
     export ZORA_BACKEND_URL="http://localhost:8000"
     export ZORA_DEVICE_TOKEN="el-token-que-te-dieron"
     python3 zora_laptop_agent.py

El agente queda corriendo, revisando cada pocos segundos si hay comandos
pendientes para esta laptop.

Nota sobre las acciones reales
------------------------------
Las acciones están implementadas DE VERDAD para Windows (el sistema donde
se usa Zora hoy): subir/bajar volumen con la API de Windows (ctypes, sin
dependencias), abrir apps con shutil.which + os.startfile, bloquear la
pantalla con rundll32 y reproducir música abriendo el URI spotify:search.
En macOS hay respaldo parcial (open/pmset) y en Linux algunas quedan como
demo. La estructura (poll -> ejecutar -> reportar) no cambia entre
sistemas operativos — solo el CONTENIDO de cada acción.
"""

import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.request
import urllib.error
import urllib.parse


BACKEND_URL = os.environ.get("ZORA_BACKEND_URL", "http://localhost:8000")
DEVICE_TOKEN = os.environ.get("ZORA_DEVICE_TOKEN")
POLL_INTERVAL_SECONDS = 3

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def _call(method: str, path: str, body: dict = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BACKEND_URL + path, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# =========================================================================
# LISTA BLANCA DE ACCIONES PERMITIDAS
# =========================================================================
# El agente NUNCA ejecuta texto/código arbitrario que le llegue del backend.
# Solo sabe hacer estas acciones específicas, cada una con su propia función
# — así, aunque alguien comprometa el backend, lo máximo que puede hacer es
# invocar una de estas acciones ya definidas, nunca correr comandos nuevos.

def action_get_status(params: dict) -> dict:
    """Info básica de la laptop — útil para confirmar que el agente vive."""
    return {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "release": platform.release(),
        "timestamp": time.time(),
    }


def action_volume_up(params: dict) -> dict:
    """Sube el volumen DE VERDAD en Windows (tecla virtual de volumen,
    4 pulsaciones = 8% aprox.) vía la API de Windows con ctypes — sin
    dependencias externas. En macOS/Linux queda simulado por ahora."""
    if IS_WINDOWS:
        VK_VOLUME_UP = 0xAF
        for _ in range(4):
            ctypes.windll.user32.keybd_event(VK_VOLUME_UP, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_VOLUME_UP, 0, 2, 0)  # KEYEVENTF_KEYUP
            time.sleep(0.05)
        return {"message": "Volumen subido en tu Windows."}
    return {"simulated": True, "message": "Volumen subido (simulado: aún no implemento esta acción en este sistema)"}


def action_volume_down(params: dict) -> dict:
    """Baja el volumen de verdad en Windows (misma técnica que subir)."""
    if IS_WINDOWS:
        VK_VOLUME_DOWN = 0xAE
        for _ in range(4):
            ctypes.windll.user32.keybd_event(VK_VOLUME_DOWN, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_VOLUME_DOWN, 0, 2, 0)
            time.sleep(0.05)
        return {"message": "Volumen bajado en tu Windows."}
    return {"simulated": True, "message": "Volumen bajado (simulado)"}


def action_open_app(params: dict) -> dict:
    """
    Abre una aplicación de verdad: la busca en el PATH (shutil.which funciona
    igual en Windows/macOS/Linux — antes usaba `which`, que solo existe en
    Linux y aquí revientaba) y la lanza. Apps de Windows como notepad/calc/
    explorer resuelven solas porque viven en el PATH del sistema.
    """
    app_name = params.get("param", "").strip() or params.get("app", "")
    if not app_name:
        return {"error": "no se especificó qué aplicación abrir"}
    exe = shutil.which(app_name)
    try:
        if exe:
            subprocess.Popen([exe])
            return {"app": app_name, "message": f"Abrí '{app_name}' en tu computadora."}
        if IS_WINDOWS:
            os.startfile(app_name)  # nombres registrados: "spotify:", apps UWP, etc.
            return {"app": app_name, "message": f"Abrí '{app_name}' en tu computadora."}
        return {"error": f"No encontré '{app_name}' en esta computadora.",
                "found_in_path": False}
    except Exception as e:  # noqa: BLE001
        return {"error": f"No pude abrir '{app_name}': {e}"}


def action_lock_screen(params: dict) -> dict:
    """Bloquea la pantalla DE VERDAD: Windows via rundll32 (oficial),
    macOS via pmset; en Linux queda simulado."""
    if IS_WINDOWS:
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
        return {"message": "Pantalla bloqueada. ¡Hasta luego!"}
    if IS_MAC:
        subprocess.run(["pmset", "displaysleepnow"], check=False)
        return {"message": "Pantalla bloqueada."}
    return {"simulated": True, "message": "Pantalla bloqueada (simulado)"}


def action_spotify_play(params: dict) -> dict:
    """Reproduce música DE VERDAD si Spotify está instalado: abre el URI
    spotify:search:<query> — Windows lo lanza con os.startfile."""
    query = params.get("param", "")
    if not query:
        return {"error": "dime qué música reproducir"}
    if IS_WINDOWS:
        try:
            os.startfile("spotify:search:" + urllib.parse.quote(query))
            return {"message": f"Buscando y reproduciendo '{query}' en Spotify."}
        except Exception:
            return {"error": "No encontré Spotify instalado en esta computadora."}
    if IS_MAC:
        subprocess.Popen(["open", "spotify:search:" + query])
        return {"message": f"Buscando '{query}' en Spotify."}
    return {"simulated": True, "message": f"(demo) Buscaría y reproduciría: '{query}'"}


def action_raw_text(params: dict) -> dict:
    """
    Para comandos que no fueron reconocidos como 'rápidos' — en un sistema
    real, aquí el agente podría reenviar el texto a un modelo local o al
    backend para que decida qué hacer con más contexto. Por ahora, solo
    lo reporta de vuelta.
    """
    return {"received_text": params.get("text", ""), "message": "Comando libre recibido"}


def action_opencode_run(params: dict) -> dict:
    """
    Corre 'opencode run <tarea>' DE VERDAD en esta laptop y devuelve la
    salida. Es la única acción que ejecuta un agente externo — está aquí
    porque es SU propósito (igual que las demás, solo acepta esta acción
    exacta; nunca texto arbitrario a shell).
    Binario: usa ZORA_OPENCODE_BIN si está definida, o busca 'opencode'
    en el PATH. Timeout configurable con ZORA_OPENCODE_TIMEOUT (segundos).
    """
    task = (params.get("param") or params.get("text") or "").strip()
    if not task:
        return {"error": "no se especificó la tarea para opencode"}

    exe = os.environ.get("ZORA_OPENCODE_BIN") or shutil.which("opencode")
    if not exe:
        return {"error": "opencode no está instalado o no está en el PATH de esta laptop "
                          "(instálalo con: npm i -g opencode-ai)"}

    timeout = int(os.environ.get("ZORA_OPENCODE_TIMEOUT", "600"))
    try:
        proc = subprocess.run(
            [exe, "run", task],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        truncated = len(out) > 4000
        return {
            "exit_code": proc.returncode,
            "output": (out[-4000:] if truncated else out) or err[-2000:],
            "truncated": truncated,
            "message": f"opencode terminó (código {proc.returncode}).",
        }
    except subprocess.TimeoutExpired:
        return {"error": f"opencode tardó más de {timeout}s y fue cancelado"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"fallo ejecutando opencode: {e}"}


ALLOWED_ACTIONS = {
    "get_status": action_get_status,
    "volume_up": action_volume_up,
    "volume_down": action_volume_down,
    "open_app": action_open_app,
    "spotify_play": action_spotify_play,
    "lock_screen": action_lock_screen,
    "raw_text": action_raw_text,
    "opencode_run": action_opencode_run,
}


def execute(command: dict) -> dict:
    action = command.get("action")
    handler = ALLOWED_ACTIONS.get(action)
    if not handler:
        return {"error": f"acción '{action}' no está en la lista blanca del agente"}
    try:
        return handler(command.get("params", {}))
    except Exception as e:  # noqa: BLE001 (prototipo)
        return {"error": f"fallo ejecutando '{action}': {e}"}


def main_loop():
    if not DEVICE_TOKEN:
        raise SystemExit(
            "Falta ZORA_DEVICE_TOKEN. Regístra la laptop primero con "
            "POST /devices y exporta el token que te devuelva."
        )

    print(f"Agente Zora iniciado — conectando a {BACKEND_URL}")
    print(f"Sistema: {platform.system()} {platform.release()} — Host: {socket.gethostname()}")

    while True:
        try:
            resp = _call("GET", f"/agent/poll?device_token={DEVICE_TOKEN}")
            command = resp.get("command")
            if command:
                print(f"→ Comando recibido: {command['action']} ({command['command_id']})")
                result = execute(command)
                print(f"  resultado: {result}")
                _call("POST", "/agent/report", {
                    "device_token": DEVICE_TOKEN,
                    "command_id": command["command_id"],
                    "result": result,
                })
        except urllib.error.URLError as e:
            print(f"(sin conexión al backend, reintentando... {e})")
        except Exception as e:  # noqa: BLE001 (prototipo)
            print(f"(error en el loop del agente: {e})")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()

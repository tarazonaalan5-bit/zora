"""
ZORA VOICE CONNECTOR - Convierte tu voz en un comando para Zora, y la
respuesta de Zora de vuelta en voz (femenina, acorde al nombre)
=====================================================================

Este script toma un archivo de audio (una grabación de comando de voz) y:
1. Lo convierte a texto (reconocimiento de voz / speech-to-text).
2. Manda ese texto al backend de Zora (`/command`), igual que si lo
   hubieras escrito.
3. Toma la respuesta y la convierte de vuelta a audio (texto-a-voz), con
   una voz de mujer, y la reproduce/guarda.

Tres formas de hacer la transcripción (voz -> texto), en orden de
preferencia
-----------------------------------------------------------------
1. **Whisper local** (gratis, corre en tu máquina, sin internet):
   si tienes instalado `openai-whisper` o `faster-whisper`, este script
   los usa automáticamente. Instálalo con:
       pip install openai-whisper --break-system-packages
   (la primera vez descarga el modelo, sí necesita internet esa vez).

2. **API de transcripción en la nube** (si tienes internet y una API key):
   si defines OPENAI_API_KEY, usa la API de Whisper de OpenAI por HTTP,
   sin necesitar instalar nada pesado localmente.

3. **Modo DEMO** (sin whisper instalado y sin API key): en vez de
   transcribir audio de verdad, busca un archivo de texto con el mismo
   nombre que el audio (ej. `comando.wav` -> `comando.txt`) y usa ese
   contenido como si fuera la transcripción. Esto permite probar TODO el
   pipeline (voz -> texto -> backend -> respuesta) sin depender de audio
   real ni de internet — es exactamente lo que usamos para probarlo aquí.

Tres formas de generar la voz de Zora (texto -> voz), mismo orden
-----------------------------------------------------------------
1. **pyttsx3 local** (gratis, offline): si está instalado, busca entre las
   voces del sistema una femenina (por nombre/metadata, ej. "Helena",
   "Zira", "Mónica", "Paulina", o variante "female"/"f3" de espeak) y la
   usa. Instálalo con:
       pip install pyttsx3 --break-system-packages
2. **API de OpenAI TTS** (si defines OPENAI_API_KEY): usa el modelo
   `tts-1` con la voz `nova` (voz femenina), genera un .mp3.
3. **Modo DEMO** (sin pyttsx3 y sin API key, o sin salida de audio en el
   sistema — como este sandbox sin entorno gráfico/audio): en vez de
   sintetizar audio de verdad, guarda el texto que Zora "diría" en un
   .txt junto con una nota de qué voz se habría usado.

Uso
---
    export ZORA_BACKEND_URL="http://localhost:8000"
    export ZORA_USER_TOKEN="tu-token-de-login"
    python3 zora_voice_connector.py comando.wav [device_id opcional]
"""

import json
import os
import sys
import urllib.request
import urllib.error


BACKEND_URL = os.environ.get("ZORA_BACKEND_URL", "http://localhost:8000")
USER_TOKEN = os.environ.get("ZORA_USER_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Nombres de voces femeninas comunes en distintos sistemas/motores, usados
# para elegir automáticamente una voz de mujer entre las disponibles.
FEMALE_VOICE_HINTS = [
    "female", "mujer", "femenina",
    "zira", "helena", "hazel",              # Windows (SAPI5)
    "monica", "mónica", "paulina", "samantha", "victoria",  # macOS
    "f1", "f2", "f3", "f4", "f5",           # variantes de espeak
]


# =========================================================================
# PASO 1: Audio -> Texto
# =========================================================================

def transcribe_local_whisper(audio_path: str) -> str:
    """Intenta usar openai-whisper instalado localmente."""
    import whisper  # ImportError si no está instalado -> se captura afuera
    model = whisper.load_model("base")
    result = model.transcribe(audio_path, language="es")
    return result["text"].strip()


def transcribe_openai_api(audio_path: str) -> str:
    """Usa la API de Whisper de OpenAI por HTTP (necesita internet + key)."""
    import mimetypes
    boundary = "----ZoraBoundary"
    filename = os.path.basename(audio_path)
    mime_type = mimetypes.guess_type(audio_path)[0] or "audio/wav"

    with open(audio_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_data + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
        f"--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["text"].strip()


def transcribe_demo_fallback(audio_path: str) -> str:
    """
    MODO DEMO: sin whisper ni API key, lee un .txt con el mismo nombre
    base que el audio (simulando "lo que se transcribiría"). Sirve para
    probar el resto del pipeline sin depender de audio real.
    """
    txt_path = os.path.splitext(audio_path)[0] + ".txt"
    if not os.path.exists(txt_path):
        raise FileNotFoundError(
            f"Modo demo: no encontré '{txt_path}' con el texto simulado. "
            f"Crea ese archivo con el comando que 'dirías', o instala "
            f"whisper / define OPENAI_API_KEY para transcripción real."
        )
    with open(txt_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def transcribe(audio_path: str) -> tuple[str, str]:
    """Devuelve (texto_transcrito, método_usado)."""
    try:
        return transcribe_local_whisper(audio_path), "whisper local"
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"(whisper local falló: {e}, probando siguiente método)")

    if OPENAI_API_KEY:
        try:
            return transcribe_openai_api(audio_path), "OpenAI Whisper API"
        except Exception as e:  # noqa: BLE001
            print(f"(API de OpenAI falló: {e}, probando modo demo)")

    return transcribe_demo_fallback(audio_path), "DEMO (archivo .txt simulado)"


# =========================================================================
# PASO 2: Texto -> Comando al backend
# =========================================================================

def send_command(text: str, device_id: str = None) -> dict:
    if not USER_TOKEN:
        raise SystemExit("Falta ZORA_USER_TOKEN (inicia sesión primero y exporta el token)")
    body = {"text": text}
    if device_id:
        body["device_id"] = device_id
    req = urllib.request.Request(
        BACKEND_URL + "/command",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {USER_TOKEN}"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# =========================================================================
# PASO 3: Respuesta -> Texto para hablar
# =========================================================================

def extract_speakable_text(response: dict) -> str:
    """
    Saca de la respuesta del backend el texto que Zora debería 'decir'.
    Cubre los 3 tipos de respuesta que puede mandar /command:
      - cloud_brain: {"result": {"response_text": "..."}}
      - quick_command: acción reconocida (ej. volumen, linterna)
      - queued_for_agent: se mandó a la laptop, aún no hay resultado
    """
    rtype = response.get("type")
    if rtype == "cloud_brain":
        return response.get("result", {}).get("response_text", "")
    if rtype == "quick_command":
        action = response.get("result", {}).get("action", "")
        return f"Listo, hecho: {action.replace('_', ' ')}."
    if rtype == "queued_for_agent":
        return "Ya le mandé la orden a tu laptop, en un momento la ejecuta."
    return response.get("error", "No entendí bien qué pasó, ¿puedes repetir?")


# =========================================================================
# PASO 4: Texto -> Voz (con voz de mujer)
# =========================================================================

def _pick_female_voice(voices):
    """De una lista de voces (pyttsx3), intenta encontrar una femenina."""
    for v in voices:
        haystack = f"{getattr(v, 'name', '')} {getattr(v, 'id', '')}".lower()
        if any(hint in haystack for hint in FEMALE_VOICE_HINTS):
            return v
    # pyttsx3 a veces expone v.gender directamente
    for v in voices:
        if getattr(v, "gender", None) == "female":
            return v
    return None


def speak_local_pyttsx3(text: str, out_path: str) -> str:
    """Intenta usar pyttsx3 (offline). Si hay salida de audio real, la
    reproduce directo; si no (como en este sandbox sin audio), igual
    guarda el .mp3/.wav si el motor lo permite."""
    import pyttsx3  # ImportError si no está instalado -> se captura afuera

    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    chosen = _pick_female_voice(voices)
    if chosen:
        engine.setProperty("voice", chosen.id)
    else:
        # Ninguna voz femenina detectada por nombre: en espeak, forzamos
        # la variante femenina "+f3" sobre la voz en español si existe.
        for v in voices:
            if "es" in getattr(v, "id", "").lower():
                engine.setProperty("voice", v.id + "+f3")
                break

    engine.save_to_file(text, out_path)
    engine.runAndWait()
    return out_path


def speak_openai_api(text: str, out_path: str) -> str:
    """Usa la API TTS de OpenAI con la voz 'nova' (femenina)."""
    body = json.dumps({
        "model": "tts-1",
        "voice": "nova",
        "input": text,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        with open(out_path, "wb") as f:
            f.write(resp.read())
    return out_path


def speak_demo_fallback(text: str, out_path: str) -> str:
    """
    MODO DEMO: sin pyttsx3 ni API key (o sin audio real disponible),
    guarda el texto que Zora 'diría' en un .txt, simulando la voz.
    """
    txt_path = os.path.splitext(out_path)[0] + "_respuesta.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(
            "[Zora, voz de mujer — modo demo, sin motor de audio disponible]\n"
            + text
        )
    return txt_path


def speak(text: str, base_name: str = "zora_respuesta") -> tuple[str, str]:
    """Devuelve (ruta_del_archivo_generado, método_usado)."""
    if not text:
        text = "No tengo nada que decir."

    try:
        path = speak_local_pyttsx3(text, base_name + ".wav")
        return path, "pyttsx3 local (voz femenina)"
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"(pyttsx3 local falló: {e}, probando siguiente método)")

    if OPENAI_API_KEY:
        try:
            path = speak_openai_api(text, base_name + ".mp3")
            return path, "OpenAI TTS API (voz 'nova', femenina)"
        except Exception as e:  # noqa: BLE001
            print(f"(API de OpenAI TTS falló: {e}, probando modo demo)")

    path = speak_demo_fallback(text, base_name + ".wav")
    return path, "DEMO (texto guardado, sin síntesis real)"


# =========================================================================
# MAIN
# =========================================================================

def main():
    if len(sys.argv) < 2:
        print("Uso: python3 zora_voice_connector.py <archivo_audio> [device_id]")
        sys.exit(1)

    audio_path = sys.argv[1]
    device_id = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"🎙️  Transcribiendo {audio_path}...")
    text, method = transcribe(audio_path)
    print(f"📝 Texto ({method}): \"{text}\"")

    print("📡 Mandando al backend de Zora...")
    response = send_command(text, device_id)
    print(f"✅ Respuesta: {json.dumps(response, ensure_ascii=False, indent=2)}")

    speakable = extract_speakable_text(response)
    print(f"🗣️  Zora va a decir: \"{speakable}\"")
    audio_out, tts_method = speak(speakable)
    print(f"🔊 Voz generada ({tts_method}): {audio_out}")


if __name__ == "__main__":
    main()

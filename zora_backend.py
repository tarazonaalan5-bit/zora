"""
ZORA BACKEND - El "cerebro" del asistente
=============================================

Este es el servidor central que:
1. Recibe comandos de voz/texto (ya convertidos a texto) desde cualquier
   dispositivo (celular, laptop, TV).
2. Decide si es un comando RAPIDO (ejecución local/directa, sin IA) o
   COMPLEJO (necesita el modelo de lenguaje en la nube).
3. Maneja usuarios, sesiones (tokens) y dispositivos registrados.
4. Aplica las reglas de permisos: TV = compartida, laptop/celular = solo el dueño.
5. Guarda todo en una base de datos SQLite real (zora.db) — ya no se pierde
   nada al reiniciar el servidor.
6. Maneja geocercas (avisa si un dispositivo entra/sale de una zona) y
   emergencias/SOS (notifica a los contactos de confianza).
7. Guarda actividad física simple (pasos/distancia) por dispositivo.

Usa SOLO la librería estándar de Python (no necesita pip install nada),
así que puedes correrlo con: python3 zora_backend.py

Para conectarlo a un modelo de IA real (Claude), define la variable de
entorno ANTHROPIC_API_KEY antes de correrlo:
    export ANTHROPIC_API_KEY="tu-api-key-aqui"
    python3 zora_backend.py

Si no tienes ANTHROPIC_API_KEY (o se te acaba el saldo), Zora cae
automáticamente a un cerebro de respaldo GRATIS con NVIDIA Build
(sin tarjeta de crédito, se saca la key en https://build.nvidia.com):
    export NVIDIA_API_KEY="nvapi-..."

Para generación de imágenes reales por voz/texto ("hazme una imagen de..."),
Zora usa Pollinations.ai — genuinamente gratis, sin key, sin cuenta y sin
tarjeta. No hace falta configurar nada para que funcione.

El cerebro de respaldo (NVIDIA) sí tiene un límite de usos gratis por mes.
Zora lleva la cuenta sola y, cuando se acaba, avisa con un mensaje claro
(en vez de fallar feo) diciendo cuándo se recarga — no hace falta que
hagas nada.

Para que las alertas de SOS manden un correo real a los contactos de
confianza (no solo quedar guardadas en el backend), define además:
    export ZORA_SMTP_HOST="smtp.gmail.com"
    export ZORA_SMTP_PORT="587"
    export ZORA_SMTP_USER="tu_correo@gmail.com"
    export ZORA_SMTP_PASS="tu-contraseña-de-aplicación"
Sin esas variables, el backend sigue funcionando: la alerta se guarda y se
puede consultar por API, pero no se manda ningún correo de verdad (modo
demo, se imprime en consola qué se habría mandado).
"""

import json
import base64
import ast
import os
import sys
import platform
import re
import unicodedata
import secrets
import hashlib
import time
import math
import smtplib
import socket
import sqlite3
import threading
import urllib.request
import urllib.error
import urllib.parse
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows y consolas mal configuradas usan cp1252/charmap, que NO puede
# imprimir emojis (🚨 del SOS, 🃏 de flashcards...) — ese UnicodeEncodeError
# subía hasta romper el endpoint completo (el SOS devolvía 400). Forzamos
# UTF-8 con reemplazo en la salida para que ningún print tumbe una petición.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — si no se puede, seguimos igual
        pass


# =========================================================================
# 0. TODAS LAS API KEYS EN UN SOLO LUGAR (zora.env)
# =========================================================================
# En vez de tener que editar el código o el launcher cada vez que agregas
# una API nueva, TODAS las keys (las de hoy y las que agreguemos después:
# SMS, deportes, lo que sea) van en un solo archivo de texto: zora.env,
# junto a este mismo zora_backend.py. Es el ÚNICO lugar donde las pegas.
#
# Formato (una por línea, sin comillas):
#   NVIDIA_API_KEY=nvapi-tu-key-aqui
#
# (Las imágenes ya no necesitan key — usan Pollinations.ai, gratis y
# abierto.)
#
# Si el archivo no existe, Zora sigue funcionando igual — solo que sin las
# funciones que necesitan esas keys (avisa con un mensaje, no se rompe).
# Si una key YA está puesta como variable de entorno real del sistema
# (export / set), esa tiene prioridad sobre lo que diga zora.env.

def load_local_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zora.env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:  # el entorno real del sistema manda si ya existe
                os.environ[key] = value


load_local_env()


# =========================================================================
# 1. BASE DE DATOS (SQLite real — persiste entre reinicios del servidor)
# =========================================================================
# Sesiones y colas de comandos siguen en memoria a propósito: son datos de
# corta duración (una sesión dura horas, una cola de comandos se vacía en
# segundos) y no pasa nada si se pierden al reiniciar. Todo lo que sí debe
# sobrevivir a un reinicio (usuarios, dispositivos, contactos, geocercas,
# historial de emergencias, actividad) vive en zora.db.

def _default_data_dir() -> str:
    """
    Carpeta fija y persistente para los datos de Zora (base de datos e
    imágenes generadas), separada por completo de la carpeta del código.
    Así, actualizar Zora (reemplazar la carpeta con un zip nuevo, o incluso
    reinstalar) NUNCA borra tus datos — solo se pierden si borras esta
    carpeta a propósito.
      - Windows: %LOCALAPPDATA%\\Zora  (normalmente C:\\Users\\<tú>\\AppData\\Local\\Zora)
      - macOS:   ~/Library/Application Support/Zora
      - Linux:   ~/.local/share/zora
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "Zora")
    elif system == "Darwin":
        path = os.path.expanduser("~/Library/Application Support/Zora")
    else:
        path = os.path.expanduser("~/.local/share/zora")
    os.makedirs(path, exist_ok=True)
    return path


DATA_DIR = _default_data_dir()
DB_PATH = os.environ.get("ZORA_DB_PATH", os.path.join(DATA_DIR, "zora.db"))
_db_lock = threading.Lock()  # sqlite3 no es seguro para escrituras concurrentes sin esto


def _migrate_legacy_db_if_needed():
    """
    Una sola vez: si zora.db aún no existe en la carpeta nueva y persistente
    (DATA_DIR), pero SÍ existe uno viejo al lado del código (donde vivía
    antes de este cambio), lo copiamos para no perder cuentas/historial ya
    creados en versiones anteriores de Zora.
    """
    if os.path.exists(DB_PATH):
        return
    legacy_path = os.path.join(os.path.dirname(__file__), "zora.db")
    if os.path.exists(legacy_path):
        import shutil
        shutil.copy2(legacy_path, DB_PATH)
        print(f"(Migración) Encontré una base de datos anterior en {legacy_path} "
              f"y la copié a la ubicación nueva y persistente: {DB_PATH}")


_migrate_legacy_db_if_needed()


# =========================================================================
# 1b. BASE DE DATOS EN LA NUBE (Turso — SQLite hospedado, OPCIONAL)
# =========================================================================
# En hosts con disco efímero (plan gratis de Render y similares) el archivo
# local zora.db se borra en cada reinicio. Con Turso (https://turso.tech —
# plan gratis generoso, sin tarjeta) la MISMA base de datos vive en la nube
# y sobrevive a reinicios, redespliegues y siestas del servidor.
#
#   1. Crea cuenta gratis en turso.tech y crea una base de datos.
#   2. Saca la URL y un token (con su CLI):
#        turso db show <nombre> --url
#        turso db tokens create <nombre>
#   3. Pégalos en zora.env:
#        TURSO_DATABASE_URL=https://tu-bd-tu-usuario.turso.io
#        TURSO_AUTH_TOKEN=tu-token-largo-aqui
#
# Sin esas dos variables TODO sigue igual que siempre: zora.db local.
# Con ellas, get_db() devuelve un conector compatible que habla con Turso
# por HTTP usando solo urllib (cero dependencias nuevas). El resto del
# backend no cambia ni una línea: mismas consultas SQL, mismos resultados.
#
# Truco de eficiencia: los INSERT/UPDATE/DELETE se agrupan en memoria y
# salen TODOS JUNTOS en un solo viaje HTTP al llamar commit() — igual que
# el patrón execute()->commit()->close() que ya usa todo este archivo.

def _cloud_env(name: str):
    value = os.environ.get(name, "")
    if not value or value.strip().upper().startswith("PEGA-AQUI"):
        return None
    return value.strip()


TURSO_DATABASE_URL = _cloud_env("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = _cloud_env("TURSO_AUTH_TOKEN")


def _turso_encode_arg(value):
    """Convierte un valor Python al formato que pide la API HTTP v2 de Turso."""
    if value is None:
        return {"type": "n"}
    if isinstance(value, bool):
        return {"type": "i", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "i", "value": str(value)}
    if isinstance(value, float):
        return {"type": "f", "value": repr(value)}
    if isinstance(value, bytes):
        return {"type": "b", "value": base64.b64encode(value).decode("ascii"), "base64": True}
    return {"type": "s", "value": str(value)}


def _turso_decode_value(cell):
    """Convierte una celda de respuesta Turso a su valor Python real."""
    if not isinstance(cell, dict):
        return cell
    ctype = cell.get("type", "")
    raw = cell.get("value")
    if ctype in ("n", "null") or raw is None:
        return None
    try:
        if ctype in ("i", "integer"):
            return int(raw)
        if ctype in ("f", "real"):
            return float(raw)
    except (TypeError, ValueError):
        return raw
    return raw


class TursoRow:
    """Imita a sqlite3.Row: row["columna"], dict(row), r.keys(), etc."""

    def __init__(self, cols, values):
        names = [c["name"] for c in cols]
        self._order = names
        self._data = {
            names[i]: (_turso_decode_value(values[i]) if i < len(values) else None)
            for i in range(len(names))
        }

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._order[key]]
        return self._data[key]

    def keys(self):
        return list(self._order)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data

    def __repr__(self):
        return f"TursoRow({self._data!r})"


class TursoCursor:
    def __init__(self, cols, rows):
        self._rows = [TursoRow(cols, r) for r in (rows or [])]
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out


class TursoConnection:
    """Imita la parte de sqlite3.Connection que usa este backend, pero habla
    con Turso por HTTP. Los escrituras se agrupan hasta commit()."""

    def __init__(self):
        self._pending = []

    @staticmethod
    def _pipeline(stmts):
        body = json.dumps({"requests": [*stmts, {"type": "close"}]}).encode()
        req = urllib.request.Request(
            TURSO_DATABASE_URL.rstrip("/") + "/v2/pipeline",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Turso devolvió error {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from e
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            raise RuntimeError(f"No se pudo hablar con la base de datos en la nube (Turso): {e}") from e

        results = payload.get("results", [])
        reads = []
        for r in results:
            if r.get("type") == "error":
                msg = (r.get("error") or {}).get("message", "error desconocido")
                raise RuntimeError(f"Turso: {msg}")
            res = ((r.get("response") or {}).get("result")) if r.get("type") == "ok" else None
            if isinstance(res, dict) and "cols" in res:
                reads.append((res["cols"], res["rows"]))
        return reads

    @staticmethod
    def _stmt(sql, params):
        return {"type": "execute",
                "stmt": {"sql": sql, "args": [_turso_encode_arg(p) for p in params]}}

    def execute(self, sql, params=()):
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "BEGIN"):
            # Escritura (o PRAGMA-like): se acumula para mandar junta en commit()
            self._pending.append(self._stmt(sql, tuple(params)))
            return None
        # Lectura: primero vaciar pendientes (mismo orden que sqlite local)
        self.commit()
        reads = self._pipeline([self._stmt(sql, tuple(params))])
        if not reads:
            return TursoCursor([], [])
        cols, rows = reads[0]
        return TursoCursor(cols, rows)

    def executescript(self, script):
        stmts = []
        for raw in script.split(";"):
            lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
            clean = "\n".join(lines).strip()
            if clean:
                stmts.append(self._stmt(clean, ()))
        self._pending.extend(stmts)
        self.commit()

    def commit(self):
        if not self._pending:
            return
        stmts, self._pending = self._pending, []
        self._pipeline(stmts)

    def close(self):
        try:
            self.commit()
        except Exception:  # noqa: BLE001 — cerrar nunca debe tumbar nada
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def get_db():
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        return TursoConnection()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        shared INTEGER NOT NULL,
        last_seen REAL,
        online INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS device_tokens (
        device_token TEXT PRIMARY KEY,
        device_id TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS trusted_contacts (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT
    );
    CREATE TABLE IF NOT EXISTS geofences (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        radius_m REAL NOT NULL,
        device_id TEXT
    );
    CREATE TABLE IF NOT EXISTS locations (
        device_id TEXT PRIMARY KEY,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS geofence_state (
        device_id TEXT NOT NULL,
        geofence_id TEXT NOT NULL,
        inside INTEGER NOT NULL,
        PRIMARY KEY (device_id, geofence_id)
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        device_id TEXT,
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sos_events (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        device_id TEXT,
        lat REAL,
        lon REAL,
        ts REAL NOT NULL,
        message TEXT,
        notified TEXT
    );
    CREATE TABLE IF NOT EXISTS activity (
        device_id TEXT NOT NULL,
        date TEXT NOT NULL,
        steps INTEGER DEFAULT 0,
        distance_km REAL DEFAULT 0,
        PRIMARY KEY (device_id, date)
    );
    CREATE TABLE IF NOT EXISTS api_usage (
        service TEXT NOT NULL,
        period_key TEXT NOT NULL,
        count INTEGER DEFAULT 0,
        PRIMARY KEY (service, period_key)
    );
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS last_image (
        user_id TEXT PRIMARY KEY,
        image_id TEXT NOT NULL,
        prompt TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reminders (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        device_id TEXT,
        message TEXT NOT NULL,
        trigger_ts REAL NOT NULL,
        repeat_daily INTEGER DEFAULT 0,
        fired INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS reminder_deliveries (
        reminder_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        delivered_ts REAL NOT NULL,
        PRIMARY KEY (reminder_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS flashcard_decks (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        topic TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS flashcards (
        id TEXT PRIMARY KEY,
        deck_id TEXT NOT NULL,
        front TEXT NOT NULL,
        back TEXT NOT NULL,
        times_shown INTEGER DEFAULT 0,
        times_correct INTEGER DEFAULT 0,
        last_result INTEGER
    );
    CREATE TABLE IF NOT EXISTS quiz_sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        topic TEXT,
        questions TEXT NOT NULL,
        answers TEXT,
        score INTEGER,
        total INTEGER,
        state TEXT DEFAULT 'active',
        created_at REAL NOT NULL,
        finished_at REAL
    );
    """)
    conn.commit()
    conn.close()


# ---- Sesiones y colas de comandos: en memoria (ver nota arriba) ----
SESSIONS = {}          # token -> {"user_id": ..., "expires": ...}
COMMAND_QUEUES = {}    # device_id -> [ {"command_id":..., "action":..., "params":...} ]
COMMAND_RESULTS = {}   # command_id -> {"status": "pending"|"done", "result": ...}

SESSION_DURATION_SECONDS = 60 * 60 * 8  # 8 horas


# =========================================================================
# 2. AUTENTICACIÓN (login, tokens, permisos por dispositivo)
# =========================================================================

def hash_password(password: str, salt: str) -> str:
    """Nunca se guarda la contraseña en texto plano — se guarda su hash."""
    return hashlib.sha256((salt + password).encode()).hexdigest()


def create_user(username: str, password: str) -> dict:
    with _db_lock:
        conn = get_db()
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            conn.close()
            raise ValueError("Ese usuario ya existe")
        salt = secrets.token_hex(8)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt) VALUES (?,?,?)",
            (username, hash_password(password, salt), salt),
        )
        conn.commit()
        conn.close()
    return {"user_id": username}


def login(username: str, password: str) -> str:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or hash_password(password, row["salt"]) != row["password_hash"]:
        raise PermissionError("Usuario o contraseña incorrectos")
    token = secrets.token_hex(24)
    SESSIONS[token] = {"user_id": username, "expires": time.time() + SESSION_DURATION_SECONDS}
    return token


def user_from_token(token: str) -> str:
    session = SESSIONS.get(token)
    if not session or session["expires"] < time.time():
        raise PermissionError("Sesión inválida o expirada, inicia sesión de nuevo")
    return session["user_id"]


def register_device(name: str, device_type: str, owner_id: str) -> dict:
    """
    device_type: 'tv' (compartida, cualquier familiar autorizado la controla)
                 'laptop' o 'celular' (personal, solo el dueño la controla)

    Para 'laptop'/'celular' además se genera un DEVICE TOKEN aparte del
    token de usuario — es lo que el AGENTE que corre en ese dispositivo usa
    para autenticarse contra el backend.
    """
    device_id = secrets.token_hex(6)
    shared = 1 if device_type == "tv" else 0
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO devices (device_id, name, type, owner_id, shared, last_seen, online) "
            "VALUES (?,?,?,?,?,?,0)",
            (device_id, name, device_type, owner_id, shared, time.time()),
        )
        result = {"device_id": device_id, "name": name, "type": device_type,
                  "owner_id": owner_id, "shared": bool(shared)}
        if device_type in ("laptop", "celular"):
            device_token = secrets.token_hex(24)
            conn.execute(
                "INSERT INTO device_tokens (device_token, device_id) VALUES (?,?)",
                (device_token, device_id),
            )
            result["device_token"] = device_token  # se le da UNA vez, al registrar
        conn.commit()
        conn.close()
    return result


def get_device(device_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def can_control_device(user_id: str, device_id: str) -> bool:
    device = get_device(device_id)
    if not device:
        return False
    if device["shared"]:
        return True  # TV: cualquier familiar con cuenta puede controlarla
    return device["owner_id"] == user_id  # laptop/celular: solo el dueño


def device_from_token(device_token: str) -> str:
    conn = get_db()
    row = conn.execute(
        "SELECT device_id FROM device_tokens WHERE device_token=?", (device_token,)
    ).fetchone()
    conn.close()
    if not row:
        raise PermissionError("Token de dispositivo inválido")
    return row["device_id"]


def enqueue_command(device_id: str, action: str, params: dict) -> str:
    command_id = secrets.token_hex(8)
    COMMAND_QUEUES.setdefault(device_id, []).append({
        "command_id": command_id,
        "action": action,
        "params": params,
    })
    COMMAND_RESULTS[command_id] = {"status": "pending", "result": None}
    return command_id


# =========================================================================
# 3. COMANDOS RÁPIDOS (ejecución local, sin pasar por el modelo de IA)
# =========================================================================
# Para tareas simples y frecuentes NO llamamos al modelo grande — solo
# hacemos coincidencia de patrones y devolvemos la acción directa.
# Cada entrada es: (patrón regex, nombre de la acción)

def _no_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


QUICK_COMMANDS = [
    (r"\b(?:prende|enciende)\s+la\s+linterna\b", "flashlight_on"),
    (r"\bapaga\s+la\s+linterna\b", "flashlight_off"),
    (r"\bsube\s+el?\s+volumen\b", "volume_up"),
    (r"\bbaja\s+el?\s+volumen\b", "volume_down"),
    # Música exige la palabra explícita (música/canción/reproduce/toca) para
    # que "pon arroz a la lista" no termine sonando en Spotify.
    (r"\b(?:pon|reproduce)\s+(?:música|la\s+canción|el\s+tema)\b\s*[:\-]?\s*(.*)", "spotify_play"),
    (r"\b(?:reproduce|toca)\s+(?:el\s+tema\s+)?(.+)", "spotify_play"),
    (r"\b(?:pausa|detén|para)\s+la\s+música\b", "spotify_pause"),
    (r"\bqué\s+hora\s+es\b", "get_time"),
    (r"\bcuánt[oa]s?\s+(?:pasos|distancia)\s+llevo\b", "get_activity_stats"),
]

# Los patrones se comparan SIN acentos y el texto de entrada también se
# normaliza sin acentos — así "pon musica rock" (sin tilde) funciona igual
# que "pon música rock".
_QUICK_RES = [(re.compile(_no_accents(p)), a) for p, a in QUICK_COMMANDS]

# Palabras que disparan el flujo de emergencia directo (sin pasar por el
# cerebro en la nube ni por comandos rápidos normales — máxima prioridad).
SOS_PATTERN = re.compile(r"\b(?:sos|auxilio|ayuda\s*(?:urgente|por\s+favor)?|emergencia)\b")

# Acciones que SOLO tienen sentido en una computadora con agente corriendo.
# Si el usuario no eligió laptop y tiene exactamente UNA, se le enruta sola;
# si tiene varias, se le pregunta cuál; si no tiene ninguna, se le explica
# cómo conectarla (antes esto fingía éxito sin hacer nada — bug reportado).
DEVICE_BOUND_ACTIONS = {"volume_up", "volume_down", "spotify_play", "spotify_pause"}

COMO_CONECTAR_PC = (
    "Todavía no tienes ninguna computadora conectada a Zora. Conectarla son "
    "dos pasos (un minuto): (1) abre la web de Zora en esa PC y pulsa el "
    "botón \"Conectar esta PC\" del encabezado — te dará un token; "
    "(2) descarga ahí zora_laptop_agent.py y córrelo pegando ese token. "
    "Cuando el agente quede en línea, dime por ejemplo 'sube el volumen' "
    "o 'abre spotify' y lo hago de verdad. Los celulares aún no están "
    "soportados, lo siento.")


def _no_laptop_guidance() -> dict:
    """Respuesta tipo cerebro con la guía de conexión."""
    return {"response_text": COMO_CONECTAR_PC}


def try_quick_command(text: str):
    normalized = _no_accents(text.strip().lower())
    for pattern, action in _QUICK_RES:
        match = pattern.search(normalized)
        if match:
            payload = {"action": action, "raw_text": text}
            if match.groups():
                # Con varios grupos (patrones con partes opcionales) nos
                # quedamos con el primero que no sea None.
                payload["param"] = next((g for g in match.groups() if g), "").strip()
            return payload
    return None


# =========================================================================
# 4. CEREBRO EN LA NUBE (llamada al modelo de IA para lo complejo)
# =========================================================================

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Respaldo GRATIS del cerebro en la nube: NVIDIA Build (build.nvidia.com) da
# una API key gratis (sin tarjeta) que habla el mismo formato que OpenAI y
# sirve más de 80 modelos (Llama, DeepSeek, GLM, Nemotron...). Zora la usa
# automáticamente si no configuraste ANTHROPIC_API_KEY, o si esta se quedó
# sin fondos — así siempre hay un cerebro respondiendo, aunque sea uno más
# sencillo que Claude.
#   1. Entra a https://build.nvidia.com , crea cuenta gratis.
#   2. Abre cualquier modelo (ej. "Llama 3.3 70B Instruct") y saca tu key
#      (empieza con "nvapi-").
#   3. export NVIDIA_API_KEY="nvapi-..."
def _real_env_key(name: str):
    """Lee una variable de entorno y la trata como 'no configurada' si está
    vacía o si sigue siendo el texto de plantilla de los launchers
    (iniciar_zora_windows.bat / iniciar_zora_mac_linux.sh) — así, si
    alguien se le olvida reemplazar 'PEGA-AQUI-...', Zora avisa con el
    mensaje claro de 'no tengo esta API configurada' en vez de mandarle a
    Google/NVIDIA una key inválida y devolver un error más confuso."""
    value = os.environ.get(name, "")
    if not value or value.strip().upper().startswith("PEGA-AQUI"):
        return None
    return value


NVIDIA_API_KEY = _real_env_key("NVIDIA_API_KEY")
# meta/llama-3.3-70b-instruct es de los modelos grandes del catálogo
# gratis de NVIDIA y puede tardar 60-100+ segundos en horas de mucha
# demanda. Usamos por defecto una versión mucho más rápida (responde en
# 2-5 segundos típico); sigue siendo suficiente para comandos de casa.
# Este valor es solo el punto de partida al arrancar — se puede cambiar
# en caliente diciéndole "Zora, usa el modelo <nombre>" (ver más abajo),
# sin reiniciar el backend. Si prefieres otro por defecto sin tocar
# nada por voz, pon NVIDIA_MODEL en zora.env.
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
NVIDIA_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# Respaldo adicional: OpenRouter (openrouter.ai) es un agregador que da
# acceso a cientos de modelos (OpenAI, Google, Meta, Mistral, DeepSeek...)
# con una sola API compatible con OpenAI. Tiene modelos gratis (sufijo
# ":free") y de pago (recargas crédito). Sirve como TERCER nivel de
# respaldo: si Claude falla o no está, Zora prueba OpenRouter; si ese
# tampoco responde, cae al respaldo gratis de NVIDIA.
#   1. Crea cuenta en https://openrouter.ai/keys
#   2. Genera una API key (empieza con "sk-or-v1-").
#   3. OPENROUTER_API_KEY=sk-or-v1-... en zora.env
OPENROUTER_API_KEY = _real_env_key("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

# =========================================================================
# 3.5 MODELO DEL CEREBRO, CAMBIABLE POR VOZ
# =========================================================================
# El catálogo gratis de NVIDIA Build tiene varios modelos con distinto
# balance entre velocidad y calidad de respuesta. En vez de tener que
# editar zora.env y reiniciar cada vez que se quiere probar otro, se
# puede cambiar hablándole a Zora directamente. El cambio es global (para
# todos los usuarios de esta Zora, no por persona) y vive en memoria — si
# reinicias el backend, vuelve al de zora.env.

MODEL_CATALOG = [
    {"id": "meta/llama-3.1-8b-instruct", "label": "rápido (por defecto — responde en segundos, más simple)"},
    {"id": "nvidia/nemotron-nano-9b-v2", "label": "nano (de NVIDIA, más natural, velocidad parecida al rápido)"},
    {"id": "mistralai/mistral-nemotron", "label": "nemotron (punto medio entre velocidad y calidad)"},
    {"id": "meta/llama-3.3-70b-instruct", "label": "grande (mejor calidad, puede tardar bastante más)"},
    {"id": "nvidia/nemotron-3-ultra-550b-a55b", "label": "ultra (la mejor calidad del catálogo, el más lento)"},
]
MODEL_BY_ID = {m["id"]: m for m in MODEL_CATALOG}
_current_nvidia_model = NVIDIA_MODEL  # el que está activo ahora mismo


def get_current_model() -> str:
    return _current_nvidia_model


def set_current_model(model_id: str) -> dict:
    global _current_nvidia_model
    entry = MODEL_BY_ID.get(model_id)
    if not entry:
        raise ValueError(
            f"No conozco el modelo '{model_id}'. Opciones: " + ", ".join(m["id"] for m in MODEL_CATALOG)
        )
    _current_nvidia_model = entry["id"]
    return entry


MODEL_LIST_PATTERN = re.compile(r"\bqu[eé]\s+modelos?\s+(?:hay|tienes|disponibles)\b")
MODEL_CURRENT_PATTERN = re.compile(r"\bqu[eé]\s+modelo\s+tienes\b")
MODEL_CHANGE_PATTERN = re.compile(r"\busa(?:r)?\s+(?:el\s+)?modelo\s+(.+)")


def try_model_settings_command(text: str):
    """Devuelve un dict de respuesta si el texto es un comando sobre el
    modelo del cerebro (listar/consultar/cambiar), o None si no aplica."""
    normalized = text.strip().lower()

    if MODEL_LIST_PATTERN.search(normalized):
        options = ", ".join(f"{m['id'].split('/')[-1]} ({m['label']})" for m in MODEL_CATALOG)
        return {"response_text": f"Estos son los modelos que puedo usar: {options}."}

    if MODEL_CURRENT_PATTERN.search(normalized):
        entry = MODEL_BY_ID.get(_current_nvidia_model, {})
        return {"response_text": f"Ahora mismo pienso con el modelo {entry.get('label', _current_nvidia_model)}."}

    match = MODEL_CHANGE_PATTERN.search(normalized)
    if match:
        requested = match.group(1).strip().rstrip(".")
        found = None
        for m in MODEL_CATALOG:
            short_name = m["id"].split("/")[-1]
            if requested == m["id"].lower() or requested in short_name.lower() or short_name.lower().split("-")[0] in requested:
                found = m
                break
        if not found:
            options = ", ".join(m["id"].split("/")[-1] for m in MODEL_CATALOG)
            return {"response_text": f"No conozco ese modelo. Puedo usar: {options}."}
        set_current_model(found["id"])
        return {"response_text": f"Listo, ahora voy a pensar con el modelo {found['label']}. "
                                  f"El primer comando después de cambiar puede tardar un poco más."}

    return None

# El prompt base ahora refleja la verdad: Zora SÍ puede controlar la
# computadora conectada del usuario a través del sistema de agentes
# (comandos rápidos reconocidos por frase exacta). Antes este prompt decía
# "NO tienes forma real de controlar dispositivos" y el modelo respondía
# "no puedo" aunque el sistema tuviera todo listo — ese fue exactamente el
# reporte del usuario. Lo que sigue prohibido es MENTIR: nunca decir que ya
# ejecutó algo si el agente no lo hizo, ni inventar dispositivos.
BASE_SYSTEM_PROMPT = (
    "Eres Zora, el cerebro de un asistente domestico familiar. Si puedes "
    "controlar la computadora del usuario: el sistema reconoce estas frases "
    "EXACTAS y las envia a su agente en la laptop — 'sube el volumen', "
    "'baja el volumen', 'pon musica <nombre>' o 'reproduce <nombre>', "
    "'pausa la musica', 'abre <aplicacion>', 'bloquea la pantalla' y "
    "'opencode <tarea de programacion>'. Cuando te pidan esas cosas con "
    "otras palabras, NO digas que no puedes: confirmalo y sugiere la frase "
    "exacta (ejemplo: 'dime \"abre spotify\" y lo abro en tu computadora'). "
    "La linterna es del celular y TODAVIA no tienes agente para telefonos: "
    "si te la piden, explicalo con carino y honestidad. Nunca digas que ya "
    "hiciste algo si el agente aun no lo confirma, no inventes dispositivos "
    "que no esten en la lista de abajo, y NUNCA escribas etiquetas tipo "
    "[ACCION: algo]. Responde breve, natural y calido."
)

# Un dispositivo cuenta como "en línea" si su agente poll-eó hace poco.
DEVICE_ONLINE_WINDOW_SECONDS = 30


def list_user_laptops(user_id=None):
    """Laptops registradas de este usuario, la más reciente primero."""
    if not user_id:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT device_id, name, online, last_seen FROM devices "
        "WHERE owner_id=? AND type='laptop' ORDER BY last_seen DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    now = time.time()
    return [{
        "device_id": r["device_id"],
        "name": r["name"],
        "online": bool(r["online"]) and (now - (r["last_seen"] or 0)) < DEVICE_ONLINE_WINDOW_SECONDS,
    } for r in rows]


def build_system_prompt(user_id=None):
    """Prompt del cerebro + contexto EN VIVO de los dispositivos de este
    usuario — así Zora sabe qué tiene conectado y guía en consecuencia."""
    prompt = BASE_SYSTEM_PROMPT
    laptops = list_user_laptops(user_id)
    if laptops:
        estado = "; ".join(
            f"{l['name']} ({'EN LINEA' if l['online'] else 'sin conexion'})"
            for l in laptops)
        prompt += (" Dispositivos de este usuario: " + estado + ". Si hay "
                   "alguno EN LINEA sus comandos rapidos llegan al agente; "
                   "si ninguno esta en linea, recuerdale abrir el agente en "
                   "esa computadora (python zora_laptop_agent.py).")
    else:
        prompt += (" Este usuario TODAVIA no tiene ninguna computadora "
                   "conectada. Si quiere que controles su PC, explicale los "
                   "dos pasos: (1) registrarla desde la web de Zora con el "
                   "boton 'Conectar esta PC' y (2) correr alli "
                   "'python zora_laptop_agent.py' pegando el token que le "
                   "den. Los celulares todavia no son soportados.")
    return prompt


# Cuántos turnos previos (usuario+Zora) se mandan como contexto en cada
# llamada al modelo. Más alto = recuerda más, pero cada mensaje tarda un
# poco más y gasta más tokens/cuota gratis.
MEMORY_TURNS = 10


def get_chat_history(user_id: str, limit: int = MEMORY_TURNS) -> list:
    """Últimos N turnos (usuario+asistente) de este usuario, en orden
    cronológico, listos para mandarle al modelo como contexto."""
    conn = get_db()
    rows = conn.execute(
        "SELECT role, content FROM chat_history WHERE user_id=? "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit * 2),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_chat_turn(user_id: str, role: str, content: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO chat_history (user_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (user_id, role, content, time.time()),
    )
    conn.commit()
    conn.close()


def clear_chat_history(user_id: str):
    conn = get_db()
    conn.execute("DELETE FROM chat_history WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def call_cloud_brain(user_text: str, user_id: str = None) -> str:
    history = get_chat_history(user_id) if user_id else []
    # Prompt con contexto EN VIVO de los dispositivos de este usuario —
    # así Zora sabe qué tiene conectado y guía en vez de decir "no puedo".
    system = build_system_prompt(user_id)
    # Cadena de respaldo: Claude -> OpenRouter -> NVIDIA (gratis) -> simulado.
    # Cada proveedor devuelve None si falla (sin clave, error de red, cuota
    # agotada...) para que el siguiente lo intente. Solo NVIDIA (el último
    # peldaño gratis) devuelve siempre un mensaje de cara al usuario.
    answer = None
    if ANTHROPIC_API_KEY:
        answer = _call_anthropic(user_text, history=history, system_prompt=system)
    if answer is None and OPENROUTER_API_KEY:
        answer = _call_openrouter(user_text, history=history, system_prompt=system)
    if answer is None and NVIDIA_API_KEY:
        answer = _call_nvidia(user_text, history=history, system_prompt=system)
    if answer is None:
        answer = (
            "(Simulado) No hay ANTHROPIC_API_KEY, OPENROUTER_API_KEY ni "
            "NVIDIA_API_KEY configuradas todavía, así que no puedo pensar en "
            "esto de verdad — pero en producción aquí llamaría al modelo con "
            "tu comando: \"" + user_text + "\""
        )
    if user_id:
        save_chat_turn(user_id, "user", user_text)
        save_chat_turn(user_id, "assistant", answer)
    return answer


def _call_anthropic(user_text: str, history: list = None, system_prompt: str = None):
    """Devuelve el texto de respuesta, o None si falló (para que la cadena
    de respaldo en call_cloud_brain intente con OpenRouter/NVIDIA)."""
    messages = list(history or []) + [{"role": "user", "content": user_text}]
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "system": system_prompt or BASE_SYSTEM_PROMPT,
        "messages": messages,
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
            return "\n".join(parts) if parts else "(el modelo no devolvió texto)"
    except Exception:  # noqa: BLE001 — cualquier fallo deja que la cadena pruebe el siguiente proveedor
        return None


def _call_openrouter(user_text: str, history: list = None, system_prompt: str = None):
    """Segundo peldaño de respaldo. Devuelve el texto, o None si falla
    (sin clave, error de red, modelo no disponible...) para que NVIDIA
    tome el relevo. No lleva control de cuota propia: OpenRouter cobra por
    uso según el modelo (los ':free' no cuestan), así que no hay tope
    mensual que llevar aquí como con NVIDIA Build."""
    if not OPENROUTER_API_KEY:
        return None
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt or BASE_SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 500,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            # OpenRouter pide (opcional) identificar la app para estadísticas
            "HTTP-Referer": "https://zora.local",
            "X-Title": "Zora",
        },
        method="POST",
    )
    try:
        # Timeout generoso: algunos modelos de OpenRouter pueden tardar.
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return text if text else "(el modelo no devolvió texto)"
    except Exception:  # noqa: BLE001 — fallo deja que NVIDIA intente
        return None


def _call_nvidia(user_text: str, note: str = "", history: list = None,
                 system_prompt: str = None) -> str:
    if usage_exhausted("cerebro_nvidia"):
        u = get_usage("cerebro_nvidia")
        return (f"Se acabaron los tokens gratis de este mes de la IA de respaldo (NVIDIA) "
                f"— se recargan {u['resets']}. Mientras tanto no puedo pensar comandos "
                f"complejos, solo los rápidos (linterna, volumen, música, etc.).")

    body = json.dumps({
        "model": get_current_model(),
        "messages": [
            {"role": "system", "content": system_prompt or BASE_SYSTEM_PROMPT},
            *(history or []),
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 500,
    }).encode()
    req = urllib.request.Request(
        NVIDIA_CHAT_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {NVIDIA_API_KEY}"},
        method="POST",
    )
    try:
        # Timeout más largo (90s) por si el modelo grande está en uso o
        # el servicio gratis de NVIDIA está saturado a esa hora; con el
        # modelo rápido por defecto casi nunca se llega a esperar tanto.
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            increment_usage("cerebro_nvidia")
            return note + (text or "(el modelo no devolvió texto)")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            increment_usage("cerebro_nvidia", mark_exhausted=True)
            u = get_usage("cerebro_nvidia")
            return (f"Se acabaron los tokens gratis de este mes de la IA de respaldo "
                    f"(NVIDIA) — se recargan {u['resets']}.")
        return f"(Error llamando al respaldo NVIDIA: {e.code})"
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return ("No me respondió a tiempo la IA de respaldo (NVIDIA) — puede ser tu "
                "internet o que su servicio esté lento ahora mismo. Intenta de nuevo "
                "en un momento.")


# =========================================================================
# 5. GEOCERCAS Y UBICACIÓN
# =========================================================================

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distancia en metros entre dos coordenadas GPS."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def create_alert(owner_id, device_id, kind, message):
    """Uso público (fuera de update_location, que ya tiene el lock tomado)."""
    alert_id = secrets.token_hex(8)
    with _db_lock:
        conn = get_db()
        _insert_alert(conn, alert_id, owner_id, device_id, kind, message)
        conn.commit()
        conn.close()
    return alert_id


def _insert_alert(conn, alert_id, owner_id, device_id, kind, message):
    """Inserta una alerta usando una conexión/lock que el llamador ya tiene
    abiertos — evita el deadlock de tomar _db_lock dos veces en el mismo hilo."""
    conn.execute(
        "INSERT INTO alerts (id, owner_id, device_id, kind, message, ts) VALUES (?,?,?,?,?,?)",
        (alert_id, owner_id, device_id, kind, message, time.time()),
    )


def update_location(device_id: str, lat: float, lon: float) -> list:
    """
    Guarda la última ubicación de un dispositivo y revisa TODAS las
    geocercas del dueño para ver si hubo una entrada/salida. Devuelve la
    lista de alertas nuevas generadas en esta actualización (puede estar
    vacía si no hubo ningún cambio de estado).
    """
    device = get_device(device_id)
    if not device:
        raise ValueError("Dispositivo no encontrado")
    owner_id = device["owner_id"]

    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO locations (device_id, lat, lon, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(device_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, ts=excluded.ts",
            (device_id, lat, lon, time.time()),
        )
        conn.commit()

        fences = conn.execute(
            "SELECT * FROM geofences WHERE owner_id=? AND (device_id IS NULL OR device_id=?)",
            (owner_id, device_id),
        ).fetchall()

        new_alerts = []
        for fence in fences:
            dist = haversine_m(lat, lon, fence["lat"], fence["lon"])
            now_inside = dist <= fence["radius_m"]

            prev = conn.execute(
                "SELECT inside FROM geofence_state WHERE device_id=? AND geofence_id=?",
                (device_id, fence["id"]),
            ).fetchone()
            prev_inside = bool(prev["inside"]) if prev else None

            if prev_inside is None or bool(prev_inside) != now_inside:
                conn.execute(
                    "INSERT INTO geofence_state (device_id, geofence_id, inside) VALUES (?,?,?) "
                    "ON CONFLICT(device_id, geofence_id) DO UPDATE SET inside=excluded.inside",
                    (device_id, fence["id"], int(now_inside)),
                )
                # La primera vez que se ve el dispositivo no avisamos (no es
                # una entrada/salida real, es solo el punto de partida).
                if prev_inside is not None:
                    kind = "geofence_enter" if now_inside else "geofence_exit"
                    verbo = "entró a" if now_inside else "salió de"
                    message = f"{device['name']} {verbo} la zona \"{fence['name']}\""
                    alert_id = secrets.token_hex(8)
                    _insert_alert(conn, alert_id, owner_id, device_id, kind, message)
                    new_alerts.append({"id": alert_id, "kind": kind, "message": message})
        conn.commit()
        conn.close()
    return new_alerts


def get_last_location(device_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM locations WHERE device_id=?", (device_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# =========================================================================
# 6. EMERGENCIAS / SOS Y CONTACTOS DE CONFIANZA
# =========================================================================

SMTP_HOST = os.environ.get("ZORA_SMTP_HOST")
SMTP_PORT = int(os.environ.get("ZORA_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("ZORA_SMTP_USER")
SMTP_PASS = os.environ.get("ZORA_SMTP_PASS")
SMTP_FROM = os.environ.get("ZORA_SMTP_FROM", SMTP_USER)


def send_email(to_addr: str, subject: str, body: str) -> bool:
    """Manda un correo real por SMTP si hay credenciales configuradas.
    Si no las hay, no falla: solo devuelve False (modo demo)."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and to_addr):
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_addr
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001 (prototipo: no tumbar el SOS por un error de correo)
        print(f"[SOS] No se pudo mandar correo a {to_addr}: {e}")
        return False


# ---- SMS reales (textbee.dev — usa un Android tuyo como pasarela) ----
# Gratis, sin tarjeta: 300 SMS/mes. Instrucciones: https://textbee.dev
#   1. Instala la app de textbee en un Android tuyo y vincúlalo a tu cuenta.
#   2. Saca tu API key en el dashboard de textbee.dev.
#   3. TEXTBEE_API_KEY=tu-key en zora.env.
TEXTBEE_API_KEY = _real_env_key("TEXTBEE_API_KEY")
TEXTBEE_URL = "https://api.textbee.dev/api/v1/gateway/send-sms"


def send_sms(to_phone: str, message: str) -> bool:
    """Manda un SMS real vía textbee.dev si hay key configurada y el
    contacto tiene teléfono guardado. Si no, no falla: devuelve False."""
    if not (TEXTBEE_API_KEY and to_phone):
        return False
    body = json.dumps({"recipients": [to_phone], "message": message}).encode()
    req = urllib.request.Request(
        TEXTBEE_URL, data=body,
        headers={"x-api-key": TEXTBEE_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 300
    except Exception as e:  # noqa: BLE001 (prototipo: no tumbar el SOS por un error de SMS)
        print(f"[SOS] No se pudo mandar SMS a {to_phone}: {e}")
        return False


def trigger_sos(user_id: str, device_id, lat, lon, extra_message: str = "") -> dict:
    if (lat is None or lon is None) and device_id:
        last = get_last_location(device_id)
        if last:
            lat, lon = last["lat"], last["lon"]

    location_txt = (
        f"Última ubicación conocida: {lat}, {lon} (https://maps.google.com/?q={lat},{lon})"
        if lat is not None else "No hay ubicación disponible."
    )

    message = f"🚨 EMERGENCIA de {user_id}. {extra_message or ''}\n{location_txt}".strip()

    conn = get_db()
    contacts = conn.execute("SELECT * FROM trusted_contacts WHERE owner_id=?", (user_id,)).fetchall()
    conn.close()

    notified = []
    for c in contacts:
        # Prioridad: SMS (llega aunque no tenga internet) > correo > registrado nomás.
        sent_sms = send_sms(c["phone"], message) if c["phone"] else False
        sent_email = False if sent_sms else (send_email(c["email"], "🚨 Alerta de emergencia — Zora", message) if c["email"] else False)
        channel = "sms" if sent_sms else ("email" if sent_email else "simulado")
        notified.append({"contact": c["name"], "channel": channel, "sent_real": sent_sms or sent_email})
        if channel == "simulado":
            print(f"[SOS - MODO DEMO] Se le avisaría a {c['name']} ({c['phone'] or c['email'] or 'sin contacto'}):\n{message}")

    sos_id = secrets.token_hex(8)
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO sos_events (id, user_id, device_id, lat, lon, ts, message, notified) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sos_id, user_id, device_id, lat, lon, time.time(), message, json.dumps(notified)),
        )
        conn.commit()
        conn.close()

    return {"sos_id": sos_id, "message": message, "notified": notified, "contacts_count": len(contacts)}


# =========================================================================
# 7. ACTIVIDAD FÍSICA (pasos / distancia)
# =========================================================================

def today_str():
    return time.strftime("%Y-%m-%d")


def add_activity(device_id: str, steps: int, distance_km: float):
    date = today_str()
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO activity (device_id, date, steps, distance_km) VALUES (?,?,?,?) "
            "ON CONFLICT(device_id, date) DO UPDATE SET "
            "steps = steps + excluded.steps, distance_km = distance_km + excluded.distance_km",
            (device_id, date, steps, distance_km),
        )
        conn.commit()
        conn.close()


def get_activity_today(device_id: str) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM activity WHERE device_id=? AND date=?", (device_id, today_str())
    ).fetchone()
    conn.close()
    return dict(row) if row else {"device_id": device_id, "date": today_str(), "steps": 0, "distance_km": 0}


# =========================================================================
# 8. CONTROL DE CUOTA DE APIS GRATIS (imágenes, y cualquier otra que se
#    agregue después con el mismo patrón: SMS, deportes, etc.)
# =========================================================================
# Varias APIs externas dan una cantidad de usos gratis que se reinicia cada
# cierto tiempo (por día o por mes). Aquí llevamos la cuenta NOSOTROS, para
# poder avisarle al usuario ANTES de que la API le devuelva un error feo, y
# para poder decirle exactamente cuándo se recargan sus tokens gratis.
#
# Pollinations.ai (imágenes) no tiene límite propio que nosotros debamos
# controlar — es abierto y gratis — así que ya no lleva contador de cuota
# aquí. Solo queda el del cerebro de respaldo (NVIDIA), que sí tiene un
# tope mensual publicado.

FREE_TIER_LIMITS = {
    "cerebro_nvidia": {"limit": 950, "period": "monthly", "label": "cerebro de respaldo (NVIDIA Build)"},
}


def usage_period_key(period: str) -> str:
    return time.strftime("%Y-%m-%d") if period == "daily" else time.strftime("%Y-%m")


def next_reset_text(period: str) -> str:
    if period == "daily":
        return "mañana a medianoche"
    return "el día 1 del próximo mes"


def get_usage(service: str) -> dict:
    cfg = FREE_TIER_LIMITS[service]
    period_key = usage_period_key(cfg["period"])
    conn = get_db()
    row = conn.execute(
        "SELECT count FROM api_usage WHERE service=? AND period_key=?", (service, period_key)
    ).fetchone()
    conn.close()
    count = row["count"] if row else 0
    return {"service": service, "count": count, "limit": cfg["limit"],
            "remaining": max(0, cfg["limit"] - count), "period": cfg["period"],
            "resets": next_reset_text(cfg["period"]), "label": cfg["label"]}


def usage_exhausted(service: str) -> bool:
    u = get_usage(service)
    return u["remaining"] <= 0


def increment_usage(service: str, mark_exhausted: bool = False):
    """mark_exhausted=True se usa cuando la API misma nos dijo 'ya no te
    quedan' (429) aunque nuestro contador local todavía no llegara al
    límite — así dejamos de intentar el resto del día/mes en vez de seguir
    fallando en cada comando."""
    cfg = FREE_TIER_LIMITS[service]
    period_key = usage_period_key(cfg["period"])
    new_count = cfg["limit"] if mark_exhausted else None
    with _db_lock:
        conn = get_db()
        if new_count is not None:
            conn.execute(
                "INSERT INTO api_usage (service, period_key, count) VALUES (?,?,?) "
                "ON CONFLICT(service, period_key) DO UPDATE SET count=?",
                (service, period_key, new_count, new_count),
            )
        else:
            conn.execute(
                "INSERT INTO api_usage (service, period_key, count) VALUES (?,?,1) "
                "ON CONFLICT(service, period_key) DO UPDATE SET count=count+1",
                (service, period_key),
            )
        conn.commit()
        conn.close()


# =========================================================================
# 9. GENERACIÓN DE IMÁGENES (Pollinations.ai)
# =========================================================================
# Pollinations.ai es genuinamente gratis: sin API key, sin cuenta, sin
# tarjeta. Basta con pedirle una URL con la descripción y devuelve la
# imagen directamente. Por eso ya no hay "no_key" ni control de cuota
# propio aquí — si algo falla es un problema de red/servicio, no de saldo.

POLLINATIONS_IMAGE_URL = "https://image.pollinations.ai/prompt/{prompt}"
IMAGES_DIR = os.path.join(DATA_DIR, "generated_images")

# =========================================================================
# 9.5. INTERFAZ WEB SERVIDA POR EL PROPIO BACKEND
# =========================================================================
# En vez de que cada app (Windows, Mac, Linux, Android, TV, iOS) traiga su
# propia copia pegada del cliente por dentro, el backend también la sirve
# como página web normal. Así, actualizar la interfaz en el futuro es
# cambiar UN solo archivo (static/index.html) en UN solo lugar (el
# servidor) — todas las apps que apunten a esta URL ven el cambio la
# próxima vez que abren o recargan, sin reinstalar ni recompilar nada.
#
# Esto es justo lo que hace posible que las apps de escritorio (Electron)
# y Android (Capacitor) se configuren para cargar la URL del servidor en
# vez de un archivo local — ver STATIC_APP_URL más abajo y el README.

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/service-worker.js": ("service-worker.js", "application/javascript"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}

# =========================================================================
# 9.6. PÁGINA DE DESCARGAS (instalar sin cables ni comandos)
# =========================================================================
# Después de compilar el .apk/.exe/.dmg/.AppImage en tu propia PC (ver
# COMO_COMPILAR.md), copia esos archivos a la carpeta "downloads/" (al
# lado de zora_backend.py, se crea sola la primera vez que hace falta).
# En cuanto estén ahí, http://tu-servidor:8000/descargas los muestra como
# una página normal de descargas — cada quien la abre desde el navegador
# de su celular/PC/TV y le da a "descargar", sin USB, sin ADB, sin
# comandos. Nota importante sobre "buscarlo en Google": esto NO aparece
# solo en resultados de búsqueda de Google (eso requiere que Google
# indexe tu dominio público, algo que toma tiempo y no se puede forzar) —
# lo que sí funciona de inmediato es compartir el LINK directo
# (WhatsApp, correo, mensaje) para que cada quien lo abra y descargue.

DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), "downloads")

DOWNLOAD_FILE_INFO = {
    ".apk": ("application/vnd.android.package-archive",
             "Android / Android TV / Fire TV — actívale a Android \"permitir orígenes desconocidos\" al instalar"),
    ".exe": ("application/x-msdownload", "Windows — instalador normal, doble clic y siguiente-siguiente"),
    ".dmg": ("application/x-apple-diskimage", "Mac — ábrelo y arrastra Zora a Aplicaciones"),
    ".appimage": ("application/x-executable", "Linux — dale permiso de ejecución y ábrelo"),
}


def _download_page_html() -> str:
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    files = sorted(os.listdir(DOWNLOADS_DIR))
    rows = ""
    for name in files:
        ext = os.path.splitext(name)[1].lower()
        _, label = DOWNLOAD_FILE_INFO.get(ext, ("application/octet-stream", "Archivo"))
        size_mb = os.path.getsize(os.path.join(DOWNLOADS_DIR, name)) / (1024 * 1024)
        rows += (f'<a class="card" href="/downloads/{name}" download>'
                 f'<div class="name">{name}</div>'
                 f'<div class="label">{label}</div>'
                 f'<div class="size">{size_mb:.1f} MB — toca para descargar</div>'
                 f'</a>')
    if not rows:
        rows = ('<p class="empty">Todavía no hay instaladores aquí. Compílalos siguiendo '
                'COMO_COMPILAR.md y copia el .apk/.exe/.dmg/.AppImage a la carpeta '
                '"downloads/", al lado de zora_backend.py — aparecen solos en esta página, '
                'no hay que reiniciar nada.</p>')
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Descargar Zora</title>
<style>
  body {{ background:#0b0b0d; color:#f2f2f2; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         max-width:480px; margin:0 auto; padding:24px 16px; }}
  h1 {{ font-size:20px; }}
  p.sub {{ color:#9a9a9e; font-size:13px; margin-bottom:24px; }}
  .card {{ display:block; background:#151517; border:1px solid #232326; border-radius:12px;
           padding:14px; margin-bottom:10px; text-decoration:none; color:#f2f2f2; }}
  .name {{ font-weight:600; font-size:14px; }}
  .label {{ font-size:12px; color:#9a9a9e; margin-top:2px; }}
  .size {{ font-size:11px; color:#e0a8b0; margin-top:6px; }}
  .empty {{ color:#9a9a9e; font-size:13px; line-height:1.5; }}
</style></head>
<body>
  <h1>Descargar Zora</h1>
  <p class="sub">Abre esta página desde el celular, la PC o el navegador del TV donde quieras instalar Zora, y toca el archivo que te corresponda.</p>
  {rows}
</body></html>"""



def generate_image(prompt: str) -> dict:
    """
    Devuelve siempre un dict con "type":
      - "ok": {"type": "ok", "image_id": ...}
      - "error": cualquier fallo de red/servicio (no hay "no_key" ni
        "quota_exceeded" — Pollinations no pide key ni tiene cuota nuestra)
    """
    url = POLLINATIONS_IMAGE_URL.format(prompt=urllib.parse.quote(prompt)) + \
        "?width=1024&height=1024&nologo=true&seed=" + str(secrets.randbelow(10_000_000))
    req = urllib.request.Request(url, headers={"User-Agent": "Zora/1.0"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            image_bytes = resp.read()
    except urllib.error.HTTPError as e:
        return {"type": "error", "message": f"Pollinations devolvió un error ({e.code})."}
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return {"type": "error", "message": "El servicio de imágenes tardó demasiado en responder — intenta de nuevo en un momento."}

    if not image_bytes:
        return {"type": "error", "message": "El servicio de imágenes no devolvió nada para ese pedido."}

    os.makedirs(IMAGES_DIR, exist_ok=True)
    image_id = secrets.token_hex(8) + ".png"
    with open(os.path.join(IMAGES_DIR, image_id), "wb") as f:
        f.write(image_bytes)

    return {"type": "ok", "image_id": image_id}


IMAGE_REQUEST_PATTERN = re.compile(
    r"\b(?:genera(?:me)?|dibuja(?:me)?|crea(?:me)?|hazme|haz)\s+una?\s+(?:imagen|foto|dibujo)\s+(?:de|d[eé]l?)?\s*(.+)",
    re.IGNORECASE,
)

# "sí"/"dale"/"hazlo" después de que Zora ofreció mostrar o hacer una
# imagen: en vez de dejar que el chat general lo invente, generamos la
# imagen de verdad usando lo que Zora acababa de sugerir.
AFFIRMATIVE_PATTERN = re.compile(
    r"^\s*(?:s[ií]|dale|h[aá]zlo|ok|va|de\s+acuerdo|claro|por\s+favor)\s*[.!]?\s*$",
    re.IGNORECASE,
)

IMAGE_OFFER_PATTERN = re.compile(
    r"imagen(?:\s+de)?\s+(.+?)(?:[.?!]|$)",
    re.IGNORECASE,
)


def extract_offered_image_prompt(assistant_text: str):
    """Si el último mensaje de Zora mencionaba una imagen concreta (p.ej.
    'te muestro una imagen de X'), devuelve X para poder generarla de
    verdad. Si no encuentra nada claro, devuelve None."""
    match = IMAGE_OFFER_PATTERN.search(assistant_text or "")
    if not match:
        return None
    prompt = match.group(1).strip()
    # Corta en la primera coma larga o corchete para no arrastrar texto
    # de más (p.ej. etiquetas [ACCION: ...] que el modelo pudo colar).
    prompt = re.split(r"\[|,\s+con\s+", prompt)[0].strip()
    return prompt or None

# "dame/muéstrame/enséñame la imagen (otra vez)" — sin descripción nueva,
# pide repetir la última imagen que Zora generó para este usuario.
REPEAT_IMAGE_PATTERN = re.compile(
    r"^\s*(?:dame|mu[eé]strame|ens[eé][ñn]ame|ver|quiero\s+ver)\s+"
    r"(?:otra\s+vez\s+)?(?:la\s+)?imagen(?:\s+otra\s+vez)?\s*$",
    re.IGNORECASE,
)


def set_last_image(user_id: str, image_id: str, prompt: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO last_image (user_id, image_id, prompt) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET image_id=excluded.image_id, prompt=excluded.prompt",
        (user_id, image_id, prompt),
    )
    conn.commit()
    conn.close()


def get_last_image(user_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT image_id, prompt FROM last_image WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# =========================================================================
# 9.64 VOCES PREINSTALADAS DE ZORA (gratis, sin key, sin instalar nada)
# =========================================================================
# Antes la voz de Zora dependía de las voces que tuviera instaladas el
# sistema (y en muchos equipos no había ninguna buena en español). Ahora
# Zora trae SUS PROPIAS voces.
#
# Ruta técnica (probada en orden): Pollinations migró su API de audio a un
# gateway que ahora exige API key; StreamElements cerró su endpoint abierto.
# La que sigue funcionando gratis y sin cuenta es el TTS de Google
# Translate — con dos límites que ya resuelvo aquí: máximo ~200 caracteres
# por pedido (troceo el texto por puntuación junto) y pausa breve entre
# pedidos. Todo queda cacheado en disco: cada frase se sintetiza UNA vez.
#
# Si algún día este servicio también cambia, el cliente cae solo a las
# voces del navegador/sistema — Zora nunca se queda muda.

GOOGLE_TTS_URL = ("https://translate.google.com/translate_tts"
                  "?ie=UTF-8&client=tw-ob&tl={lang}&ttsspeed={speed}&q={text}")
TTS_CACHE_DIR = os.path.join(DATA_DIR, "tts_cache")

ZORA_VOICES = [
    {"id": "clasica", "lang": "es", "speed": "1", "label": "Zora Clásica (voz preinstalada)"},
    {"id": "despacio", "lang": "es", "speed": "0.5", "label": "Zora Despacio (para aprender)"},
    {"id": "ingles", "lang": "en", "speed": "1", "label": "Zora in English (listening)"},
]
ZORA_VOICE_IDS = {v["id"] for v in ZORA_VOICES}

_TTS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://translate.google.com/",
}


def _split_tts_text(text: str, max_len: int = 180) -> list:
    """Divide el texto en pedazos <= max_len cortando por puntuación o
    espacios (Google TTS rechaza frases muy largas)."""
    chunks, current = [], ""
    for part in re.split(r"([.!?;,:\n])", text):
        if not part:
            continue
        if len(current) + len(part) + 1 <= max_len:
            current += part
        else:
            if current.strip():
                chunks.append(current.strip())
            while len(part) > max_len:  # pieza enorme sin puntuación: corte duro
                cut = part.rfind(" ", 0, max_len)
                cut = cut if cut > 0 else max_len
                chunks.append(part[:cut].strip())
                part = part[cut:].strip()
            current = part
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c] or [text[:max_len]]


def generate_tts(text: str, voice: str = "clasica") -> dict:
    """Devuelve {"type": "ok", "audio": bytes} o {"type": "error", ...}."""
    text = (text or "").strip()
    if not text:
        return {"type": "error", "message": "no hay texto para convertir a voz"}
    if len(text) > 600:
        text = text[:600]  # para leer respuestas en voz basta y sobra

    preset = next((v for v in ZORA_VOICES if v["id"] == voice), ZORA_VOICES[0])

    os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    cache_key = hashlib.md5(f"{preset['id']}|{text}".encode()).hexdigest() + ".mp3"
    cache_path = os.path.join(TTS_CACHE_DIR, cache_key)
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 1000:
        with open(cache_path, "rb") as f:
            return {"type": "ok", "audio": f.read(), "cached": True}

    audio_parts = []
    try:
        for chunk in _split_tts_text(text):
            url = GOOGLE_TTS_URL.format(lang=preset["lang"], speed=preset["speed"],
                                        text=urllib.parse.quote(chunk))
            req = urllib.request.Request(url, headers=_TTS_HEADERS, method="GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = resp.read()
            if not ctype.startswith("audio") or len(data) < 500:
                return {"type": "error",
                        "message": "el servicio de voz no devolvió audio para esa frase"}
            audio_parts.append(data)
            time.sleep(0.12)  # pausa breve entre pedidos
    except urllib.error.HTTPError as e:
        return {"type": "error", "message": f"el servicio de voz devolvió un error ({e.code})"}
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        return {"type": "error", "message": "el servicio de voz tardó demasiado"}

    full_audio = b"".join(audio_parts)
    if len(full_audio) < 1000:
        return {"type": "error", "message": "el servicio de voz no devolvió audio"}
    with open(cache_path, "wb") as f:
        f.write(full_audio)
    return {"type": "ok", "audio": full_audio, "cached": False}


# =========================================================================
# 9.65 TRADUCTOR (NVIDIA Riva Translate — gratis, misma NVIDIA_API_KEY)
# =========================================================================
# Modelo aparte del "cerebro" de conversación, especializado solo en
# traducir: más rápido y no gasta la cuota mensual de cerebro_nvidia.

NVIDIA_TRANSLATE_MODEL = "nvidia/riva-translate-4b-instruct-v1_1"

TRANSLATE_PATTERN = re.compile(
    r"\btraduce(?:me)?\s+(?:esto\s+)?(?:al?\s+(\w+)\s*[:,]?\s*)?(.+)",
    re.IGNORECASE,
)


def translate_text(text: str, target_lang: str = "inglés") -> dict:
    """Devuelve {"type": "ok", "text": ...} o {"type": "no_key"/"error", ...}."""
    if not NVIDIA_API_KEY:
        return {"type": "no_key"}

    prompt = (f"Traduce el siguiente texto al {target_lang}. Responde solo "
              f"con la traducción, sin explicaciones:\n\n{text}")
    body = json.dumps({
        "model": NVIDIA_TRANSLATE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
    }).encode()
    req = urllib.request.Request(
        NVIDIA_CHAT_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {NVIDIA_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            translated = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if not translated:
                return {"type": "error", "message": "el traductor no devolvió texto"}
            return {"type": "ok", "text": translated.strip()}
    except urllib.error.HTTPError as e:
        return {"type": "error", "message": f"NVIDIA devolvió un error ({e.code})"}
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        return {"type": "error", "message": "no respondió a tiempo el traductor"}


# =========================================================================
# 9.66 TRANSCRIPCIÓN DE AUDIO (NVIDIA Parakeet ASR — gratis, NVIDIA_API_KEY)
# =========================================================================
# A diferencia del cerebro/traductor (JSON), este endpoint pide el audio
# como multipart/form-data — lo armamos a mano con la librería estándar,
# sin instalar nada extra (requests, etc.).

NVIDIA_ASR_URL = ("https://1598d209-5e27-4d3c-8079-4751568b1081"
                   ".invocation.api.nvcf.nvidia.com/v1/audio/transcriptions")


def transcribe_audio(audio_bytes: bytes, filename: str = "audio.wav", language: str = "es-US") -> dict:
    """Devuelve {"type": "ok", "text": ...} o {"type": "no_key"/"error", ...}."""
    if not NVIDIA_API_KEY:
        return {"type": "no_key"}

    boundary = "----zoraboundary" + secrets.token_hex(8)
    parts = []
    parts.append(f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"language\"\r\n\r\n"
                 f"{language}\r\n".encode())
    parts.append(f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: audio/wav\r\n\r\n".encode())
    parts.append(audio_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        NVIDIA_ASR_URL,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            text = data.get("text", "")
            if not text:
                return {"type": "error", "message": "no se detectó texto en el audio"}
            return {"type": "ok", "text": text.strip()}
    except urllib.error.HTTPError as e:
        return {"type": "error", "message": f"NVIDIA devolvió un error ({e.code})"}
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        return {"type": "error", "message": "no respondió a tiempo la transcripción"}


# =========================================================================
# 9.67 FILTRO DE SEGURIDAD FAMILIAR (Meta Llama Guard 4 — vía NVIDIA Build)
# =========================================================================
# Revisa lo que el usuario pregunta ANTES de mandarlo al cerebro principal.
# Pensado para uso familiar (con niños de por medio): si detecta contenido
# claramente inapropiado o peligroso, Zora responde con un mensaje neutral
# en vez de dejar pasar la pregunta al modelo de conversación.
# Si la key no está configurada, o el filtro falla por cualquier motivo,
# NO bloqueamos nada — dejamos pasar el mensaje normal (fail-open), para
# que un problema de red nunca deje a Zora sin poder responder nada.

NVIDIA_SAFETY_MODEL = "meta/llama-guard-4-12b"


def check_content_safety(text: str) -> dict:
    """Devuelve {"safe": True} si no hay filtro configurado o el mensaje
    pasa, o {"safe": False, "category": ...} si el filtro lo marca unsafe.
    Nunca lanza excepción: cualquier fallo se trata como "safe" (fail-open)."""
    if not NVIDIA_API_KEY:
        return {"safe": True}

    body = json.dumps({
        "model": NVIDIA_SAFETY_MODEL,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 20,
    }).encode()
    req = urllib.request.Request(
        NVIDIA_CHAT_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {NVIDIA_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            verdict = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip().lower()
            if verdict.startswith("unsafe"):
                category = verdict.split("\n")[1].strip() if "\n" in verdict else ""
                return {"safe": False, "category": category}
            return {"safe": True}
    except Exception:  # noqa: BLE001 — fail-open: nunca bloqueamos por un error de red
        return {"safe": True}


# =========================================================================
# 9.65b CLIMA (Open-Meteo — gratis, sin key, sin registro, sin cuota)
# =========================================================================
# Open-Meteo ofrece dos APIs públicas y gratuitas: geocoding (nombre de
# ciudad -> lat/lon) y forecast (pronóstico). No pide API key ni cuenta.
# Sirve para "qué clima hace en <ciudad>", "va a llover en <ciudad>", etc.

WMO_CODE_ES = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado",
    3: "nublado", 45: "niebla", 48: "niebla con escarcha",
    51: "llovizna ligera", 53: "llovizna moderada", 55: "llovizna intensa",
    56: "llovizna helada ligera", 57: "llovizna helada intensa",
    61: "lluvia ligera", 63: "lluvia moderada", 65: "lluvia intensa",
    66: "lluvia helada ligera", 67: "lluvia helada intensa",
    71: "nieve ligera", 73: "nieve moderada", 75: "nieve intensa",
    77: "granizo de nieve", 80: "chubascos ligeros", 81: "chubascos moderados",
    82: "chubascos violentos", 85: "chubascos de nieve ligeros",
    86: "chubascos de nieve intensos", 95: "tormenta",
    96: "tormenta con granizo ligero", 99: "tormenta con granizo intenso",
}


def get_weather(location: str) -> str:
    """Devuelve un texto corto con el clima actual y el pronóstico del día
    para la ciudad dada. Usa Open-Meteo (gratis, sin key)."""
    location = location.strip().rstrip(".,;?!")
    if not location:
        return "Dime un lugar, por ejemplo: \"qué clima hace en Madrid\"."

    # 1. Geocoding: nombre -> lat/lon (con nombre bonito para responder)
    geo_url = ("https://geocoding-api.open-meteo.com/v1/search?name="
               + urllib.parse.quote(location) + "&count=1&language=es&format=json")
    try:
        with urllib.request.urlopen(geo_url, timeout=10) as resp:
            geo = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError):
        return "No pude consultar el servicio de clima ahora mismo (sin internet o caído)."
    results = geo.get("results") or []
    if not results:
        return f"No encontré el lugar \"{location}\". Prueba con el nombre de una ciudad."

    place = results[0]
    lat, lon = place["latitude"], place["longitude"]
    name = place.get("name", location)
    admin = place.get("admin1", "")
    country = place.get("country", "")
    full = name + (f", {admin}" if admin else "") + (f", {country}" if country else "")

    # 2. Forecast: clima actual + máx/mín + probabilidad de lluvia del día
    fc_url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        f"weather_code,wind_speed_10m"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&timezone=auto&forecast_days=1"
    )
    try:
        with urllib.request.urlopen(fc_url, timeout=10) as resp:
            fc = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError):
        return "No pude obtener el pronóstico ahora mismo (sin internet o caído)."

    cur = fc.get("current", {})
    daily = fc.get("daily", {})
    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    hum = cur.get("relative_humidity_2m")
    wind = cur.get("wind_speed_10m")
    code = cur.get("weather_code")
    desc = WMO_CODE_ES.get(code, "condición desconocida")
    tmax = (daily.get("temperature_2m_max") or [None])[0]
    tmin = (daily.get("temperature_2m_min") or [None])[0]
    rain_prob = (daily.get("precipitation_probability_max") or [None])[0]

    parts = [f"Clima en {full}: {desc}"]
    if temp is not None:
        parts.append(f"{temp}°C" + (f" (sensación {feels}°C)" if feels is not None else ""))
    if tmax is not None and tmin is not None:
        parts.append(f"máx {tmax}°C / mín {tmin}°C")
    if hum is not None:
        parts.append(f"humedad {hum}%")
    if wind is not None:
        parts.append(f"viento {wind} km/h")
    if rain_prob is not None:
        parts.append(f"probabilidad de lluvia {rain_prob}%")
    return ". ".join(parts) + "."


WEATHER_PATTERN = re.compile(
    r"\b(?:qu[eé]\s+(?:clima|tiempo)\s+(?:hace|va)\s+(?:en|de|para)\s+"
    r"|c[oó]mo\s+(?:est[aá]|va)\s+el\s+(?:clima|tiempo)\s+(?:en|de)\s+"
    r"|(?:clima|tiempo)\s+(?:en|de)\s+"
    r"|va\s+a\s+(?:llover|nevar)\s+(?:en|de)\s+)"
    r"(.+)",
    re.IGNORECASE,
)


# =========================================================================
# 9.7 BÚSQUEDA WEB (DuckDuckGo Instant Answer — gratis, sin key, sin registro)
# =========================================================================
# DuckDuckGo devuelve respuestas rápidas a preguntas de hecho, definiciones,
# noticias recientes, datos científicos, deportes, etc — sin pasar por la
# IA (mucho más rápido y naturalmente ideado para información factual).
# Se usa para: "busca X", "qué es X", "dime sobre X", "quién es X", "buscar X".

def web_search(query: str) -> str:
    """Devuelve un texto conciso sobre X usando DuckDuckGo Instant Answer
    (sin HTML, sin resultado numerado, solo lo esencial)."""
    query = query.strip().rstrip(". ,;")
    if not query:
        return "Dime algo que buscar, por ejemplo: 'busca inteligencia artificial'."

    url = ("https://api.duckduckgo.com/?q=" + urllib.parse.quote(query)
           + "&format=json&no_html=1&skip_disambig=1&media=0&safe_strict=0")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError):
        return "No pude buscar en internet ahora mismo (sin conexión o site caído)."

    heading = data.get("Heading", "")
    abstract = data.get("AbstractText", "")
    answer = data.get("Answer")  # calculadoras, fechas exactas
    related_text = data.get("RelatedTopics", [])
    sources = data.get("Source", "")
    entities = data.get("Entities", [])
    images = data.get("Images", [])
    external_link = data.get("ExternalLink", "")
    sound = data.get("Sound")

    parts = []
    if answer is not None:
        # Calculadora, fecha exacta, etc — responde directo
        return str(answer) + "."

    if heading:
        parts.append(f"**{heading}**")
    if abstract:
        # Corta si es muy largo, más compacto
        body = abstract if len(abstract) < 250 else abstract[:240] + "..."
        parts.append(body.replace("\n", " "))
    if entities and len(entities) > 0:
        # Estructuras con subdatos: ciudades, aeropuertos, etc.
        for entity in entities[:2]:
            if entity.get("Type") and entity.get("Text"):
                parts.append(f"*{entity['Type']}: {entity['Text']}*")
    if external_link:
        parts.append(f" fuente: {external_link}")

    if related_text and len(related_text) > 1:
        opts = ", ".join(f"• {r.get('Text', '')}" for r in related_text[:4])
        parts.append(f"• otras búsquedas: {opts}...")

    if not parts:
        parts.append("DuckDuckGo no ha encontrado resultados claros para: " + query + ".")

    return ". ".join(parts) + "."


# Antes este patrón era tan amplio que se comía frases como "dime qué hora
# es" (las mandaba a DuckDuckGo en vez del comando rápido o al cerebro).
# Ahora solo dispara con intenciones de búsqueda claras.
WEB_SEARCH_PATTERN = re.compile(
    r"\b(?:busca(?:r|me)?|busco|dime\s+sobre|cu[eé]ntame\s+(?:sobre|de)|"
    r"qu[eé]\s+es|qui[eé]n\s+(?:es|fue)|noticias?\s+(?:de|sobre))\s+"
    r"(?:en\s+internet\s+)?(.+)",
    re.IGNORECASE,
)


# =========================================================================
# 9.8 CALCULADORA (ast segura + math — requiere numérico, offline, sin key)
# =========================================================================
# Detecta si el mensaje es una expresión matemática — responde con el
# resultado exacto, sin IA. "2+2" = 4, "15% de 230" = 69, "sqrt(16)" = 4,
# "raiz(25)" = 5, "100*0.5" = 50, etc.

# Nodos AST permitidos en la evaluación segura
_ALLOWED_AST_NODES = {
    ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
    ast.UAdd, ast.USub, ast.Call, ast.Name, ast.Attribute, ast.Load,
}

# Funciones matemáticas permitidas en el namespace de eval
_SAFE_MATH_NAMES = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "sqrt": math.sqrt, "raiz": math.sqrt, "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "exp": math.exp,
    "pi": math.pi, "e": math.e,
    "ceil": math.ceil, "floor": math.floor,
    "degrees": math.degrees, "radians": math.radians,
}

def safe_eval_expr(expr: str) -> str:
    """Evalúa expresión matemática de forma segura usando ast. Devuelve
    resultado formateado o mensaje de error amigable."""
    expr = expr.strip()
    if not expr:
        return "Dime una expresión, por ejemplo: 2+2, sqrt(16), 15% de 230, 100*0.5."
    if len(expr) > 200:
        return "Expresión demasiado larga. Intenta con algo más corto."

    try:
        tree = ast.parse(expr, mode="eval")

        # Validar que solo use nodos permitidos
        for node in ast.walk(tree):
            if type(node) not in _ALLOWED_AST_NODES:
                return "Expresión no permitida. Solo operaciones matemáticas básicas."
            # Bloquear llamadas a funciones no permitidas
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id not in _SAFE_MATH_NAMES:
                    return f"Función '{node.func.id}' no permitida. Usa: sqrt, sin, cos, log, abs, etc."
            if isinstance(node, ast.Name) and node.id not in _SAFE_MATH_NAMES:
                return f"Nombre '{node.id}' no permitido. Solo constantes y funciones matemáticas."

        # Evaluar en namespace seguro
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, _SAFE_MATH_NAMES)
        if isinstance(result, (int, float)):
            # Formatear: entero si es .0, sino decimal limpio
            if isinstance(result, float) and result.is_integer():
                return f"{int(result)}."
            return f"{result:.10g}."
        return "Resultado no numérico."

    except SyntaxError:
        return "Sintaxis inválida. Ejemplos: 2+2*3, sqrt(16), 15% de 230, 100*0.5."
    except ZeroDivisionError:
        return "Error: división por cero."
    except Exception:
        return "Error en la expresión. Usa números y operaciones: + - * / // % ** sqrt() abs() etc."


CALC_PATTERN = re.compile(
    r"^\s*(?:calcula|cu[aá]nto es|eval[u]?a|resultado de)?\s*"
    r"([0-9\.\s\+\-\*/\%\(\)]+)"
    r"(?:\s*(?:porcentaje|por ciento|%)\s+de\s*([0-9\.]+))?\s*$",
    re.IGNORECASE,
)

# Patrón más amplio para detectar expresiones matemáticas en texto libre
MATH_EXPR_PATTERN = re.compile(
    r"\b(\d+(?:\.\d+)?\s*[\+\-\*/]\s*\d+(?:\.\d+)?(?:\s*[\+\-\*/]\s*\d+(?:\.\d+)?)*)\b"
)

# Expresiones con función matemática: "sqrt(16)", "raiz(25)", "sin(30)"
# (el patrón plano de arriba no puede capturarlas porque las funciones
# tienen letras).
CALC_FUNC_PATTERN = re.compile(
    r"\b(sqrt|ra[ií]z|raiz|sin|cos|tan|log|log10|abs|round|ceil|floor|exp)\s*\(([^()]+)\)",
    re.IGNORECASE,
)


# =========================================================================
# 9.9 LISTA DE COMPRAS / NOTAS (SQLite local, compartida entre dispositivos)
# =========================================================================
# Una tabla en zora.db donde todos los dispositivos ven la misma lista.
# Comandos: "agrega a la lista: leche, pan", "quita leche de la lista",
# "muestra la lista", "borra la lista".

def _ensure_shopping_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shopping_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT NOT NULL,
            added_by TEXT,
            added_at REAL DEFAULT (strftime('%s','now'))
        )
    """)
    conn.commit()
    conn.close()

_ensure_shopping_table()

def get_shopping_list(user_id: str = None) -> list:
    """La lista es de toda la familia (compartida entre dispositivos), así
    que user_id se mantiene por compatibilidad pero no filtra resultados."""
    conn = get_db()
    rows = conn.execute(
        "SELECT item, added_by, added_at FROM shopping_list ORDER BY added_at DESC"
    ).fetchall()
    conn.close()
    return [{"item": r["item"], "added_by": r["added_by"], "added_at": r["added_at"]} for r in rows]

def add_shopping_item(item: str, user_id: str = None):
    conn = get_db()
    conn.execute(
        "INSERT INTO shopping_list (item, added_by) VALUES (?, ?)",
        (item.strip(), user_id or "desconocido"),
    )
    conn.commit()
    conn.close()

def remove_shopping_item(item: str):
    conn = get_db()
    conn.execute("DELETE FROM shopping_list WHERE item=?", (item.strip(),))
    conn.commit()
    conn.close()

def clear_shopping_list():
    conn = get_db()
    conn.execute("DELETE FROM shopping_list")
    conn.commit()
    conn.close()


# El verbo "pon" SOLO cuenta si menciona la lista de forma explícita
# ("pon arroz en la lista") — sin ese requisito, "pon música rock"
# terminaba agregando "música rock" a las compras.
SHOPPING_ADD_EXPLICIT_PATTERN = re.compile(
    r"\b(?:agrega|añade|pon|incluye|anota|mete)\s+(.+?)\s+(?:a|en)\s+la\s+lista\b",
    re.IGNORECASE,
)
SHOPPING_ADD_PATTERN = re.compile(
    r"\b(?:agrega|añade|incluye|anota)\s+(?:a\s+la\s+lista|en\s+la\s+lista)?\s*[:]?\s*(.+)",
    re.IGNORECASE,
)
# Acepta las tres formas naturales: "quita pan", "quita pan de la lista"
# y "quita de la lista pan" (el patrón viejo solo entendía la primera).
SHOPPING_REMOVE_PATTERN = re.compile(
    r"\b(?:quita|elimina|borra|saca)(?:\s+de\s+la\s+lista)?\s+(.+?)(?:\s+de\s+la\s+lista)?\s*[.!]?\s*$",
    re.IGNORECASE,
)
SHOPPING_LIST_PATTERN = re.compile(
    r"\b(?:muestra|ver|lista|ense[ñn]ame)\s+(?:la\s+)?(?:lista|compras|notas)\b",
    re.IGNORECASE,
)
SHOPPING_CLEAR_PATTERN = re.compile(
    r"\b(?:borra|limpia|vac[ií]a)\s+(?:la\s+)?(?:lista|compras|notas)\b",
    re.IGNORECASE,
)


# =========================================================================
# 9.10 ALARMAS / TEMPORIZADORES / RECORDATORIOS (persistentes y CON aviso)
# =========================================================================
# Antes las alarmas vivían en una lista en memoria y sus callbacks eran
# "pass": Zora prometía avisar pero el timer disparaba en silencio, y todo
# se perdía al reiniciar. Ahora:
#   - Cada alarma/timer/recordatorio se guarda en la tabla `reminders` de
#     zora.db (sobrevive reinicios del servidor).
#   - Un hilo revisa cada 2 segundos si algo venció; al vencer lo marca,
#     deja una alerta en el historial (GET /alerts) y, si era diaria,
#     agenda el día siguiente.
#   - El cliente consulta GET /notifications cada pocos segundos: ahí
#     llegan los recordatorios ya vencidos QUE TODAVÍA NO LE HAN SIDO
#     ENTREGADOS a ese usuario (tabla reminder_deliveries), para que la
#     app los muestre y los lea en voz alta una sola vez.
#
# Comandos: "alarma a las 07:30", "timer 10 minutos", "recuérdame llamar a
# mamá a las 18:00", "avísame en 5 minutos", "qué alarmas tengo",
# "cancela mis alarmas".

_alarm_thread = None
_alarm_stop_event = threading.Event()


def start_alarm_scheduler():
    global _alarm_thread
    if _alarm_thread is None or not _alarm_thread.is_alive():
        _alarm_stop_event.clear()
        _alarm_thread = threading.Thread(target=_reminder_worker, daemon=True)
        _alarm_thread.start()


def create_reminder(user_id: str, message: str, trigger_ts: float,
                    device_id=None, repeat_daily: bool = False) -> str:
    reminder_id = secrets.token_hex(8)
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO reminders (id, user_id, device_id, message, trigger_ts, repeat_daily, fired) "
            "VALUES (?,?,?,?,?,?,0)",
            (reminder_id, user_id, device_id, message, trigger_ts, int(repeat_daily)),
        )
        conn.commit()
        conn.close()
    start_alarm_scheduler()  # por si el hilo todavía no corre
    return reminder_id


def list_pending_reminders(user_id: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT message, trigger_ts, repeat_daily FROM reminders "
        "WHERE user_id=? AND fired=0 AND trigger_ts>? ORDER BY trigger_ts LIMIT 20",
        (user_id, time.time()),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_user_reminders(user_id: str) -> int:
    """Borra las alarmas/timers aún no disparados de este usuario.
    Devuelve cuántos borró."""
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT id FROM reminders WHERE user_id=? AND fired=0", (user_id,)
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM reminders WHERE id=?", (r["id"],))
        conn.commit()
        conn.close()
    return len(rows)


def _reminder_worker():
    """Hilo que dispara los recordatorios vencidos. La fuente de verdad es
    la base de datos — por eso sobrevive reinicios sin 'recargar' nada."""
    while not _alarm_stop_event.is_set():
        try:
            now = time.time()
            with _db_lock:
                conn = get_db()
                try:
                    due = conn.execute(
                        "SELECT * FROM reminders WHERE fired=0 AND trigger_ts<=?", (now,)
                    ).fetchall()
                    for r in due:
                        conn.execute("UPDATE reminders SET fired=1 WHERE id=?", (r["id"],))
                        alert_id = secrets.token_hex(8)
                        _insert_alert(conn, alert_id, r["user_id"], r["device_id"],
                                      "reminder", r["message"])
                        if r["repeat_daily"]:
                            conn.execute(
                                "INSERT INTO reminders (id, user_id, device_id, message, trigger_ts, repeat_daily, fired) "
                                "VALUES (?,?,?,?,?,1,0)",
                                (secrets.token_hex(8), r["user_id"], r["device_id"],
                                 r["message"], r["trigger_ts"] + 86400),
                            )
                    conn.commit()
                finally:
                    # Cerrar SIEMPRE la conexión: si un ciclo falla a mitad,
                    # una conexión abandonada con transacción abierta deja la
                    # base bloqueada para todos los demás.
                    conn.close()
        except Exception:  # noqa: BLE001 — un ciclo fallido no mata el hilo
            pass
        time.sleep(2)


def pop_due_notifications(user_id: str) -> list:
    """Devuelve los recordatorios ya vencidos que este usuario todavía NO ha
    recibido, y los marca como entregados (cada uno se notifica UNA vez)."""
    out = []
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, message, trigger_ts FROM reminders "
            "WHERE user_id=? AND fired=1 AND id NOT IN "
            "(SELECT reminder_id FROM reminder_deliveries WHERE user_id=?) "
            "ORDER BY trigger_ts LIMIT 20",
            (user_id, user_id),
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO reminder_deliveries (reminder_id, user_id, delivered_ts) VALUES (?,?,?)",
                (r["id"], user_id, time.time()),
            )
            out.append({"id": r["id"], "message": r["message"], "trigger_ts": r["trigger_ts"]})
        conn.commit()
        conn.close()
    return out


def parse_time_to_seconds(text: str) -> float:
    """Convierte '10 minutos', '1 hora', '30 segundos', 'a las 14:30' a segundos desde ahora."""
    text = text.strip().lower()
    # "a las HH:MM" o "a las H:MM"
    m = re.search(r"a\s+las\s+(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        now = time.localtime()
        target = time.mktime(time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, h, mi, 0, 0, 0, -1)))
        if target <= time.time():
            target += 86400  # mañana
        return target - time.time()
    # "en X minutos/horas/segundos" o "X minutos/horas/segundos"
    m = re.search(r"(?:en\s+)?(\d+(?:\.\d+)?)\s*(minutos?|mins?|horas?|hrs?|segundos?|secs?)", text)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        if unit.startswith("min"):
            return val * 60
        if unit.startswith("hor") or unit.startswith("hr"):
            return val * 3600
        return val
    # Por defecto 0
    return 0


_TIME_ABS_RE = re.compile(r"a\s+las?\s*\d{1,2}:\d{2}", re.IGNORECASE)
_TIME_REL_RE = re.compile(
    r"\b(?:en\s+)?\d+(?:\.\d+)?\s*(?:minutos?|mins?|horas?|hrs?|segundos?|secs?)\b",
    re.IGNORECASE,
)


def split_task_from_time(text: str):
    """Separa 'llamar a mamá a las 18:00' -> (segundos_hasta_entonces, 'llamar a mamá').
    Si no encuentra expresión de tiempo devuelve (0, '')."""
    seconds = parse_time_to_seconds(text)
    if seconds <= 0:
        return 0, ""
    task = _TIME_ABS_RE.sub("", text)
    task = _TIME_REL_RE.sub("", task)
    task = re.sub(r"\s{2,}", " ", task).strip(" ,.")
    return seconds, task


ALARM_PATTERN = re.compile(
    r"\b(?:alarma(?:\s+a\s+las)?|despertador|av[ií]same|recu[eé]rdame|pon(?:go)?\s+alarma)\s+(?:a\s+las\s+|en\s+|para\s+|que\s+)?(.+)",
    re.IGNORECASE,
)
TIMER_PATTERN = re.compile(
    r"\b(?:timer|cron[oó]metro|cuenta\s+atr[aá]s)\s+(\d+(?:[.,]\d+)?)\s*(minutos?|mins?|horas?|hrs?|segundos?|secs?)",
    re.IGNORECASE,
)
ALARM_LIST_PATTERN = re.compile(
    r"\bqu[eé]\s+alarmas?\s+tengo\b|\bmis\s+alarmas?\b|\bpr[oó]ximas?\s+alarmas?\b",
    re.IGNORECASE,
)
ALARM_CANCEL_PATTERN = re.compile(
    r"\bcancela(?:r)?\s+(?:mis\s+)?(?:alarmas?|timers?|temporizador(es)?|recordatorios?)\b",
    re.IGNORECASE,
)


# =========================================================================
# 9.11 MONITOR DEL SISTEMA (psutil opcional — offline, degradación grácil)
# =========================================================================
# "cómo va el sistema", "estado del sistema", "cpu", "memoria", "batería",
# "disco", "temperatura" (si hay sensor).

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

def get_system_status() -> str:
    if not _PSUTIL_AVAILABLE:
        return ("psutil no está instalado. Para monitor del sistema: "
                "pip install psutil (o apt install python3-psutil).")
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        parts = [f"CPU: {cpu:.0f}%"]
        parts.append(f"RAM: {mem.percent:.0f}% ({mem.used//1024//1024}MB/{mem.total//1024//1024}MB)")
        parts.append(f"Disco: {disk.percent:.0f}% ({disk.used//1024//1024//1024}GB/{disk.total//1024//1024//1024}GB)")
        # Batería (si laptop)
        batt = psutil.sensors_battery()
        if batt:
            status = "cargando" if batt.power_plugged else "desconectado"
            parts.append(f"Batería: {batt.percent:.0f}% ({status})")
        # Temperaturas (si disponible)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    for e in entries:
                        if e.current:
                            parts.append(f"Temp {name}: {e.current:.0f}°C")
                            break
        except Exception:  # noqa: BLE001
            pass
        return ". ".join(parts) + "."
    except Exception as e:  # noqa: BLE001
        return f"No pude leer el estado del sistema: {e}"


SYSTEM_STATUS_PATTERN = re.compile(
    r"\b(?:c[oó]mo\s+va\s+el\s+sistema|estado\s+(?:del\s+)?sistema|"
    r"cpu|memoria|ram|bater[ií]a|disco|temperatura)\b",
    re.IGNORECASE,
)


# =========================================================================
# 9.12 COTIZACIONES (gratis, sin key, sin registro)
# - Monedas fiat: open.er-api.com (tasas del BCE actualizadas diariamente).
#   (Antes se usaba exchangerate.host, que desde finales de 2024 exige una
#   key de pago — por eso cambié el proveedor.)
# - Cripto (BTC, ETH, etc.): CoinGecko API pública gratuita.
# Los nombres en español ("dolares", "euros", "pesos") se traducen a su
# código ISO con la tabla de alias de abajo (la normalización de acentos
# la hace _no_accents, definida arriba junto a los comandos rápidos).

CURRENCY_ALIASES = {
    "usd": "USD", "dolar": "USD", "dolares": "USD",
    "dolar americano": "USD", "dolares americanos": "USD",
    "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "gbp": "GBP", "libra": "GBP", "libras": "GBP",
    "libra esterlina": "GBP", "libras esterlinas": "GBP",
    "jpy": "JPY", "yen": "JPY", "yenes": "JPY",
    "mxn": "MXN", "peso mexicano": "MXN", "pesos mexicanos": "MXN",
    "cop": "COP", "peso colombiano": "COP", "pesos colombianos": "COP",
    "ars": "ARS", "peso argentino": "ARS", "pesos argentinos": "ARS",
    "clp": "CLP", "peso chileno": "CLP", "pesos chilenos": "CLP",
    # "pesos" a secas: lo interpretamos como mexicanos (el caso más común);
    # si quieres otra variante, dilo completo ("pesos colombianos").
    "peso": "MXN", "pesos": "MXN",
    "chf": "CHF", "franco suizo": "CHF", "francos suizos": "CHF",
    "franco": "CHF", "francos": "CHF",
    "cny": "CNY", "yuan": "CNY", "yuanes": "CNY",
    "brl": "BRL", "real": "BRL", "reales": "BRL",
    "real brasileño": "BRL", "reales brasileños": "BRL",
    "pen": "PEN", "sol peruano": "PEN", "soles peruanos": "PEN",
    "soles": "PEN", "sol": "PEN",
    "cad": "CAD", "dolar canadiense": "CAD", "dolares canadienses": "CAD",
    "aud": "AUD", "dolar australiano": "AUD", "dolares australianos": "AUD",
    "crc": "CRC", "colon": "CRC", "colones": "CRC",
    "inr": "INR", "rupia": "INR", "rupias": "INR",
}

CRYPTO_IDS = {
    "btc": "bitcoin", "bitcoin": "bitcoin", "bitcoins": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum", "ethers": "ethereum",
    "usdt": "tether", "tether": "tether",
    "bnb": "binancecoin", "binance coin": "binancecoin",
    "xrp": "ripple", "ripple": "ripple",
    "doge": "dogecoin", "dogecoin": "dogecoin",
    "ada": "cardano", "cardano": "cardano",
    "ltc": "litecoin", "litecoin": "litecoin",
    "dot": "polkadot", "polkadot": "polkadot",
    "link": "chainlink", "chainlink": "chainlink",
}


def _normalize_currency(name: str):
    """Devuelve el código ISO ('USD', 'EUR'...) o None si no reconoce el nombre."""
    key = _no_accents(name.strip().lower())
    return CURRENCY_ALIASES.get(key)


def _normalize_crypto(name: str):
    key = _no_accents(name.strip().lower())
    return CRYPTO_IDS.get(key)


def _coingecko_price(crypto_id: str, vs: str = "usd"):
    """Precio actual de una cripto contra una moneda fiat, o None si falla."""
    url = ("https://api.coingecko.com/api/v3/simple/price?ids="
           + crypto_id + "&vs_currencies=" + vs)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError):
        return None
    return data.get(crypto_id, {}).get(vs)


def get_exchange_rate(from_currency: str, to_currency: str, amount: float = 1.0) -> str:
    from_code = _normalize_currency(from_currency)
    to_code = _normalize_currency(to_currency)
    from_crypto = _normalize_crypto(from_currency)
    to_crypto = _normalize_crypto(to_currency)

    # --- Cripto involucrada: CoinGecko ---
    if from_crypto or to_crypto:
        if from_crypto and to_crypto:
            # cripto -> cripto: cruzamos ambas contra USD
            p_from = _coingecko_price(from_crypto)
            p_to = _coingecko_price(to_crypto)
            if not p_from or not p_to:
                return f"No encontré precios para {from_currency} -> {to_currency}."
            converted = amount * p_from / p_to
            return f"{amount:g} {from_currency.upper()} = {converted:.8g} {to_currency.upper()} (vía USD)."

        if from_crypto:  # cripto -> fiat
            price = _coingecko_price(from_crypto, (to_code or "USD").lower())
            if not price:
                return f"No encontré el precio de {from_currency}."
            return f"{amount:g} {from_currency.upper()} = {amount * price:.4g} {(to_code or 'USD')}."

        # fiat -> cripto
        price = _coingecko_price(to_crypto, (from_code or "USD").lower())
        if not price:
            return f"No encontré el precio de {to_currency}."
        converted = amount / price
        label = next((k for k, v in CRYPTO_IDS.items() if v == to_crypto), to_crypto)
        return f"{amount:g} {(from_code or 'USD')} = {converted:.8g} {label.upper()} (~{price:g} c/u)."

    # --- Solo fiat: open.er-api.com ---
    if not from_code or not to_code:
        desconocido = from_currency if not from_code else to_currency
        return f"No reconozco la moneda \"{desconocido}\". Prueba con dolares, euros, pesos, libras..."
    url = f"https://open.er-api.com/v6/latest/{from_code}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError):
        return "No pude consultar la cotización ahora mismo (sin internet o servicio caído)."
    rates = data.get("rates") or {}
    rate = rates.get(to_code)
    if not rate:
        return f"No hay cotización para {from_code} -> {to_code}."
    converted = amount * rate
    return f"{amount:g} {from_code} = {converted:.4g} {to_code} (tasa: {rate:.4g})."


EXCHANGE_PATTERN = re.compile(
    r"\b(?:cu[aá]nto\s+(?:es|vale|cuesta)\s+)?(\d+(?:\.\d+)?)\s+(\w+)\s+(?:en|a|to|hacia)\s+(\w+)\b",
    re.IGNORECASE,
)
EXCHANGE_SIMPLE_PATTERN = re.compile(
    r"\b(?:cotizaci[oó]n|precio|tipo\s+de\s+cambio)\s+(?:del?\s+)?(\w+)\s+(?:a|en|/|contra)\s+(\w+)\b",
    re.IGNORECASE,
)
# "precio del bitcoin" / "cuánto vale ethereum" a secas -> en USD
EXCHANGE_CRYPTO_SINGLE_PATTERN = re.compile(
    r"\b(?:precio|valor|cu[aá]nto\s+(?:est[aá]|vale|cuesta))\s+(?:del?\s+)?"
    r"(" + "|".join(sorted(CRYPTO_IDS, key=len, reverse=True)) + r")\b\s*$",
    re.IGNORECASE,
)

# =========================================================================
# 9.7. DEPORTES (TheSportsDB — gratis, sin registro, sin cuota estricta)
# =========================================================================
# TheSportsDB ofrece una key pública gratuita ("123") que ya funciona sin
# que hagas nada — no necesita registro ni cuenta. Si algún día quieres tu
# propia key (por si la pública se satura), la pones en THESPORTSDB_API_KEY
# en zora.env y Zora la usa en su lugar automáticamente.

def thesportsdb_key() -> str:
    return _real_env_key("THESPORTSDB_API_KEY") or "123"


def get_last_match_text(team_name: str) -> str:
    key = thesportsdb_key()
    search_url = (f"https://www.thesportsdb.com/api/v1/json/{key}/searchteams.php"
                  f"?t={urllib.parse.quote(team_name)}")
    try:
        with urllib.request.urlopen(search_url, timeout=10) as resp:
            search_data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return f"No pude consultar el servicio de deportes ahora mismo: {e}"

    teams = search_data.get("teams") or []
    if not teams:
        return f"No encontré ningún equipo llamado \"{team_name}\"."

    team = teams[0]
    team_id = team["idTeam"]
    events_url = f"https://www.thesportsdb.com/api/v1/json/{key}/eventslast.php?id={team_id}"
    try:
        with urllib.request.urlopen(events_url, timeout=10) as resp:
            events_data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return f"No pude consultar el último partido: {e}"

    results = events_data.get("results") or []
    if not results:
        return f"No encontré partidos recientes de {team['strTeam']}."

    ev = results[0]
    home, away = ev.get("strHomeTeam", "?"), ev.get("strAwayTeam", "?")
    hs, aws = ev.get("intHomeScore"), ev.get("intAwayScore")
    marcador = f"{hs}-{aws}" if hs is not None and aws is not None else "(sin marcador registrado)"
    fecha = ev.get("dateEvent", "fecha desconocida")
    return f"Último partido de {team['strTeam']}: {home} {marcador} {away}, el {fecha}."


SPORTS_PATTERN = re.compile(r"\bc[oó]mo\s+va\s+(?:el\s+|la\s+)?(.+)", re.IGNORECASE)


# =========================================================================
# 9.7b MODO ESTUDIO (colegio): quizzes, flashcards, resúmenes y explicaciones
# =========================================================================
# Zora como compañero de estudio:
#   - "explícame la fotosíntesis fácil" -> explicación sencilla con ejemplos.
#   - "resume este texto: ..."          -> resumen + 3 ideas clave.
#   - "hazme un quiz de fracciones"     -> 5 preguntas de opción múltiple; se
#       responde por letra (a/b/c/d o 1-4), se corrige al instante con
#       explicación y score final. La sesión queda guardada en la BD.
#   - "crea flashcards de historia" / "pruébame" -> mazos persistentes; el
#       repaso prioriza las tarjetas que más se han fallado.
#
# Las sesiones de quiz viven en la tabla quiz_sessions; los mazos en
# flashcard_decks/flashcards (con estadísticas de aciertos). Las sesiones
# activas "de conversación" (a qué tarjeta vas, etc.) van en memoria: si el
# servidor se reinicia a mitad de un repaso, no se pierde nada importante.

STUDY_SYSTEM_PROMPT = (
    "Eres Zora, ayudante de estudio para estudiantes de colegio. Explicas en "
    "español sencillo, claro y cálido, con ejemplos de la vida diaria, sin "
    "jerga innecesaria y sin inventarte datos. Cuando te pidan responder solo "
    "con JSON, respondes EXCLUSIVAMENTE el JSON pedido: sin texto extra, sin "
    "saludos y sin bloques de código markdown."
)

STUDY_SESSIONS = {}  # user_id -> {"mode": "quiz"|"flashcards", ...} sesión en curso

# Gancho SOLO para pruebas automatizadas (ZORA_FAKE_LLM=1): devuelve
# respuestas deterministas sin llamar a ningún proveedor real, así el modo
# estudio se puede probar punta a punta sin gastar créditos de las APIs.
FAKE_LLM = os.environ.get("ZORA_FAKE_LLM") == "1"


def _fake_llm_response(user_text: str) -> str:
    t = user_text.lower()
    if '"correcta"' in t or "opción múltiple" in t or "opcion multiple" in t:
        qs = []
        for i in range(1, 6):
            qs.append({
                "pregunta": f"Pregunta de prueba {i}: ¿cuánto es {i}+{i}?",
                "opciones": {"a": str(i + i + 1), "b": str(i + i), "c": str(i * i), "d": str(i)},
                "correcta": "b",
                "explicacion": f"{i}+{i}={2*i}, siempre.",
            })
        return json.dumps(qs, ensure_ascii=False)
    if '"frente"' in t or "flashcard" in t:
        cards = [{"frente": f"¿Concepto de prueba {i}?", "reverso": f"Definición de prueba {i}."}
                 for i in range(1, 5)]
        return json.dumps(cards, ensure_ascii=False)
    return "Explicación de prueba: esto es una respuesta simulada para pruebas."


def _anthropic_chat(messages: list, system: str, max_tokens: int):
    body = json.dumps({
        "model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
        "system": system, "messages": messages,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
            return "\n".join(parts) if parts else None
    except Exception:  # noqa: BLE001 — deja que la cadena pruebe el siguiente proveedor
        return None


def _openrouter_chat(messages: list, system: str, max_tokens: int):
    if not OPENROUTER_API_KEY:
        return None
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_CHAT_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                 "HTTP-Referer": "https://zora.local", "X-Title": "Zora"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return text or None
    except Exception:  # noqa: BLE001
        return None


def _nvidia_chat(messages: list, system: str, max_tokens: int):
    if not NVIDIA_API_KEY or usage_exhausted("cerebro_nvidia"):
        return None
    body = json.dumps({
        "model": get_current_model(),
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        NVIDIA_CHAT_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {NVIDIA_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            increment_usage("cerebro_nvidia")
            return text or None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            increment_usage("cerebro_nvidia", mark_exhausted=True)
        return None
    except Exception:  # noqa: BLE001
        return None


def llm_complete(user_text: str, system: str = None, max_tokens: int = 800):
    """Una llamada LLM de un turno para funciones concretas (estudio,
    resúmenes...). NO toca el historial de chat del usuario. Recorre la misma
    cadena de respaldo que el cerebro principal (Claude -> OpenRouter ->
    NVIDIA) y devuelve texto o None si todos fallan."""
    if FAKE_LLM:
        return _fake_llm_response(user_text)
    system = system or STUDY_SYSTEM_PROMPT
    messages = [{"role": "user", "content": user_text}]
    answer = None
    if ANTHROPIC_API_KEY:
        answer = _anthropic_chat(messages, system, max_tokens)
    if answer is None and OPENROUTER_API_KEY:
        answer = _openrouter_chat(messages, system, max_tokens)
    if answer is None and NVIDIA_API_KEY:
        answer = _nvidia_chat(messages, system, max_tokens)
    return answer


def _extract_json(text: str, want_list: bool = True):
    """Saca el primer array/objeto JSON de una respuesta del modelo, tolerando
    cercos de código markdown y texto alrededor."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?|```", "", text)
    opener, closer = ("[", "]") if want_list else ("{", "}")
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                    return parsed if isinstance(parsed, list if want_list else dict) else None
                except (ValueError, TypeError):
                    return None
    return None


# ---- Quizzes ----

QUIZ_START_PATTERN = re.compile(
    r"\bhaz(?:me)?\s+un\s+(?:quiz|examen|test)(?:\s+(?:de|sobre)\s+(.+))?",
    re.IGNORECASE,
)
QUIZ_QUIT_PATTERN = re.compile(
    r"\b(?:cancela|termina|abandona|para)\s+(?:el\s+)?(?:quiz|examen|test)\b|\bsalir\s+del\s+quiz\b",
    re.IGNORECASE,
)
QUIZ_STATUS_PATTERN = re.compile(
    r"\b(?:c[oó]mo\s+voy|cu[aá]nto\s+llevo)\s+(?:en\s+)?(?:el\s+)?(?:quiz|examen|test)\b",
    re.IGNORECASE,
)
_LETTER_BY_DIGIT = {"1": "a", "2": "b", "3": "c", "4": "d"}
QUIZ_ANSWER_PATTERN = re.compile(
    r"^\s*(?:pregunta\s*\d+\s*[:.\-]?\s*)?([1-4abcd])\s*[.)]?\s*$",
    re.IGNORECASE,
)


def _generate_quiz_questions(topic: str, n: int = 5):
    prompt = (
        'Genera un quiz de ' + str(n) + ' preguntas de opción múltiple sobre "' + topic +
        '" para un estudiante de colegio. Nivel medio, exactamente 4 opciones '
        '(a,b,c,d), una sola correcta. Responde SOLO con JSON válido: una lista '
        'así: [{"pregunta": "...", "opciones": {"a": "...", "b": "...", "c": "...", "d": "..."}, '
        '"correcta": "a", "explicacion": "por qué breve"}]'
    )
    raw = llm_complete(prompt, max_tokens=1200)
    items = _extract_json(raw, want_list=True)
    if not isinstance(items, list):
        return None
    clean = []
    for it in items[:n]:
        try:
            q = str(it["pregunta"]).strip()
            opts = {str(k).lower(): str(v).strip() for k, v in it["opciones"].items()}
            correct = str(it["correcta"]).strip().lower()[:1]
            expl = str(it.get("explicacion", "")).strip()
        except (KeyError, TypeError, AttributeError):
            continue
        if not q or correct not in opts or len(opts) < 2:
            continue
        clean.append({"pregunta": q, "opciones": opts, "correcta": correct, "explicacion": expl})
    return clean or None


def _start_quiz(user_id: str, topic: str):
    questions = _generate_quiz_questions(topic)
    if not questions:
        return {"response_text": "No pude armar el quiz ahora mismo (el cerebro en la nube "
                                 "no respondió). Intenta de nuevo en un momento."}
    quiz_id = secrets.token_hex(8)
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO quiz_sessions (id, user_id, topic, questions, answers, total, state, created_at) "
            "VALUES (?,?,?,?,?,?,'active',?)",
            (quiz_id, user_id, topic, json.dumps(questions, ensure_ascii=False),
             json.dumps([], ensure_ascii=False), len(questions), time.time()),
        )
        conn.commit()
        conn.close()
    STUDY_SESSIONS[user_id] = {"mode": "quiz", "quiz_id": quiz_id, "index": 0}
    first = questions[0]
    return {"response_text": format_quiz_question(first, 1, len(questions))}


def format_quiz_question(q: dict, number: int, total: int) -> str:
    lines = [f"📚 Pregunta {number}/{total}: {q['pregunta']}"]
    for letter in sorted(q["opciones"]):
        lines.append(f"   {letter}) {q['opciones'][letter]}")
    lines.append("Respóndeme con la letra (a, b, c o d).")
    return "\n".join(lines)


def get_active_quiz(user_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM quiz_sessions WHERE user_id=? AND state='active' "
        "ORDER BY created_at DESC LIMIT 1", (user_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def handle_quiz_answer(user_id: str, session: dict, answer_raw: str) -> dict:
    quiz = get_active_quiz(user_id)
    if not quiz:
        STUDY_SESSIONS.pop(user_id, None)
        return None
    questions = json.loads(quiz["questions"])
    idx = session.get("index", 0)
    if idx >= len(questions):
        return finish_quiz(user_id, quiz["id"])
    letter = answer_raw.strip().lower()
    letter = _LETTER_BY_DIGIT.get(letter, letter[:1])
    q = questions[idx]
    correct = letter == q["correcta"]
    answers = json.loads(quiz["answers"] or "[]")
    answers.append({"letter": letter, "correct": bool(correct)})
    with _db_lock:
        conn = get_db()
        conn.execute("UPDATE quiz_sessions SET answers=? WHERE id=?",
                     (json.dumps(answers, ensure_ascii=False), quiz["id"]))
        conn.commit()
        conn.close()

    verdict = (f"✅ ¡Correcto! {q['explicacion']}" if correct
               else f"❌ Casi — la respuesta era **{q['correcta']}) {q['opciones'][q['correcta']]}**. "
                    f"{q['explicacion']}")
    nxt = idx + 1
    if nxt >= len(questions):
        result = finish_quiz(user_id, quiz["id"])
        return {"response_text": verdict + "\n\n" + result["response_text"]}
    session["index"] = nxt
    return {"response_text": verdict + "\n\n" +
            format_quiz_question(questions[nxt], nxt + 1, len(questions))}


def finish_quiz(user_id: str, quiz_id: str) -> dict:
    # Se releen las respuestas desde la BD (la copia en memoria del llamador
    # puede estar desactualizada: no incluye la respuesta que acaba de llegar).
    conn = get_db()
    row = conn.execute("SELECT * FROM quiz_sessions WHERE id=?", (quiz_id,)).fetchone()
    conn.close()
    if not row:
        STUDY_SESSIONS.pop(user_id, None)
        return {"response_text": "El quiz ya no está disponible."}
    questions = json.loads(row["questions"])
    answers = json.loads(row["answers"] or "[]")
    score = sum(1 for a in answers if a.get("correct"))
    total = len(questions)
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE quiz_sessions SET score=?, state='done', finished_at=? WHERE id=?",
            (score, time.time(), quiz_id),
        )
        conn.commit()
        conn.close()
    STUDY_SESSIONS.pop(user_id, None)
    pct = round(score * 100 / total) if total else 0
    frase = ("¡Excelente trabajo! 🎉" if pct >= 80 else
             "¡Muy bien! Sigue practicando." if pct >= 60 else
             "Vamos bien, repasa lo fallado y me vuelves a pedir otro quiz. 💪")
    return {"response_text": f"📊 Resultado: {score}/{total} ({pct}%). {frase}"}


# ---- Flashcards ----

FLASHCARDS_CREATE_PATTERN = re.compile(
    r"\b(?:crea|créame|genera|generame|genérame|hazme|haz)\s+(?:unas?\s+)?"
    r"flashcards?\s*(?:de|sobre|para|con)?\s*(.*)",
    re.IGNORECASE,
)
FLASHCARD_TEST_PATTERN = re.compile(
    r"\bpr[uú][eé]bame\s*(?:con\s+)?(?:las?\s+)?(?:flashcards?\s*)?(?:de|sobre)?\s*(.*)",
    re.IGNORECASE,
)
FLASHCARD_DECKS_PATTERN = re.compile(r"\bmis\s+mazos?\b|\bqu[eé]\s+mazos?\s+tengo\b", re.IGNORECASE)
FLASHCARD_DELETE_PATTERN = re.compile(
    r"\bborra(?:r)?\s+(?:el\s+)?mazo\s+(?:de|sobre)?\s*(.+)", re.IGNORECASE)
FLASHCARD_STOP_PATTERN = re.compile(
    r"\b(?:salir|parar|para|suficiente|basta|termina(?:r)?\s+el\s+repaso|ya\s+no)\b", re.IGNORECASE)

# --- OpenCode (agente de código que corre EN TU LAPTOP, vía el agente) ---
OPENCODE_PATTERN = re.compile(
    r"^\s*(?:usa\s+)?open[\s\-]?code\b[:,]?\s*(?:para\s+)?(.*)",
    re.IGNORECASE,
)
AFFIRMATIVE_YES = re.compile(r"^\s*(?:s[ií]|s[ií]\s*,?\s*(?:la\s+)?sab[ií]a|correcto|acert[eé]|la\s+sab[ií]a|bien)\s*[.!]?\s*$", re.IGNORECASE)
AFFIRMATIVE_NO = re.compile(r"^\s*(?:no|nop|no\s+la\s+sab[ií]a|fall[eé]|mal|equivocado)\s*[.!]?\s*$", re.IGNORECASE)


def create_flashcards(user_id: str, topic: str) -> dict:
    prompt = (
        'Crea 6 flashcards de estudio sobre "' + topic + '" para un estudiante de '
        'colegio. Preguntas cortas y concretas, respuestas breves (máximo 2 líneas). '
        'Responde SOLO con JSON válido: lista así: [{"frente": "pregunta", "reverso": "respuesta"}]'
    )
    raw = llm_complete(prompt, max_tokens=1000)
    items = _extract_json(raw, want_list=True)
    if not isinstance(items, list):
        return {"response_text": "No pude crear las flashcards ahora mismo (el cerebro en la nube "
                                 "no respondió). Intenta de nuevo en un momento."}
    cards = []
    for it in items[:8]:
        try:
            front = str(it["frente"]).strip()
            back = str(it["reverso"]).strip()
        except (KeyError, TypeError, AttributeError):
            continue
        if front and back:
            cards.append((front, back))
    if not cards:
        return {"response_text": "El modelo no devolvió tarjetas útiles. Prueba con otro tema."}
    deck_id = secrets.token_hex(6)
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO flashcard_decks (id, user_id, topic, created_at) VALUES (?,?,?,?)",
            (deck_id, user_id, topic.strip() or "general", time.time()),
        )
        for front, back in cards:
            conn.execute(
                "INSERT INTO flashcards (id, deck_id, front, back) VALUES (?,?,?,?)",
                (secrets.token_hex(8), deck_id, front, back),
            )
        conn.commit()
        conn.close()
    return {"response_text": f"✏️ Listo: creé {len(cards)} flashcards de \"{topic}\". "
                             f"Dime \"pruébame\" cuando quieras repasarlas."}


def list_flashcard_decks(user_id: str) -> dict:
    conn = get_db()
    rows = conn.execute(
        "SELECT d.topic, COUNT(c.id) AS cards FROM flashcard_decks d "
        "LEFT JOIN flashcards c ON c.deck_id=d.id WHERE d.user_id=? "
        "GROUP BY d.id ORDER BY d.created_at DESC LIMIT 20", (user_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return {"response_text": "Todavía no tienes mazos. Pídeme algo como \"crea flashcards de la célula\"."}
    parts = ", ".join(f"{r['topic']} ({r['cards']} tarjetas)" for r in rows)
    return {"response_text": "Tus mazos: " + parts + ". Dime \"pruébame flashcards de <tema>\" para repasar."}


def delete_flashcard_deck(user_id: str, topic: str) -> dict:
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT id FROM flashcard_decks WHERE user_id=? AND LOWER(topic)=LOWER(?)",
            (user_id, topic.strip()),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM flashcards WHERE deck_id=?", (r["id"],))
            conn.execute("DELETE FROM flashcard_decks WHERE id=?", (r["id"],))
        conn.commit()
        conn.close()
    if not rows:
        return {"response_text": f"No encontré ningún mazo llamado \"{topic}\"."}
    return {"response_text": f"Borrado el mazo de \"{topic}\"."}


def next_flashcard_for_review(deck_id: str):
    """Prioriza: nunca vistas -> las últimas que fallaron -> menos vistas."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM flashcards WHERE deck_id=? "
        "ORDER BY CASE WHEN last_result IS NULL THEN 0 ELSE 1 END, "
        "last_result ASC, times_shown ASC, RANDOM() LIMIT 1",
        (deck_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_flashcard_deck(user_id: str, topic: str = ""):
    conn = get_db()
    if topic:
        row = conn.execute(
            "SELECT id FROM flashcard_decks WHERE user_id=? AND LOWER(topic) LIKE ? "
            "ORDER BY created_at DESC LIMIT 1", (user_id, "%" + topic.strip().lower() + "%"),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM flashcard_decks WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    conn.close()
    return row["id"] if row else None


def show_next_card(user_id: str, session: dict) -> dict:
    card = next_flashcard_for_review(session["deck_id"])
    if not card:
        STUDY_SESSIONS.pop(user_id, None)
        return {"response_text": "Ese mazo no tiene tarjetas. Crea otro con \"crea flashcards de ...\"."}
    session.update({"card_id": card["id"], "stage": "answered_pending"})
    return {"response_text": f"🃏 {card['front']}\n\n(dime tu respuesta o \"no sé\")"}


def reveal_and_ask(user_id: str, session: dict, attempt: str) -> dict:
    card = get_flashcard(session["card_id"])
    if not card:
        STUDY_SESSIONS.pop(user_id, None)
        return {"response_text": "Se perdió esa tarjeta del repaso. Dime \"pruébame\" para seguir."}
    session["attempt"] = attempt
    session["stage"] = "verdict_pending"
    return {"response_text": f"La respuesta era:\n📖 {card['back']}\n\n¿La sabías? (sí/no)"}


def get_flashcard(card_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM flashcards WHERE id=?", (card_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def record_flashcard_result(session: dict, knew_it: bool) -> dict:
    card = get_flashcard(session["card_id"])
    if card:
        with _db_lock:
            conn = get_db()
            conn.execute(
                "UPDATE flashcards SET times_shown=times_shown+1, times_correct=times_correct+?, "
                "last_result=? WHERE id=?",
                (1 if knew_it else 0, 1 if knew_it else 0, card["id"]),
            )
            conn.commit()
            conn.close()
    session["reviewed"] = session.get("reviewed", 0) + 1
    if knew_it and session.get("streak") is not None:
        session["streak"] += 1
    elif knew_it:
        session["streak"] = 1
    else:
        session["streak"] = 0
    return show_next_card_or_finish(session["user_id"], session)


def show_next_card_or_finish(user_id: str, session: dict) -> dict:
    card = next_flashcard_for_review(session["deck_id"])
    if not card:
        STUDY_SESSIONS.pop(user_id, None)
        reviewed = session.get("reviewed", 0)
        return {"response_text": f"🎉 Repaso terminado: viste {reviewed} tarjeta(s). "
                                 f"Dime \"pruébame\" cuando quieras otra ronda."}
    session.update({"card_id": card["id"], "stage": "answered_pending"})
    session.pop("attempt", None)
    return {"response_text": f"🃏 {card['front']}\n\n(dime tu respuesta o \"no sé\")"}


# ---- Resúmenes y explicaciones simples ----

EXPLAIN_SIMPLE_PATTERN = re.compile(
    r"\bexpl[ií]came\s+(.+)", re.IGNORECASE,
)
SUMMARY_PATTERN = re.compile(
    r"\b(?:res[uú]mem?e|resumeme|haz(?:me)?\s+un\s+resumen|haz\s+un\s+resumen)\s*[:,]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)


def explain_simple(topic: str) -> dict:
    prompt = ('Explícame "' + topic.strip() + '" como para un estudiante de colegio: '
              'en máximo 8 líneas, con un ejemplo cotidiano y sin palabras raras '
              '(si usas un término técnico, defínelo al instante).')
    answer = llm_complete(prompt, max_tokens=500)
    if not answer:
        return {"response_text": "Ahora mismo no puedo conectarme al cerebro en la nube para "
                                 "explicártelo. Intenta de nuevo en un momento."}
    return {"response_text": answer.strip()}


def summarize_text(text: str) -> dict:
    text = text.strip()
    if not text:
        return {"response_text": "Pásame el texto después de \"resume este texto:\" y te lo "
                                 "condenso con sus ideas clave."}
    if len(text) > 6000:
        text = text[:6000]
        note = "(me pasaste mucho texto, tomé los primeros ~6000 caracteres)\n\n"
    else:
        note = ""
    prompt = ('Resume el siguiente texto en máximo 6 líneas claras y luego agrega '
              '"Ideas clave:" con 3 puntos. Texto:\n\n' + text)
    answer = llm_complete(prompt, max_tokens=500)
    if not answer:
        return {"response_text": "No pude resumirlo ahora mismo (el cerebro en la nube no "
                                 "respondió). Intenta de nuevo en un momento."}
    return {"response_text": note + answer.strip()}



HELP_PATTERN = re.compile(
    r"\b(?:qu[eé]\s+(?:puedes|sabes)\s+hacer|todas?\s+tus\s+funciones|"
    r"men[uú]\s+de\s+comandos|lista\s+de\s+comandos|qu[eé]\s+funciones\s+tienes|"
    r"todo\s+lo\s+que\s+puedes\s+hacer)\b",
    re.IGNORECASE,
)

FORGET_PATTERN = re.compile(
    r"\b(?:olvida\s+(?:todo|la\s+conversaci[oó]n|lo\s+que\s+hablamos)|"
    r"borra\s+(?:la\s+)?(?:memoria|conversaci[oó]n|historial)|"
    r"empieza\s+de\s+cero|reinicia\s+(?:la\s+)?conversaci[oó]n)\b",
    re.IGNORECASE,
)


def build_help_text() -> str:
    lines = ["¡Hola! Soy Zora, tu asistente de confianza. Aquí lo que puedo hacer por ti ahora:", ""]
    lines.append("· Rápido, al instante: linterna, subir/bajar volumen, poner/pausar música, decirte la hora.")
    lines.append("· Actividad física: solo dime 'cuántos pasos llevo'.")
    lines.append("· Emergencias: di 'ayuda' o 'emergencia', o el botón SOS — aviso a tus contactos con tu ubicación,"
                 + (" por SMS real" if TEXTBEE_API_KEY else
                    " por correo real" if SMTP_HOST else
                    " aunque todavía no sale el mensaje (falta configurar SMS o correo)"))
    lines.append("· Geocercas: te aviso si entras o sales de una zona que definas, como 'Casa'.")
    lines.append("· Deportes: 'cómo va el Barça' y te doy el último resultado.")
    lines.append("· Clima: 'qué clima hace en Madrid' o 'va a llover en Barcelona'.")
    lines.append("· Web: 'busca inteligencia artificial' y te resumo lo más relevante.")
    lines.append("· Imágenes: pídeme 'hazme una imagen de un perro en la playa' (gratis, sin límite).")

    if NVIDIA_API_KEY:
        lines.append("· Traductor: 'traduce al inglés: hola'.")
        lines.append("· Transcripción de audio: envíame una nota de voz y te digo qué dice.")
        lines.append("· Filtro de seguridad familiar.")

    if ANTHROPIC_API_KEY:
        lines.append("· Conversación abierta: Claude piensa por mí (Claude principal con respaldo a OpenRouter y NVIDIA).")
    elif OPENROUTER_API_KEY:
        lines.append("· Conversación abierta: pienso con OpenRouter (modelos variados) — cae a NVIDIA gratis si algo falla.")
    elif NVIDIA_API_KEY:
        lines.append(f"· Conversación abierta: pienso con mi cerebro de respaldo gratis de NVIDIA "
                     f"(te quedan {get_usage('cerebro_nvidia')['remaining']} usos este mes).")
    else:
        lines.append("· Conversación abierta: todavía no tengo clave de IA (ANTHROPIC_API_KEY, OPENROUTER_API_KEY o NVIDIA_API_KEY) "
                      "— la de NVIDIA es gratis. ¡Avísame si la configuras!")

    lines.append("· Calculadora: '2+2', 'sqrt(16)', '15% de 230'.")
    lines.append("· Lista de compras: 'agrega leche a la lista', 'quita pan', 'muéstrame la lista'.")
    lines.append("· Alarmas y temporizadores (avisan de verdad): 'alarma a las 7:30', "
                 "'timer 10 minutos', 'recuérdame llamar a mamá a las 18:00'.")
    lines.append("· Sistema: 'cómo va el sistema', 'estado del sistema'.")
    lines.append("· Cambio de moneda: '100 dólares en euros', 'precio del bitcoin'.")
    lines.append("· Modo estudio: 'explícame la fotosíntesis fácil', 'resume este texto: ...', "
                 "'hazme un quiz de fracciones', 'crea flashcards de historia' y 'pruébame'.")
    lines.append("· OpenCode: 'opencode crea un script que...' — su agente de código "
                 "trabaja en tu laptop y te traigo el resultado.")
    laptops = list_user_laptops(None)  # sin usuario: sección genérica
    lines.append("· Control de tu computadora: 'sube el volumen', 'abre spotify', "
                 "'bloquea la pantalla' — de verdad, vía el agente en tu PC. "
                 "Si aún no la conectaste: botón '+ PC' arriba y corre "
                 "zora_laptop_agent.py allí con el token que te dé.")

    lines.append("")
    lines.append("¡Y si mandas comandos a tu laptop, también controlo apps, volumen y Spotify de verdad allí! ¿Algo más que quieras saber?")
    return "\n".join(lines)


# =========================================================================
# 10. SERVIDOR HTTP (endpoints)
# =========================================================================

class ZoraHandler(BaseHTTPRequestHandler):

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def _bearer_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
        return None

    def _query_params(self):
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        return dict(p.split("=", 1) for p in query.split("&") if "=" in p)

    # ---- Rutas ----

    def do_POST(self):
        try:
            route = self.path.split("?", 1)[0]
            handlers = {
                "/register": self._handle_register,
                "/login": self._handle_login,
                "/devices": self._handle_register_device,
                "/command": self._handle_command,
                "/agent/report": self._handle_agent_report,
                "/contacts": self._handle_add_contact,
                "/location": self._handle_post_location,
                "/geofences": self._handle_add_geofence,
                "/sos": self._handle_sos,
                "/activity": self._handle_add_activity,
                "/transcribe": self._handle_transcribe,
            }
            handler = handlers.get(route)
            if not handler:
                return self._send_json(404, {"error": "ruta no encontrada"})
            handler()
        except PermissionError as e:
            self._send_json(401, {"error": str(e)})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 (prototipo)
            self._send_json(500, {"error": f"error interno: {e}"})

    def do_DELETE(self):
        try:
            route = self.path.split("?", 1)[0]
            if route == "/contacts":
                return self._handle_delete_contact()
            if route == "/geofences":
                return self._handle_delete_geofence()
            self._send_json(404, {"error": "ruta no encontrada"})
        except PermissionError as e:
            self._send_json(401, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"error interno: {e}"})

    def do_GET(self):
        try:
            route = self.path.split("?", 1)[0]
            if route == "/devices":
                return self._handle_list_devices()
            if route == "/health":
                return self._send_json(200, {"status": "ok"})
            if route == "/agent/poll":
                return self._handle_agent_poll()
            if route == "/command/status":
                return self._handle_command_status()
            if route == "/contacts":
                return self._handle_list_contacts()
            if route == "/geofences":
                return self._handle_list_geofences()
            if route == "/location/last":
                return self._handle_get_location()
            if route == "/alerts":
                return self._handle_list_alerts()
            if route == "/sos/history":
                return self._handle_sos_history()
            if route == "/activity/today":
                return self._handle_get_activity()
            if route == "/images":
                return self._handle_get_image()
            if route == "/usage":
                return self._handle_get_usage()
            if route == "/notifications":
                return self._handle_notifications()
            if route == "/reminders":
                return self._handle_list_reminders()
            if route == "/tts":
                return self._handle_tts()
            if route == "/" or route in STATIC_FILES:
                return self._handle_static(route)
            if route == "/descargas":
                return self._handle_downloads_page()
            if route.startswith("/downloads/"):
                return self._handle_download_file(route[len("/downloads/"):])
            self._send_json(404, {"error": "ruta no encontrada"})
        except PermissionError as e:
            self._send_json(401, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": f"error interno: {e}"})

    # ---- Cuentas / dispositivos ----

    def _handle_register(self):
        data = self._read_json()
        result = create_user(data["username"], data["password"])
        self._send_json(201, result)

    def _handle_login(self):
        data = self._read_json()
        token = login(data["username"], data["password"])
        self._send_json(200, {"token": token})

    def _handle_register_device(self):
        user_id = user_from_token(self._bearer_token())
        data = self._read_json()
        result = register_device(data["name"], data["type"], user_id)
        self._send_json(201, result)

    def _handle_list_devices(self):
        user_id = user_from_token(self._bearer_token())
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM devices WHERE shared=1 OR owner_id=?", (user_id,)
        ).fetchall()
        conn.close()
        visible = {r["device_id"]: {k: r[k] for k in r.keys()} for r in rows}
        self._send_json(200, {"devices": visible})

    # ---- Comandos ----

    def _handle_command(self):
        user_id = user_from_token(self._bearer_token())
        data = self._read_json()
        text = data.get("text", "")
        device_id = data.get("device_id")

        def brain(response_text: str):
            """Respuesta tipo cerebro (el cliente ya sabe mostrarla)."""
            return self._send_json(200, {"type": "cloud_brain",
                                         "result": {"response_text": response_text}})

        if device_id and not can_control_device(user_id, device_id):
            return self._send_json(403, {"error": "No tienes permiso para controlar ese dispositivo"})

        # Prioridad máxima: si el texto suena a emergencia, no pasa por
        # comandos rápidos ni por el cerebro en la nube — dispara el SOS.
        if SOS_PATTERN.search(text.strip().lower()):
            result = trigger_sos(user_id, device_id, None, None, extra_message=text)
            return self._send_json(200, {"type": "sos_triggered", "result": result})

        # "Zora, usa el modelo <nombre>" / "qué modelo tienes" / "qué
        # modelos hay" — cambia el cerebro NVIDIA sin reiniciar el backend.
        model_outcome = try_model_settings_command(text)
        if model_outcome:
            return brain(model_outcome["response_text"])

        # "Qué puedes hacer": Zora se explica sola, reflejando en vivo qué
        # tiene configurado (con key real) y qué no.
        if HELP_PATTERN.search(text.strip().lower()):
            return brain(build_help_text())

        # "Olvida todo": borra el historial de conversación de este usuario.
        if FORGET_PATTERN.search(text.strip().lower()):
            clear_chat_history(user_id)
            return brain("Listo, borré lo que hablamos. Empezamos de cero.")

        # --- SESIÓN DE ESTUDIO ACTIVA: intercepta las respuestas del quiz
        # y del repaso de flashcards ANTES de cualquier otra regla ---
        session = STUDY_SESSIONS.get(user_id)
        if session and session.get("mode") == "quiz":
            if QUIZ_QUIT_PATTERN.search(text.strip().lower()):
                quiz = get_active_quiz(user_id)
                if quiz:
                    with _db_lock:
                        conn = get_db()
                        conn.execute(
                            "UPDATE quiz_sessions SET state='cancelled', finished_at=? WHERE id=?",
                            (time.time(), quiz["id"]),
                        )
                        conn.commit()
                        conn.close()
                STUDY_SESSIONS.pop(user_id, None)
                return brain("Quiz cancelado. Cuando quieras, me pides otro.")
            ans_match = QUIZ_ANSWER_PATTERN.match(text.strip())
            if ans_match:
                outcome = handle_quiz_answer(user_id, session, ans_match.group(1))
                return brain(outcome["response_text"])
            return brain("Estamos en pleno quiz — respóndeme con la letra (a, b, c o d), "
                         "o dime 'cancela el quiz' si quieres parar.")

        if session and session.get("mode") == "flashcards":
            normalized = text.strip().lower()
            if FLASHCARD_STOP_PATTERN.fullmatch(normalized):
                reviewed = session.get("reviewed", 0)
                STUDY_SESSIONS.pop(user_id, None)
                return brain(f"Repaso detenido: viste {reviewed} tarjeta(s). "
                             f"Dime \"pruébame\" cuando quieras seguir.")
            if session.get("stage") == "verdict_pending":
                if AFFIRMATIVE_YES.match(normalized):
                    return brain(record_flashcard_result(session, True)["response_text"])
                if AFFIRMATIVE_NO.match(normalized):
                    return brain(record_flashcard_result(session, False)["response_text"])
                return brain("¿La sabías o no? Respóndeme \"sí\" o \"no\" (o \"salir\").")
            # stage answered_pending: lo que escriba cuenta como su intento
            return brain(reveal_and_ask(user_id, session, text.strip())["response_text"])

        # --- COMANDOS RÁPIDOS (instantáneos, sin IA) — van primero porque
        # sus patrones son específicos; así nada se los roba por el camino ---
        quick = try_quick_command(text)
        if quick:
            if quick["action"] == "get_activity_stats":
                if not device_id:
                    return self._send_json(200, {"type": "quick_command",
                                                  "result": {**quick, "note": "Elige un dispositivo para ver sus pasos"}})
                quick["stats"] = get_activity_today(device_id)
                return self._send_json(200, {"type": "quick_command",
                                              "latency": "instantánea (sin IA)", "result": quick})

            # La linterna vive en el CELULAR y aún no hay agente de teléfonos:
            # respuesta honesta en vez de fingir éxito.
            if quick["action"] in ("flashlight_on", "flashlight_off"):
                return brain("La linterna es del celular y todavía no tengo un "
                             "agente para teléfonos — lo siento. En tu computadora "
                             "sí puedo hacer cosas de verdad: 'sube el volumen', "
                             "'abre spotify', 'bloquea la pantalla'...")

            device = get_device(device_id) if device_id else None
            if device and device.get("type") == "laptop":
                command_id = enqueue_command(device_id, quick["action"], quick)
                return self._send_json(202, {"type": "queued_for_agent",
                                              "command_id": command_id, "action": quick["action"]})
            if device:
                return brain("Elegiste un dispositivo que no es computadora y esa "
                             "acción solo la puedo hacer allí donde corre el agente "
                             "de Zora. Selecciona tu laptop en el menú de arriba.")

            if quick["action"] in DEVICE_BOUND_ACTIONS or quick["action"] == "open_app" \
                    or quick["action"] == "lock_screen":
                laptops = list_user_laptops(user_id)
                online = [l for l in laptops if l["online"]]
            if len(online) == 1:
                # Una sola laptop conectada: se enruta sola, sin fricción.
                command_id = enqueue_command(online[0]["device_id"], quick["action"], quick)
                return self._send_json(202, {"type": "queued_for_agent",
                                              "command_id": command_id,
                                              "action": quick["action"]})
            if len(laptops) > 1:
                nombres = ", ".join(l["name"] for l in laptops)
                return brain(f"Tienes varias computadoras ({nombres}) — elige una "
                             f"en el menú de arriba y repito tu comando.")
            if laptops:
                # Registrada pero sin agente corriendo (o sin poll reciente):
                # mensaje específico en vez del genérico "no tienes ninguna".
                nombres = ", ".join(l["name"] for l in laptops)
                return brain(f"Tu computadora ({nombres}) está registrada pero su "
                             f"agente no está corriendo ahora mismo. Ábrelo allí "
                             f"con 'python zora_laptop_agent.py' y en segundos me "
                             f"doy cuenta de que está en línea.")
            return brain(COMO_CONECTAR_PC)

            return self._send_json(200, {"type": "quick_command",
                                          "latency": "instantánea (sin IA)", "result": quick})

        # --- MONITOR DEL SISTEMA (antes que deportes: "cómo va el sistema"
        # era secuestrado por el patrón de deportes "cómo va <equipo>") ---
        if SYSTEM_STATUS_PATTERN.search(text.strip()):
            return brain(get_system_status())

        # Deportes: "cómo va <equipo>"
        sports_match = SPORTS_PATTERN.search(text.strip())
        if sports_match:
            return brain(get_last_match_text(sports_match.group(1).strip()))

        # Clima: "qué clima hace en <ciudad>" / "tiempo en <ciudad>" / "va a llover en ..."
        weather_match = WEATHER_PATTERN.search(text.strip())
        if weather_match:
            return brain(get_weather(weather_match.group(1).strip()))

        # --- CALCULADORA (un solo bloque, offline e instantáneo) ---
        # Expresiones con función: "sqrt(16)", "raiz(25)", "sin(30)"
        calc_text = text.strip()
        func_match = CALC_FUNC_PATTERN.search(calc_text)
        if func_match and len(calc_text) <= 80:
            fn = {"raíz": "sqrt", "raiz": "sqrt"}.get(func_match.group(1).lower(),
                                                      func_match.group(1).lower())
            answer = safe_eval_expr(f"{fn}({func_match.group(2)})")
            if answer and not answer.startswith(("Dime", "Expresión", "Sintaxis", "Error")):
                return self._send_json(200, {"type": "instant_calculation",
                                              "result": {"text": answer}})
        # Expresiones planas: "2+2", "15% de 230", "cuánto es 45*3"
        calc_match = CALC_PATTERN.search(calc_text)
        if calc_match:
            prefix = calc_text[:calc_match.start()].lower()
            allowed_prefix = calc_text.lower().startswith(
                ("calcula", "cuánto es", "cuanto es", "evalúa", "evalua", "resultado de"))
            if allowed_prefix or not any(ch.isalpha() for ch in prefix):
                pct = re.search(
                    r"(\d+(?:[.,]\d+)?)\s*(?:%|por\s*ciento|porcentaje)\s+(?:de|del)\s+(\d+(?:[.,]\d+)?)",
                    calc_text)
                if pct:
                    expr = f"({pct.group(1).replace(',', '.')})/100*{pct.group(2).replace(',', '.')}"
                elif calc_match.lastindex and calc_match.group(2):
                    expr = f"{calc_match.group(1)}*{calc_match.group(2)}/100"
                else:
                    expr = calc_match.group(1)
                answer = safe_eval_expr(expr)
                if answer and not answer.startswith(("Dime", "Expresión", "Sintaxis", "Error")):
                    return self._send_json(200, {"type": "instant_calculation",
                                                  "result": {"text": answer}})

        # --- COTIZACIONES (fiat vía open.er-api.com, cripto vía CoinGecko) ---
        exchange_match = EXCHANGE_PATTERN.search(calc_text)
        if exchange_match:
            amount = float(exchange_match.group(1))
            answer = get_exchange_rate(exchange_match.group(2), exchange_match.group(3), amount)
            return self._send_json(200, {"type": "fast_search", "result":
                {"source": "open.er-api.com", "from": exchange_match.group(2),
                 "to": exchange_match.group(3), "amount": amount, "text": answer}})
        simple_exchange = EXCHANGE_SIMPLE_PATTERN.search(calc_text)
        if simple_exchange:
            answer = get_exchange_rate(simple_exchange.group(1), simple_exchange.group(2), 1.0)
            return self._send_json(200, {"type": "fast_search", "result":
                {"source": "open.er-api.com", "from": simple_exchange.group(1),
                 "to": simple_exchange.group(2), "text": answer}})
        crypto_single = EXCHANGE_CRYPTO_SINGLE_PATTERN.search(calc_text.rstrip(".!? "))
        if crypto_single:
            answer = get_exchange_rate(crypto_single.group(1), "USD", 1.0)
            return self._send_json(200, {"type": "fast_search", "result":
                {"source": "CoinGecko", "crypto": crypto_single.group(1), "text": answer}})

        # Búsqueda web: "busca X", "qué es X", "dime sobre X", "quién es X"
        web_match = WEB_SEARCH_PATTERN.search(calc_text)
        if web_match:
            query = web_match.group(1).strip()
            answer = web_search(query)
            return self._send_json(200, {"type": "fast_search", "result":
                {"source": "DuckDuckGo Instant Answer", "query": query, "text": answer}})

        # --- ALARMAS / TEMPORIZADORES / RECORDATORIOS (persistentes) ---
        # Cancelar va ANTES que listar: si no, "cancela mis alarmas" sería
        # capturado por el patrón de listar ("mis alarmas").
        if ALARM_CANCEL_PATTERN.search(calc_text.lower()):
            n = cancel_user_reminders(user_id)
            if n:
                return brain(f"Listo, cancelé {n} alarma(s)/recordatorio(s) pendientes.")
            return brain("No tenías ninguna alarma pendiente por cancelar.")
        if ALARM_LIST_PATTERN.search(calc_text.lower()):
            pendientes = list_pending_reminders(user_id)
            if not pendientes:
                return brain("No tienes alarmas ni recordatorios pendientes.")
            lines = []
            for p in pendientes:
                hora = time.strftime("%H:%M", time.localtime(p["trigger_ts"]))
                dia = "" if p["repeat_daily"] else ""
                lines.append(f"· {hora} — {p['message']}" + (" (todos los días)" if p["repeat_daily"] else ""))
            return brain("Tienes programado:\n" + "\n".join(lines))
        timer_match = TIMER_PATTERN.search(calc_text)
        if timer_match:
            cantidad, unidad = timer_match.group(1).replace(",", "."), timer_match.group(2)
            duration = parse_time_to_seconds(f"{cantidad} {unidad}")
            if duration > 0:
                trigger_ts = time.time() + duration
                create_reminder(user_id,
                                f"⏰ ¡Tiempo! Terminó tu temporizador de {cantidad} {unidad}.",
                                trigger_ts, device_id=device_id)
                hora = time.strftime("%H:%M", time.localtime(trigger_ts))
                return self._send_json(200, {"type": "alarm_result", "result":
                    {"message": f"⏰ Temporizador puesto: {cantidad} {unidad}. "
                                f"Aviso a las {hora} — te enteras aunque estés en otra pestaña."}})
        alarm_match = ALARM_PATTERN.search(calc_text)
        if alarm_match:
            when_raw = alarm_match.group(1).strip()
            duration, task = split_task_from_time(when_raw)
            if duration > 0:
                trigger_ts = time.time() + duration
                hora = time.strftime("%H:%M", time.localtime(trigger_ts))
                if task:
                    mensaje = f"⏰ Recordatorio: {task}."
                    respuesta = (f"⏰ Hecho: te recuerdo \"{task}\" a las {hora} "
                                 f"(y te aparece como aviso en la app).")
                else:
                    mensaje = f"⏰ ¡Alarma! Son las {hora}."
                    respuesta = f"⏰ Alarma programada para las {hora}. Te aviso por la app."
                create_reminder(user_id, mensaje, trigger_ts, device_id=device_id)
                return self._send_json(200, {"type": "alarm_result",
                                              "result": {"message": respuesta}})
            # Pidió un recordatorio pero sin hora entendible
            return brain(f"¿Para cuándo te recuerdo \"{when_raw}\"? Dime algo como "
                         f"\"a las 18:00\" o \"en 20 minutos\".")

        # --- LISTA DE COMPRAS / NOTAS ---
        add_explicit = SHOPPING_ADD_EXPLICIT_PATTERN.search(calc_text)
        add_general = SHOPPING_ADD_PATTERN.search(calc_text)
        if add_explicit or add_general:
            item = (add_explicit or add_general).group(1).strip()
            add_shopping_item(item, user_id)
            return self._send_json(200, {"type": "shopping", "result":
                {"action": "added", "item": item,
                 "message": f"✓ Añadido a la lista: {item}. ¿Algo más?"}})
        remove_match = SHOPPING_REMOVE_PATTERN.search(calc_text)
        if SHOPPING_LIST_PATTERN.search(calc_text):
            items = get_shopping_list(user_id)
            if not items:
                return self._send_json(200, {"type": "shopping", "result":
                    {"items": [], "message": "La lista está vacía por ahora."}})
            nombres = ", ".join(i["item"] for i in items)
            return self._send_json(200, {"type": "shopping", "result":
                {"items": [i["item"] for i in items],
                 "message": f"Tu lista: {nombres}. ¿Algo más que agregar?"}})
        if SHOPPING_CLEAR_PATTERN.search(calc_text):
            clear_shopping_list()
            return self._send_json(200, {"type": "shopping", "result":
                {"action": "cleared", "message": "✓ Lista vaciada. Empezamos de cero."}})
        # Remover va DESPUÉS de ver/limpiar: si no, "borra la lista" sería
        # interpretado como quitar el ítem "la lista".
        if remove_match:
            item = remove_match.group(1).strip()
            # "quita de la lista el pan" -> guardamos "pan" sin artículo
            item = re.sub(r"^(?:el|la|los|las)\s+", "", item).strip()
            remove_shopping_item(item)
            return self._send_json(200, {"type": "shopping", "result":
                {"action": "removed", "item": item,
                 "message": f"✓ Quitado de la lista: {item}. ¿La revisamos?"}})

        # Pedido de traducción: modelo aparte, propio flujo (no gasta la
        # cuota del cerebro de conversación ni pasa por comandos rápidos).
        translate_match = TRANSLATE_PATTERN.search(calc_text)
        if translate_match:
            target_lang = (translate_match.group(1) or "inglés").strip()
            phrase = translate_match.group(2).strip()
            outcome = translate_text(phrase, target_lang)
            if outcome["type"] == "ok":
                return brain(outcome["text"])
            if outcome["type"] == "no_key":
                return brain("No tengo configurada la NVIDIA_API_KEY todavía "
                             "(la misma que usas para el cerebro de respaldo, "
                             "gratis en build.nvidia.com).")
            return brain(f"No pude traducir: {outcome.get('message', 'error desconocido')}")

        # --- MODO ESTUDIO ---
        explain_match = EXPLAIN_SIMPLE_PATTERN.search(calc_text)
        if explain_match:
            return brain(explain_simple(explain_match.group(1))["response_text"])

        summary_match = SUMMARY_PATTERN.search(calc_text)
        if summary_match:
            return brain(summarize_text(summary_match.group(1))["response_text"])

        quiz_start = QUIZ_START_PATTERN.search(calc_text)
        if quiz_start:
            topic = (quiz_start.group(1) or "cultura general").strip()
            outcome = _start_quiz(user_id, topic)
            return brain(outcome["response_text"])

        decks_match = FLASHCARD_DECKS_PATTERN.search(calc_text.lower())
        if decks_match:
            return brain(list_flashcard_decks(user_id)["response_text"])

        deck_delete = FLASHCARD_DELETE_PATTERN.search(calc_text)
        if deck_delete:
            return brain(delete_flashcard_deck(user_id, deck_delete.group(1))["response_text"])

        cards_create = FLASHCARDS_CREATE_PATTERN.search(calc_text)
        if cards_create:
            topic = cards_create.group(1).strip()
            if not topic:
                return brain("¿De qué tema quieres las flashcards? Por ejemplo: "
                             "\"crea flashcards de la tabla periódica\".")
            return brain(create_flashcards(user_id, topic)["response_text"])

        cards_test = FLASHCARD_TEST_PATTERN.search(calc_text)
        if cards_test:
            topic = cards_test.group(1).strip()
            deck_id = find_flashcard_deck(user_id, topic)
            if not deck_id:
                return brain(f"No tengo un mazo{' de ' + topic if topic else ''}. "
                             f"Crea uno con \"crea flashcards{' de ' + topic if topic else ''}\".")
            STUDY_SESSIONS[user_id] = {"mode": "flashcards", "deck_id": deck_id,
                                        "user_id": user_id, "stage": "answered_pending",
                                        "reviewed": 0}
            return brain(show_next_card(user_id, STUDY_SESSIONS[user_id])["response_text"])

        # --- OPENCODE: tareas de código ejecutadas por el agente en tu laptop ---
        oc_match = OPENCODE_PATTERN.search(calc_text)
        if oc_match:
            task = oc_match.group(1).strip()
            if not task:
                return brain("Dime qué quieres que haga opencode, por ejemplo: "
                             "\"opencode crea un script que ordene una lista\".")
            target = None
            if device_id:
                d = get_device(device_id)
                if d and d["type"] in ("laptop", "celular"):
                    target = device_id
            if not target:
                conn = get_db()
                row = conn.execute(
                    "SELECT device_id FROM devices WHERE owner_id=? AND type='laptop' "
                    "ORDER BY last_seen DESC LIMIT 1", (user_id,),
                ).fetchone()
                conn.close()
                target = row["device_id"] if row else None
            if not target:
                return brain("Para usar opencode necesito tu laptop: regístrala "
                             "(POST /devices con type=laptop) y ten corriendo el agente.")
            command_id = enqueue_command(target, "opencode_run", {
                "action": "opencode_run", "param": task, "raw_text": text})
            return self._send_json(202, {
                "type": "queued_for_agent",
                "command_id": command_id,
                "action": "opencode_run",
                "note": "opencode está trabajando en tu laptop; te traigo el resultado cuando termine",
            })

        # "sí" después de que Zora sugirió una imagen: generarla de verdad
        # en vez de dejar que el chat general la invente (ver bug de hoy).
        if AFFIRMATIVE_PATTERN.match(calc_text):
            history = get_chat_history(user_id, limit=1)
            last_assistant = next((h["content"] for h in reversed(history) if h["role"] == "assistant"), None)
            offered_prompt = extract_offered_image_prompt(last_assistant) if last_assistant else None
            if offered_prompt:
                outcome = generate_image(offered_prompt)
                if outcome["type"] == "ok":
                    set_last_image(user_id, outcome["image_id"], offered_prompt)
                    return self._send_json(200, {
                        "type": "image_generated",
                        "result": {"image_url": f"/images?id={outcome['image_id']}", "prompt": offered_prompt},
                    })
                return brain(f"No pude generar la imagen: {outcome.get('message', 'error desconocido')}")
            # Si no había ninguna imagen ofrecida, seguimos el flujo normal.

        # Pedido de imagen: tampoco pasa por comandos rápidos normales ni
        # por el cerebro de texto — es su propio flujo.
        image_match = IMAGE_REQUEST_PATTERN.search(calc_text)
        if image_match:
            prompt = image_match.group(1).strip()
            outcome = generate_image(prompt)
            if outcome["type"] == "ok":
                set_last_image(user_id, outcome["image_id"], prompt)
                return self._send_json(200, {
                    "type": "image_generated",
                    "result": {
                        "image_url": f"/images?id={outcome['image_id']}",
                        "prompt": prompt,
                    },
                })
            return brain(f"No pude generar la imagen: {outcome.get('message', 'error desconocido')}")

        # "dame/muéstrame la imagen" (sin describir una nueva): repite la
        # última que Zora generó de verdad para este usuario.
        if REPEAT_IMAGE_PATTERN.match(calc_text):
            last = get_last_image(user_id)
            if last:
                return self._send_json(200, {
                    "type": "image_generated",
                    "result": {
                        "image_url": f"/images?id={last['image_id']}",
                        "prompt": last["prompt"],
                    },
                })
            return brain("Todavía no te he generado ninguna imagen — pídeme algo como "
                         "\"hazme una imagen de un perro en la playa\" y con gusto la creo.")

        # Sin dispositivo elegido o dispositivo no-laptop: cerebro en la nube
        # (con filtro familiar antes de gastar tokens).
        device = get_device(device_id) if device_id else None
        if device and device["type"] == "laptop":
            command_id = enqueue_command(device_id, "raw_text", {"text": text})
            return self._send_json(202, {
                "type": "queued_for_agent",
                "command_id": command_id,
                "note": "El agente de la laptop lo recogerá en su próximo poll",
            })
        safety = check_content_safety(calc_text)
        if not safety["safe"]:
            return brain("Esa pregunta toca un tema que prefiero no responder aquí. "
                         "Si es algo serio, mejor habla con un adulto de confianza.")
        answer = call_cloud_brain(text, user_id=user_id)
        return brain(answer)

    def _handle_agent_poll(self):
        params = self._query_params()
        device_token = params.get("device_token", "")
        device_id = device_from_token(device_token)

        with _db_lock:
            conn = get_db()
            conn.execute("UPDATE devices SET online=1, last_seen=? WHERE device_id=?", (time.time(), device_id))
            conn.commit()
            conn.close()

        pending = COMMAND_QUEUES.get(device_id, [])
        next_command = pending.pop(0) if pending else None
        self._send_json(200, {"command": next_command})

    def _handle_agent_report(self):
        data = self._read_json()
        device_from_token(data.get("device_token", ""))  # solo valida que el token existe
        command_id = data["command_id"]
        COMMAND_RESULTS[command_id] = {"status": "done", "result": data.get("result")}
        self._send_json(200, {"ok": True})

    def _handle_command_status(self):
        params = self._query_params()
        command_id = params.get("command_id", "")
        status = COMMAND_RESULTS.get(command_id, {"status": "not_found", "result": None})
        self._send_json(200, status)

    # ---- Contactos de confianza ----

    def _handle_add_contact(self):
        user_id = user_from_token(self._bearer_token())
        data = self._read_json()
        contact_id = secrets.token_hex(6)
        with _db_lock:
            conn = get_db()
            conn.execute(
                "INSERT INTO trusted_contacts (id, owner_id, name, phone, email) VALUES (?,?,?,?,?)",
                (contact_id, user_id, data["name"], data.get("phone"), data.get("email")),
            )
            conn.commit()
            conn.close()
        self._send_json(201, {"id": contact_id, "name": data["name"],
                               "phone": data.get("phone"), "email": data.get("email")})

    def _handle_list_contacts(self):
        user_id = user_from_token(self._bearer_token())
        conn = get_db()
        rows = conn.execute("SELECT * FROM trusted_contacts WHERE owner_id=?", (user_id,)).fetchall()
        conn.close()
        self._send_json(200, {"contacts": [dict(r) for r in rows]})

    def _handle_delete_contact(self):
        user_id = user_from_token(self._bearer_token())
        contact_id = self._query_params().get("id")
        with _db_lock:
            conn = get_db()
            conn.execute("DELETE FROM trusted_contacts WHERE id=? AND owner_id=?", (contact_id, user_id))
            conn.commit()
            conn.close()
        self._send_json(200, {"ok": True})

    # ---- Ubicación y geocercas ----

    def _handle_post_location(self):
        data = self._read_json()
        device_id = data.get("device_id")
        device_token = data.get("device_token")
        if device_token:
            device_id = device_from_token(device_token)
        else:
            user_from_token(self._bearer_token())  # solo valida sesión
        if not device_id:
            raise ValueError("Falta device_id o device_token")
        alerts = update_location(device_id, float(data["lat"]), float(data["lon"]))
        self._send_json(200, {"ok": True, "new_alerts": alerts})

    def _handle_get_location(self):
        user_id = user_from_token(self._bearer_token())
        device_id = self._query_params().get("device_id")
        if not can_control_device(user_id, device_id):
            return self._send_json(403, {"error": "No tienes permiso para ver ese dispositivo"})
        loc = get_last_location(device_id)
        self._send_json(200, {"location": loc})

    def _handle_add_geofence(self):
        user_id = user_from_token(self._bearer_token())
        data = self._read_json()
        fence_id = secrets.token_hex(6)
        with _db_lock:
            conn = get_db()
            conn.execute(
                "INSERT INTO geofences (id, owner_id, name, lat, lon, radius_m, device_id) VALUES (?,?,?,?,?,?,?)",
                (fence_id, user_id, data["name"], float(data["lat"]), float(data["lon"]),
                 float(data["radius_m"]), data.get("device_id")),
            )
            conn.commit()
            conn.close()
        self._send_json(201, {"id": fence_id, **data})

    def _handle_list_geofences(self):
        user_id = user_from_token(self._bearer_token())
        conn = get_db()
        rows = conn.execute("SELECT * FROM geofences WHERE owner_id=?", (user_id,)).fetchall()
        conn.close()
        self._send_json(200, {"geofences": [dict(r) for r in rows]})

    def _handle_delete_geofence(self):
        user_id = user_from_token(self._bearer_token())
        fence_id = self._query_params().get("id")
        with _db_lock:
            conn = get_db()
            conn.execute("DELETE FROM geofences WHERE id=? AND owner_id=?", (fence_id, user_id))
            conn.commit()
            conn.close()
        self._send_json(200, {"ok": True})

    def _handle_list_alerts(self):
        user_id = user_from_token(self._bearer_token())
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM alerts WHERE owner_id=? ORDER BY ts DESC LIMIT 50", (user_id,)
        ).fetchall()
        conn.close()
        self._send_json(200, {"alerts": [dict(r) for r in rows]})

    # ---- Emergencias / SOS ----

    def _handle_sos(self):
        user_id = user_from_token(self._bearer_token())
        data = self._read_json()
        result = trigger_sos(
            user_id, data.get("device_id"), data.get("lat"), data.get("lon"),
            extra_message=data.get("message", ""),
        )
        self._send_json(200, result)

    def _handle_sos_history(self):
        user_id = user_from_token(self._bearer_token())
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM sos_events WHERE user_id=? ORDER BY ts DESC LIMIT 20", (user_id,)
        ).fetchall()
        conn.close()
        self._send_json(200, {"events": [dict(r) for r in rows]})

    # ---- Actividad física ----

    def _handle_add_activity(self):
        data = self._read_json()
        device_id = data.get("device_id")
        if data.get("device_token"):
            device_id = device_from_token(data["device_token"])
        else:
            user_from_token(self._bearer_token())
        if not device_id:
            raise ValueError("Falta device_id o device_token")
        add_activity(device_id, int(data.get("steps", 0)), float(data.get("distance_km", 0)))
        self._send_json(200, get_activity_today(device_id))

    def _handle_get_activity(self):
        user_from_token(self._bearer_token())
        device_id = self._query_params().get("device_id")
        self._send_json(200, get_activity_today(device_id))

    def _handle_transcribe(self):
        """Recibe {"audio_base64": ..., "language": ...} y devuelve el
        texto transcrito. El audio viaja en base64 dentro del JSON, igual
        patrón que usa el resto de Zora (sin subir binarios crudos)."""
        user_from_token(self._bearer_token())
        data = self._read_json()
        audio_b64 = data.get("audio_base64", "")
        if not audio_b64:
            raise ValueError("Falta audio_base64")
        language = data.get("language", "es-US")
        audio_bytes = base64.b64decode(audio_b64)
        outcome = transcribe_audio(audio_bytes, language=language)
        if outcome["type"] == "ok":
            return self._send_json(200, {"text": outcome["text"]})
        if outcome["type"] == "no_key":
            return self._send_json(200, {
                "error": "no_key",
                "message": "No tengo configurada la NVIDIA_API_KEY todavía.",
            })
        self._send_json(200, {"error": "error", "message": outcome.get("message", "error desconocido")})

    # ---- Imágenes generadas y cuotas de APIs gratis ----

    def _handle_get_image(self):
        image_id = self._query_params().get("id", "")
        # nombre de archivo generado por nosotros mismos (secrets.token_hex + ".png"),
        # igual se valida el patrón para no abrir la puerta a leer cualquier archivo.
        if not re.fullmatch(r"[0-9a-f]{16}\.png", image_id):
            return self._send_json(400, {"error": "id de imagen inválido"})
        path = os.path.join(IMAGES_DIR, image_id)
        if not os.path.isfile(path):
            return self._send_json(404, {"error": "imagen no encontrada"})
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_get_usage(self):
        user_from_token(self._bearer_token())
        self._send_json(200, {service: get_usage(service) for service in FREE_TIER_LIMITS})

    # ---- Recordatorios (alarmas/timers) ----

    def _handle_notifications(self):
        """Recordatorios ya vencidos que este usuario aún NO ha recibido.
        Cada uno se entrega UNA sola vez; el cliente los muestra y los lee
        en voz alta."""
        user_id = user_from_token(self._bearer_token())
        due = pop_due_notifications(user_id)
        self._send_json(200, {"notifications": due})

    def _handle_list_reminders(self):
        user_id = user_from_token(self._bearer_token())
        self._send_json(200, {"reminders": list_pending_reminders(user_id)})

    # ---- Voz preinstalada de Zora ----

    def _handle_tts(self):
        """GET /tts?text=...&voice=nova -> audio mp3 (o JSON de error, en
        cuyo caso el cliente cae a las voces del navegador)."""
        params = self._query_params()
        text = urllib.parse.unquote(params.get("text", ""))
        outcome = generate_tts(text, params.get("voice", "nova"))
        if outcome["type"] != "ok":
            return self._send_json(200, {"error": "tts",
                                          "message": outcome.get("message", "error desconocido")})
        body = outcome["audio"]
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Zora-TTS-Cached", "1" if outcome.get("cached") else "0")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---- Interfaz web servida por el backend (ver sección 9.5) ----

    def _handle_static(self, route):
        filename, content_type = STATIC_FILES[route]
        path = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(path):
            return self._send_json(404, {
                "error": f"Falta static/{filename} — copia zora_web_client.html a "
                         f"static/index.html (y los demás archivos de static/) junto a zora_backend.py"})
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # sin caché: así cada mejora de la interfaz llega al recargar,
        # sin depender de que el navegador decida pedir la página de nuevo
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_downloads_page(self):
        body = _download_page_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_download_file(self, filename):
        # nada de "../" ni rutas raras: solo nombres de archivo simples
        # dentro de downloads/, para no abrir la puerta a leer cualquier
        # archivo del servidor.
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", filename):
            return self._send_json(400, {"error": "nombre de archivo inválido"})
        path = os.path.join(DOWNLOADS_DIR, filename)
        if not os.path.isfile(path):
            return self._send_json(404, {"error": "archivo no encontrado"})
        ext = os.path.splitext(filename)[1].lower()
        content_type = DOWNLOAD_FILE_INFO.get(ext, ("application/octet-stream", ""))[0]
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # silenciar logs por defecto
        pass


# =========================================================================
# 11. AUTO-PULSO (keep-alive para Render y similares)
# =========================================================================
# Los planes gratis de Render (y otros hosts) DORMIEN el servidor tras ~15
# min sin visitas, y despertarlo toma 30-60 segundos. Este hilo le manda un
# ping a su propio /health cada KEEP_ALIVE_MINUTES para que siga despierto.
#
# IMPORTANTE: el hilo solo funciona MIENTRAS el servidor está prendido —
# no puede despertarse a sí mismo. Para el "primer toque del día" hay dos
# caminos (gratis):
#   a) Un servicio tipo cron-job.org (sin cuenta de pago) que mande un GET
#      a https://tu-app.onrender.com/health a la hora en que quieres que
#      Zora se despierte (ej. 8:00). Con uno solo basta.
#   b) Que cualquier dispositivo de la casa abra la app: la primera visita
#      ya lo despierta.
# De noche (sin pulsos externos ni visitas), Render lo duerme a los ~15 min
# y así se ahorran horas del plan gratis (750 h/mes).
#
# En Render no hay que configurar nada: usa RENDER_EXTERNAL_URL, la variable
# que Render define sola. En otro host, pon KEEP_ALIVE_URL en zora.env:
#   KEEP_ALIVE_URL=https://tu-app.onrender.com
# Y si quieres otro intervalo (por defecto 10 min):
#   KEEP_ALIVE_MINUTES=10

KEEP_ALIVE_URL = _real_env_key("KEEP_ALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
KEEP_ALIVE_MINUTES = float(os.environ.get("KEEP_ALIVE_MINUTES", "10"))


def _keep_alive_worker(url: str, interval_seconds: float):
    health_url = url.rstrip("/") + "/health"
    while True:
        time.sleep(interval_seconds)
        try:
            urllib.request.urlopen(health_url, timeout=15)
        except Exception:  # noqa: BLE001 — un pulso fallido no debe tumbar nada
            pass


def start_keep_alive():
    """Arranca el auto-pulso si hay URL a donde apuntarse. Devuelve el hilo
    o None si quedó desactivado (no aplica fuera de la nube)."""
    if not KEEP_ALIVE_URL:
        return None
    t = threading.Thread(
        target=_keep_alive_worker,
        args=(KEEP_ALIVE_URL, KEEP_ALIVE_MINUTES * 60),
        daemon=True,
    )
    t.start()
    print(f"(Auto-pulso activo: me aviso a mí mismo cada {KEEP_ALIVE_MINUTES:g} min "
          f"en {KEEP_ALIVE_URL}/health para no dormirme en el hosting)")
    return t


def run(port: int = 8000):
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), ZoraHandler)
    print(f"Zora backend corriendo en http://0.0.0.0:{port} (base de datos: {DB_PATH})")
    start_alarm_scheduler()  # hilo que dispara alarmas/timers guardados en la BD
    start_keep_alive()
    server.serve_forever()


if __name__ == "__main__":
    run(int(os.environ.get("PORT", "8000")))

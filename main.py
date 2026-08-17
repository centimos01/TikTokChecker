#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TikTok Unfollow Checker
=======================

Audita tu propia cuenta de TikTok, detecta quién te ha dejado de seguir
(usuarios a los que sigues pero que ya no te siguen de vuelta) y envía una
alerta a un canal de Discord mediante un Bot.

Arquitectura
------------
* Un único proceso Python, sin frameworks web.
* Cliente HTTP propio para la API interna de TikTok: carga un fichero de
  cookies exportado del navegador (session.json o cookies.txt) para no
  introducir credenciales en cada ejecución y minimizar riesgo de baneo.
* SQLite en modo WAL como almacén persistente. En cada ciclo se guarda el
  snapshot actual de "seguidos" y "seguidores", se compara contra lo que hay
  en base de datos y se registra el histórico de unfollows (con detección de
  "volvieron a seguirte" para avisar de nuevo si te vuelven a dejar).
* Bucle en segundo plano con intervalo configurable + jitter aleatorio para
  comportarse de forma orgánica.
* Si un ciclo falla (red, login o API de TikTok) se envía un aviso de error
  a Discord y se reintenta en el siguiente ciclo; solo se alerta la primera
  caída seguida para no saturar el canal.
* Rich Presence en tiempo real: el bot muestra en Discord los conteos actualizados
  de seguidos/seguidores y el último chequeo, actualizado en cada ciclo.

Variables de entorno: ver .env.example.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlencode

import requests

try:
    import websockets  # noqa: F401 — opcional; si falta, el Gateway no arranca.
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

log = logging.getLogger("tt-check")

# ---------------------------------------------------------------------------
# Utilidades de configuración (variables de entorno)
# ---------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    """Lee una variable de entorno o devuelve el valor por defecto."""
    return os.getenv(name, "").strip() or default


def env_float(name: str, default: float) -> float:
    """Lee un número flotante de entorno; ante valor inválido, usa el default."""
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Base de datos SQLite
# ---------------------------------------------------------------------------


def db_connect(path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre (y crea si hace falta) la base de datos y aplica el esquema."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # WAL: mejor concurrencia y durabilidad para escrituras periódicas.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        -- Registro de cada ciclo de auditoría.
        CREATE TABLE IF NOT EXISTS checks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            ok         INTEGER NOT NULL DEFAULT 0,
            following  INTEGER NOT NULL DEFAULT 0,
            followers  INTEGER NOT NULL DEFAULT 0,
            unfollows  INTEGER NOT NULL DEFAULT 0
        );

        -- Snapshot vigente de cuentas que sigues.
        CREATE TABLE IF NOT EXISTS following (
            username  TEXT PRIMARY KEY,
            user_id   TEXT,
            nickname  TEXT,
            last_seen TEXT NOT NULL
        );

        -- Snapshot vigente de cuentas que te siguen.
        CREATE TABLE IF NOT EXISTS followers (
            username  TEXT PRIMARY KEY,
            user_id   TEXT,
            nickname  TEXT,
            last_seen TEXT NOT NULL
        );

        -- Histórico de unfollows detectados.
        CREATE TABLE IF NOT EXISTS unfollowers (
            username       TEXT PRIMARY KEY,
            user_id        TEXT,
            nickname       TEXT,
            first_detected TEXT NOT NULL,
            last_detected  TEXT NOT NULL,
            alerts         INTEGER NOT NULL DEFAULT 1,
            refollowed     INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_following_seen ON following(last_seen);
        CREATE INDEX IF NOT EXISTS idx_followers_seen ON followers(last_seen);
        """
    )
    return conn


def save_snapshot(conn: sqlite3.Connection, table: str,
                  rows: dict[str, dict], run_at: str) -> None:
    """Inserta/actualiza el snapshot de esta ejecución (upsert)."""
    conn.executemany(
        f"""INSERT INTO {table} (username, user_id, nickname, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                user_id   = excluded.user_id,
                nickname  = excluded.nickname,
                last_seen = excluded.last_seen""",
        [(u, d.get("user_id", ""), d.get("nickname", ""), run_at)
         for u, d in rows.items()],
    )


def prune_old_snapshots(conn: sqlite3.Connection, table: str, run_at: str) -> None:
    """Elimina filas de snapshots anteriores: la tabla solo guarda el estado
    vigente de la última ejecución completada."""
    conn.execute(f"DELETE FROM {table} WHERE last_seen < ?", (run_at,))


def compute_new_unfollows(conn: sqlite3.Connection, following: dict,
                          followers: set, run_at: str) -> list[str]:
    """
    Devuelve los usernames con un unfollow NUEVO detectado en este ciclo.

    Reglas:
    * Marca como 'refollowed' a quien estaba en el histórico y vuelve a
      aparecer en 'followers' (te volvió a seguir -> ya no es baja).
    * Un unfollow nuevo es: está en 'following' de esta ejecución, NO está en
      'followers' de esta ejecución, y no consta en el histórico (o consta
      pero con refollowed=1, es decir, te había vuelto a seguir).
    """
    # 1) Quienes volvieron a seguirte dejan de estar "pendientes".
    conn.execute(
        """UPDATE unfollowers SET refollowed = 1
           WHERE refollowed = 0 AND username IN
               (SELECT username FROM followers WHERE last_seen = ?)""",
        (run_at,),
    )

    # 2) Candidatos: te sigo yo pero ya no me sigues tú.
    new_unfollows = []
    for username in sorted(set(following) - followers):
        row = conn.execute(
            "SELECT refollowed FROM unfollowers WHERE username = ?", (username,)
        ).fetchone()
        if row is None or row["refollowed"]:
            new_unfollows.append(username)

    # 3) Registrar el nuevo evento en el histórico.
    for username in new_unfollows:
        info = following[username]
        conn.execute(
            """INSERT INTO unfollowers
                   (username, user_id, nickname, first_detected, last_detected,
                    alerts, refollowed)
               VALUES (?, ?, ?, ?, ?, 1, 0)
               ON CONFLICT(username) DO UPDATE SET
                   last_detected = excluded.last_detected,
                   nickname      = excluded.nickname,
                   refollowed    = 0,
                   alerts        = unfollowers.alerts + 1""",
            (username, info.get("user_id", ""), info.get("nickname", ""),
             run_at, run_at),
        )

    return new_unfollows


# ---------------------------------------------------------------------------
# Alerta a Discord (API REST directa, sin librerías extra)
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"
EMBED_DESC_LIMIT = 4096
MAX_EMBEDS_PER_MSG = 10


def discord_embed(token: str, channel_id: str, title: str, description: str,
                  color: int, footer: str | None = None,
                  timestamp: str | None = None) -> bool:
    """Publica uno o más embeds en el canal indicado.
    Si la descripción supera el límite de Discord (4096 chars), la divide en
    varios embeds enviados en un solo mensaje (máx. 10 embeds/mensaje)."""
    chunks: list[str] = []
    if len(description) <= EMBED_DESC_LIMIT:
        chunks = [description]
    else:
        lines = description.split("\n")
        current = ""
        for line in lines:
            if current and len(current) + 1 + len(line) > EMBED_DESC_LIMIT:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

    embeds = []
    for i, chunk in enumerate(chunks):
        embed: dict = {
            "title": title if i == 0 else f"{title} (parte {i + 1})",
            "description": chunk,
            "color": color,
        }
        if footer and i == len(chunks) - 1:
            embed["footer"] = {"text": footer}
        if timestamp and i == 0:
            embed["timestamp"] = timestamp
        embeds.append(embed)

    ok = True
    for batch_start in range(0, len(embeds), MAX_EMBEDS_PER_MSG):
        batch = embeds[batch_start:batch_start + MAX_EMBEDS_PER_MSG]
        resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"embeds": batch},
            headers={"Authorization": f"Bot {token}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code >= 400:
            log.error("Discord devolvió %s: %s", resp.status_code,
                      resp.text[:300])
            ok = False
    return ok


def format_list(names: list[str], limit: int = 0) -> str:
    """Formatea los usernames como lista de Markdown."""
    if limit:
        names = names[:limit]
    lines = [f"• [{n}](https://tiktok.com/@{n})" for n in names]
    return "\n".join(lines) or "*(lista vacía)*"


# ---------------------------------------------------------------------------
# Cliente HTTP para la API interna de TikTok
# ---------------------------------------------------------------------------

TIKTOK_BASE = "https://www.tiktok.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class TikTokClient:
    """Cliente ligero para la API web interna de TikTok.

    Carga cookies desde un fichero exportado del navegador (JSON o Netscape)
    y realiza peticiones HTTP directas con headers realistas.  La
    autenticación se basa exclusivamente en las cookies de sesión — no se
    envía nunca la contraseña.
    """

    def __init__(self, cookies_path: str, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": "https://www.tiktok.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        })
        self.timeout = timeout
        self._csrf_token: str = ""
        self.user_id: str = ""
        self.sec_uid: str = ""
        self.load_cookies(cookies_path)

    # -- Carga de cookies ---------------------------------------------------

    def load_cookies(self, path: str) -> None:
        """Carga cookies desde un fichero JSON o Netscape."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Fichero de cookies no encontrado: {path}. "
                "Exporta las cookies de tu navegador (ver README)."
            )

        text = p.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"El fichero de cookies está vacío: {path}")

        try:
            data = json.loads(text)
            self._load_cookies_json(data)
        except (json.JSONDecodeError, ValueError):
            self._load_cookies_netscape(path)

        # Inyectar cookies en la sesión de requests.
        for name, value in self._cookies.items():
            self.session.cookies.set(name, value, domain=".tiktok.com")

        # CSRF: Many browsers store it as 'tt_csrf_token' or
        # 'csrf_token_id' cookie.  Also try to grab it from a Set-Cookie
        # header named 'ttcsrf'.
        self._csrf_token = (
            self._cookies.get("ttcsrf", "")
            or self._cookies.get("tt_csrf_token", "")
            or self._cookies.get("csrf_token_id", "")
        )

        if not self._cookies.get("sessionid"):
            raise ValueError(
                "Las cookies no contienen 'sessionid'. "
                "Asegúrate de exportarlas después de iniciar sesión en TikTok."
            )
        log.info("Cookies cargadas (%d valores, sessionid=%s…).",
                 len(self._cookies),
                 self._cookies["sessionid"][:8])

    def _load_cookies_json(self, data: list[dict] | dict) -> None:
        """Formato JSON: lista de objetos con 'name'/'value' o dict plano."""
        self._cookies: dict[str, str] = {}
        if isinstance(data, dict):
            self._cookies = {str(k): str(v) for k, v in data.items()}
            return
        if isinstance(data, list):
            for item in data:
                name = item.get("name", "")
                value = item.get("value", "")
                if name:
                    self._cookies[name] = value

    def _load_cookies_netscape(self, path: str) -> None:
        """Formato Netscape / cookies.txt (tab-separated)."""
        self._cookies = {}
        jar = MozillaCookieJar(path)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            for c in jar:
                self._cookies[c.name] = c.value
        except Exception:
            # Parser manual de respaldo para ficheros simples.
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    self._cookies[parts[5]] = parts[6]

    # -- Peticiones HTTP ----------------------------------------------------

    def request(self, method: str, path: str, **kw) -> dict | None:
        """Realiza una petición a la API interna de TikTok con CSRF y retry
        ante errores transitorios (429, 403, 5xx)."""
        url = f"{TIKTOK_BASE}{path}" if path.startswith("/") else path
        headers = dict(kw.pop("headers", {}))
        headers.setdefault("x-csrf-token", self._csrf_token)
        for attempt in range(3):
            log.debug("TT %s %s (intento %d)", method, url, attempt + 1)
            try:
                resp = self.session.request(
                    method, url, headers=headers, timeout=self.timeout,
                    **kw,
                )
            except requests.RequestException as exc:
                log.warning("Error de red: %s", exc)
                time.sleep(10 * (attempt + 1))
                continue

            if resp.status_code == 200:
                # Renovar CSRF si TikTok lo entrega.
                new_csrf = self._extract_csrf(resp)
                if new_csrf:
                    self._csrf_token = new_csrf
                try:
                    return resp.json()
                except ValueError:
                    return {}
            if resp.status_code in (403, 429, 500, 502, 503):
                wait = min(30, 10 * (attempt + 1))
                log.warning("TikTok %s — esperando %ds…", resp.status_code, wait)
                time.sleep(wait)
                continue
            log.error("TikTok %s %s → %s: %s",
                      method, path, resp.status_code, resp.text[:200])
            return None
        log.error("TikTok %s %s — agotados los reintentos.", method, path)
        return None

    def _extract_csrf(self, resp: requests.Response) -> str:
        """Extrae el token CSRF de las cabeceras o cookies de la respuesta."""
        for cookie in resp.cookies:
            if cookie.name in ("ttcsrf", "tt_csrf_token", "csrf_token_id"):
                return cookie.value
        return resp.headers.get("x-csrf-token", "")

    # -- Endpoints de TikTok ------------------------------------------------

    def get_profile(self) -> dict:
        """Obtiene el perfil del usuario autenticado.
        Devuelve {'unique_id': '...', 'sec_uid': '...', 'nickname': '...'}."""
        data = self.request("GET", "/api/user/detail/", params={
            "uniqueId": "",
            "secUid": "",
            "device_platform": "webapp",
        })
        if not data or data.get("statusCode") != 0:
            # Intento alternativo: el propio usuario con cookies.
            data = self.request("GET", "/api/user/detail/", params={
                "device_platform": "webapp",
            })
        if not data:
            raise RuntimeError("No se pudo obtener el perfil de TikTok.")

        user = data.get("userInfo", {}).get("user", {})
        if not user.get("id") and not user.get("uniqueId"):
            raise RuntimeError(
                "No se encontraron datos de usuario en la respuesta. "
                "Las cookies pueden estar caducadas o TikTok bloqueó la petición."
            )
        self.user_id = str(user.get("id", ""))
        self.sec_uid = user.get("secUid", "")
        return user

    def get_following(self) -> dict[str, dict]:
        """Descarga la lista completa de cuentas que sigues.
        Devuelve {unique_id: {'user_id': ..., 'nickname': ...}}."""
        if not self.sec_uid:
            self.get_profile()

        result: dict[str, dict] = {}
        cursor = 0
        page = 0

        while True:
            page += 1
            params = {
                "user": self.user_id,
                "secUid": self.sec_uid,
                "count": 30,
                "max_cursor": cursor,
                "device_platform": "webapp",
            }
            data = self.request("GET", f"/api/following/list?{urlencode(params)}")

            if not data:
                break
            status = data.get("status_code", -1)
            if status == 8:
                log.warning("TikTok requiere CAPTCHA/verificación en following.")
                break
            if status != 0:
                log.warning("API following devolvió status %s", status)
                break

            users = data.get("data", {}).get("acceptable_contacts_query_result", {}) \
                         .get("user_list", [])
            if not users:
                users = data.get("data", {}).get("user_list", [])

            for item in users:
                user = item.get("user", {})
                uid = user.get("uniqueId", "")
                if uid:
                    result[uid] = {
                        "user_id": str(user.get("id", "")),
                        "nickname": user.get("nickname", ""),
                    }

            cursor = data.get("cursor", 0)
            has_more = data.get("has_more", 0)

            log.debug("Following página %d: +%d (total: %d)",
                      page, len(users), len(result))
            if not has_more or cursor == 0:
                break
            time.sleep(random.uniform(2, 5))

        return result

    def get_followers(self) -> dict[str, dict]:
        """Descarga la lista completa de seguidores.
        Devuelve {unique_id: {'user_id': ..., 'nickname': ...}}."""
        if not self.sec_uid:
            self.get_profile()

        result: dict[str, dict] = {}
        cursor = 0
        page = 0

        while True:
            page += 1
            params = {
                "user": self.user_id,
                "secUid": self.sec_uid,
                "count": 30,
                "max_cursor": cursor,
                "device_platform": "webapp",
            }
            data = self.request("GET", f"/api/follower/list?{urlencode(params)}")

            if not data:
                break
            status = data.get("status_code", -1)
            if status == 8:
                log.warning("TikTok requiere CAPTCHA/verificación en followers.")
                break
            if status != 0:
                log.warning("API followers devolvió status %s", status)
                break

            users = data.get("data", {}).get("user_list", [])

            for item in users:
                user = item.get("user", {})
                uid = user.get("uniqueId", "")
                if uid:
                    result[uid] = {
                        "user_id": str(user.get("id", "")),
                        "nickname": user.get("nickname", ""),
                    }

            cursor = data.get("cursor", 0)
            has_more = data.get("has_more", 0)

            log.debug("Followers página %d: +%d (total: %d)",
                      page, len(users), len(result))
            if not has_more or cursor == 0:
                break
            time.sleep(random.uniform(2, 5))

        return result


# ---------------------------------------------------------------------------
# Login / sesión de TikTok
# ---------------------------------------------------------------------------


def login_tiktok(cfg: dict) -> TikTokClient:
    """
    Carga las cookies desde el fichero configurado y valida la sesión
    obteniendo el perfil del usuario.  Si falla, lanza RuntimeError con
    instrucciones claras.
    """
    client = TikTokClient(cfg["cookies_file"])

    # Validación: obtener perfil propio.
    try:
        profile = client.get_profile()
        log.info("Sesión válida — @%s (id: %s)",
                 profile.get("uniqueId", "???"), client.user_id)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Error al validar la sesión de TikTok: {exc}\n"
            "Las cookies pueden haber caducado. Exporta nuevas desde tu navegador."
        ) from exc

    return client


# ---------------------------------------------------------------------------
# Ciclo de auditoría
# ---------------------------------------------------------------------------


def run_once(conn: sqlite3.Connection, cfg: dict) -> list[str]:
    """
    Ejecuta una comprobación completa:
    1. Login (reutilizando cookies).
    2. Descarga de seguidos y seguidores (con pausa aleatoria entre llamadas).
    3. Persistencia del snapshot en SQLite y comparación con lo anterior.
    4. Alerta a Discord solo si hay unfollows NUEVOS.
    """
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    client = login_tiktok(cfg)

    log.info("Obteniendo lista de cuentas que sigues…")
    following = client.get_following()
    log.info("Seguidos: %d", len(following))

    # Pausa aleatoria entre las dos llamadas grandes: comportamiento orgánico.
    time.sleep(random.uniform(30, 90))

    log.info("Obteniendo lista de tus seguidores…")
    followers = client.get_followers()
    log.info("Seguidores: %d", len(followers))

    # ---- Persistir el estado de esta ejecución. ----
    save_snapshot(conn, "following", following, started)
    save_snapshot(conn, "followers", followers, started)

    new_unfollows = compute_new_unfollows(conn, following, set(followers), started)

    # Las tablas de snapshot solo conservan el estado vigente del último ciclo.
    prune_old_snapshots(conn, "following", started)
    prune_old_snapshots(conn, "followers", started)

    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO checks (started_at, finished_at, ok, following, followers, unfollows)
           VALUES (?, ?, 1, ?, ?, ?)""",
        (started, finished, len(following), len(followers), len(new_unfollows)),
    )
    conn.commit()

    # ---- Notificar por Discord solo las bajas nuevas. ----
    if new_unfollows:
        update_bot_presence(len(following), len(followers),
                            unfollow=new_unfollows[0])
        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        footer = (
            f"Chequeo #{check_id} · seguidos: {len(following)} "
            f"· seguidores: {len(followers)}"
        )
        discord_embed(
            cfg["discord_token"],
            cfg["discord_channel"],
            f"{len(new_unfollows)} nuevo(s) unfollow(s) detectado(s)",
            format_list(new_unfollows),
            0xED4245,  # rojo Discord
            footer=footer,
            timestamp=finished,
        )
        log.info("Enviadas alertas para %d unfollow(s) nuevos.",
                 len(new_unfollows))
    else:
        log.info("Sin nuevos unfollows en este ciclo.")

    # Actualizar presencia del bot con los conteos finales.
    update_bot_presence(len(following), len(followers))

    return new_unfollows


# ---------------------------------------------------------------------------
# Avisos de fallo por Discord
# ---------------------------------------------------------------------------


def describe_failure(exc: BaseException) -> str:
    """Convierte una excepción en un texto claro para el log y la alerta."""
    if isinstance(exc, RuntimeError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def notify_failure(conn: sqlite3.Connection, cfg: dict, exc: BaseException,
                   started: str) -> None:
    """
    Registra un ciclo fallido en SQLite y, si es el primer fallo tras un ciclo
    correcto, envía un aviso de error a Discord (evita repetir la alerta cada
    ciclo mientras el problema persista).
    """
    try:
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        description = describe_failure(exc)

        prev = conn.execute(
            "SELECT ok FROM checks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        first_failure = prev is None or prev["ok"] == 1

        conn.execute(
            """INSERT INTO checks
                   (started_at, finished_at, ok, following, followers, unfollows)
               VALUES (?, ?, 0, 0, 0, 0)""",
            (started, finished),
        )
        conn.commit()

        log.error("Fallo en la comprobación: %s", description)
        if not first_failure:
            log.info("El problema persiste desde el ciclo anterior; "
                     "no se reenvía alerta.")
            return

        check_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        footer = (f"Chequeo fallido #{check_id} · "
                  "se reintentará en el siguiente ciclo")
        discord_embed(
            cfg["discord_token"],
            cfg["discord_channel"],
            "Error en la comprobación de TikTok",
            f"```\n{description}\n```",
            0xFAA61A,  # ámbar Discord
            footer=footer,
            timestamp=finished,
        )
    except Exception:
        log.exception("No se pudo registrar/notificar el fallo del ciclo.")


# ---------------------------------------------------------------------------
# Discord Gateway — Rich Presence en tiempo real
# ---------------------------------------------------------------------------

_gateway: DiscordGateway | None = None
_check_lock = threading.Lock()


class DiscordGateway:
    """Mantiene una conexión WebSocket al Gateway de Discord en un hilo
    dedicado, para enviar actualizaciones de presencia en tiempo real."""

    GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
    RECONNECT_DELAY = 5

    def __init__(self, token: str, conn: sqlite3.Connection, cfg: dict):
        if not _HAS_WS:
            raise RuntimeError("El paquete 'websockets' no está instalado.")
        self.token = token
        self.conn = conn
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self.ws = None
        self.heartbeat_interval: float = 41250
        self.sequence: int | None = None
        self.app_id: str | None = None
        self._presence_state = "Esperando primera comprobación…"
        self._presence_details = ""
        self._presence_ts: float | None = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gateway"
        )

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not _HAS_WS:
            log.warning("websockets no instalado; Rich Presence deshabilitado.")
            return
        self._thread.start()
        log.info("Discord Gateway iniciado en segundo plano.")

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    # -- Hilos / async ------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self) -> None:
        while self.loop.is_running():
            try:
                async with websockets.connect(
                    self.GATEWAY, close_timeout=5,
                ) as ws:
                    self.ws = ws
                    log.info("Gateway conectado.")
                    await self._handle_session()
            except websockets.ConnectionClosed as exc:
                log.warning("Gateway desconectado (%s). Reconectando en %ds…",
                            exc, self.RECONNECT_DELAY)
            except Exception as exc:
                log.warning("Gateway error (%s). Reconectando en %ds…",
                            exc, self.RECONNECT_DELAY)
            await asyncio.sleep(self.RECONNECT_DELAY)

    async def _handle_session(self) -> None:
        raw = await self.ws.recv()
        hello = json.loads(raw)
        if hello.get("op") != 10:
            return
        self.heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000

        await self.ws.send(json.dumps({
            "op": 2,
            "d": {
                "token": self.token,
                "properties": {
                    "os": "linux",
                    "browser": "python",
                    "device": "",
                },
                "intents": 0,
            },
        }))

        me = await self._rest("GET", "/users/@me")
        if me:
            self.app_id = me["id"]
            await self._register_commands()

        await self._send_presence()
        await asyncio.gather(self._heartbeat(), self._listen())

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await self.ws.send(
                    json.dumps({"op": 1, "d": self.sequence})
                )
            except websockets.ConnectionClosed:
                return

    async def _listen(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == 1:
                    await self.ws.send(
                        json.dumps({"op": 1, "d": self.sequence})
                    )
                elif op == 11:
                    pass
                if msg.get("s") is not None:
                    self.sequence = msg["s"]
                if op == 0 and msg.get("t") == "INTERACTION_CREATE":
                    asyncio.create_task(
                        self._handle_interaction(msg["d"])
                    )
        except websockets.ConnectionClosed:
            return

    # -- Presencia ----------------------------------------------------------

    def update_presence(self, state: str, details: str,
                        timestamp: float | None = None) -> None:
        self._presence_state = state
        self._presence_details = details
        self._presence_ts = timestamp
        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_presence(), self.loop
            )

    async def _send_presence(self) -> None:
        if not self.ws:
            return
        activity: dict = {
            "name": "TikTok",
            "type": 3,
            "state": self._presence_state,
        }
        if self._presence_details:
            activity["details"] = self._presence_details
        if self._presence_ts:
            activity["timestamps"] = {"start": int(self._presence_ts)}
        payload = {
            "op": 3,
            "d": {
                "since": None,
                "activities": [activity],
                "status": "online",
                "afk": False,
            },
        }
        try:
            await self.ws.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            pass

    # -- REST helper --------------------------------------------------------

    async def _rest(self, method: str, path: str, **kw):
        url = f"https://discord.com/api/v10{path}"
        headers = {"Authorization": f"Bot {self.token}"}

        def _do():
            resp = requests.request(method, url, headers=headers,
                                    timeout=10, **kw)
            if resp.status_code < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            log.debug("Discord REST %s %s → %s",
                      method, path, resp.status_code)
            return None

        return await asyncio.to_thread(_do)

    # -- Slash commands -----------------------------------------------------

    SLASH_COMMANDS = [
        {
            "name": "status",
            "description": "Muestra el estado actual del checker "
                           "(seguidos, seguidores, unfollows…)",
            "type": 1,
        },
        {
            "name": "check",
            "description": "Fuerza una comprobación manual ahora mismo",
            "type": 1,
        },
    ]

    async def _register_commands(self) -> None:
        if not self.app_id:
            return
        await self._rest(
            "PUT",
            f"/applications/{self.app_id}/commands",
            json=self.SLASH_COMMANDS,
        )
        log.info("Slash commands registrados (/status, /check).")

    async def _handle_interaction(self, interaction: dict) -> None:
        name = interaction.get("data", {}).get("name", "")
        if name == "status":
            await self._cmd_status(interaction)
        elif name == "check":
            await self._cmd_check(interaction)
        else:
            await self._respond(interaction,
                                content="Comando desconocido.")

    async def _respond(self, interaction: dict, *,
                       content: str = "", embeds: list | None = None,
                       flags: int = 0) -> None:
        data: dict = {}
        if content:
            data["content"] = content
        if embeds:
            data["embeds"] = embeds
        if flags:
            data["flags"] = flags
        await self._rest(
            "POST",
            (f"/interactions/{interaction['id']}/"
             f"{interaction['token']}/callback"),
            json={"type": 4, "data": data},
        )

    async def _cmd_status(self, interaction: dict) -> None:
        def _query():
            row = self.conn.execute(
                "SELECT following, followers, unfollows, "
                "started_at, finished_at "
                "FROM checks WHERE ok = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total = self.conn.execute(
                "SELECT COUNT(*) FROM checks WHERE ok = 1"
            ).fetchone()[0]
            return row, total

        try:
            row, total = await asyncio.to_thread(_query)
        except Exception as exc:
            await self._respond(interaction,
                                content=f"Error consultando la BD: {exc}")
            return

        if not row:
            await self._respond(
                interaction,
                content="Aún no hay ninguna comprobación completada.",
            )
            return

        following, followers, unfollows, started, finished = row

        try:
            fin_dt = datetime.fromisoformat(finished)
            delta = datetime.now(timezone.utc) - fin_dt
            mins = int(delta.total_seconds() / 60)
            if mins < 60:
                elapsed = f"hace {mins} min"
            elif mins < 1440:
                elapsed = f"hace {mins // 60}h {mins % 60}min"
            else:
                elapsed = f"hace {mins // 1440}d"
        except Exception:
            elapsed = finished

        next_check = self.cfg["interval_hours"]

        embed = {
            "title": "📊 Estado de TikTok Checker",
            "color": 0x5865F2,
            "fields": [
                {"name": "👤 Seguidos",
                 "value": str(following), "inline": True},
                {"name": "👥 Seguidores",
                 "value": str(followers), "inline": True},
                {"name": "🚫 Unfollows (último chequeo)",
                 "value": str(unfollows), "inline": True},
                {"name": "📅 Último chequeo",
                 "value": elapsed, "inline": True},
                {"name": "⏰ Próximo chequeo",
                 "value": f"~{next_check:.0f} h", "inline": True},
                {"name": "🔁 Total comprobaciones",
                 "value": str(total), "inline": True},
            ],
        }
        await self._respond(interaction, embeds=[embed])

    def _run_check(self) -> None:
        with _check_lock:
            run_once(self.conn, self.cfg)

    async def _cmd_check(self, interaction: dict) -> None:
        await self._respond(
            interaction,
            content="⏳ Comprobación manual en curso…",
            flags=64,  # EPHEMERAL
        )
        try:
            await asyncio.to_thread(self._run_check)

            def _query():
                return self.conn.execute(
                    "SELECT following, followers, unfollows "
                    "FROM checks WHERE ok = 1 ORDER BY id DESC LIMIT 1"
                ).fetchone()

            row = await asyncio.to_thread(_query)
            if row:
                following, followers, unfollows = row
                msg = (
                    f"✅ Comprobación completada.\n"
                    f"👤 Seguidos: **{following}** · "
                    f"👥 Seguidores: **{followers}** · "
                    f"🚫 Unfollows nuevos: **{unfollows}**"
                )
            else:
                msg = "✅ Comprobación completada (sin datos)."
            await self._rest(
                "PATCH",
                (f"/webhooks/{self.app_id}/"
                 f"{interaction['token']}/messages/@original"),
                json={"content": msg},
            )
        except Exception as exc:
            await self._rest(
                "PATCH",
                (f"/webhooks/{self.app_id}/"
                 f"{interaction['token']}/messages/@original"),
                json={"content": f"❌ Error: {exc}"},
            )


def update_bot_presence(following: int = 0, followers: int = 0,
                        unfollow: str | None = None) -> None:
    """Actualiza la presencia del bot en Discord con los datos actuales."""
    if _gateway is None:
        return
    if unfollow:
        state = f"Nuevo unfollow: @{unfollow}"
    else:
        state = f"{following} seguidos · {followers} seguidores"
    _gateway.update_presence(
        state,
        "Último chequeo",
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auditor de unfollows de TikTok"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Ejecuta una única comprobación y termina "
             "(útil para depurar o generar la sesión)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Log en nivel DEBUG",
    )
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = {
        "cookies_file": env_str("TIKTOK_COOKIES_FILE",
                                "/data/cookies.txt"),
        "discord_token": env_str("DISCORD_BOT_TOKEN"),
        "discord_channel": env_str("DISCORD_CHANNEL_ID"),
        "db_file": env_str("DB_FILE", "/data/audit.db"),
        "interval_hours": env_float("CHECK_INTERVAL_HOURS", 6.0),
        "jitter_minutes": env_float("JITTER_MINUTES", 30.0),
    }

    missing = [k for k in ("discord_token", "discord_channel")
               if not cfg[k]]
    if missing:
        log.error(
            "Faltan variables de entorno obligatorias: %s",
            ", ".join(k.upper() for k in missing),
        )
        sys.exit(2)

    conn = db_connect(cfg["db_file"], check_same_thread=False)

    # Iniciar el Gateway de Discord para Rich Presence y slash commands.
    global _gateway
    try:
        _gateway = DiscordGateway(cfg["discord_token"], conn, cfg)
        _gateway.start()
    except Exception as exc:
        log.warning(
            "No se pudo iniciar el Gateway de Discord (%s). "
            "Rich Presence deshabilitado.", exc
        )

    # Modo manual: una sola pasada.
    if args.once:
        try:
            with _check_lock:
                run_once(conn, cfg)
        except Exception:
            log.exception("Fallo en la comprobación manual")
            sys.exit(1)
        return

    log.info(
        "Bucle iniciado: comprobación cada ~%.1f h "
        "(+jitter de hasta %.0f min).",
        cfg["interval_hours"], cfg["jitter_minutes"],
    )

    while True:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with _check_lock:
                run_once(conn, cfg)
        except Exception as exc:
            notify_failure(conn, cfg, exc, started)

        total = (cfg["interval_hours"] * 3600
                 + random.uniform(0, cfg["jitter_minutes"] * 60))
        log.info("Siguiente comprobación en %.1f minutos.", total / 60)
        try:
            time.sleep(total)
        except KeyboardInterrupt:
            log.info("Interrumpido. Saliendo…")
            break


if __name__ == "__main__":
    main()

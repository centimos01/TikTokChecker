# TikTokChecker

> ## 🚧 ESTE PROYECTO NO FUNCIONA (ROTO / ABANDONADO) 🚧
>
> **Así es: a día de hoy este proyecto NO funciona y queda abandonado.** Lo
> publico como documentación y por si algún "ser de luz" quiere rescatar la
> idea y hacerlo funcionar de nuevo.
>
> **Por qué no funciona:** TikTok protege su API web interna con la firma
> anti-scraping **X-Gnarly** (sucesora de X-Bogus), que se regenera casi a
> diario y además valida el *fingerprint* del navegador, el `msToken` y otros
> parámetros frescos que una solución offline (solo `requests` + cookies) no
> puede replicar. Los endpoints devuelven **HTTP 200 con cuerpo vacío** si la
> firma no coincide.
>
> - La firma incluida (`x_gnarly.py`) corresponde al SDK **5.1.2** (septiembre
>   2025), ya obsoleta frente a las versiones 5.2+/5.3 que TikTok exige en
>   2026.
> - La vía correcta sería ejecutar las llamadas desde un **navegador headless
>   real (Playwright/Puppeteer)** para que el propio SDK de TikTok genere las
>   firmas y el fingerprint, pero requiere ~500MB de RAM extra por encima de
>   un cliente HTTP ligero.
>
> **Qué hay implementado y funciona:** el checker descarga seguidos/seguidores
> vía la API web interna de TikTok (con cookies de sesión, retry y backoff),
> persiste snapshots en SQLite (WAL), detecta dos tipos de baja y alerta por
> Discord (con Rich Presence y el comando `/notificaciones`). El fallo no es
> la lógica de auditoría ni las alertas — es exclusivamente la autenticación
> de firmas contra TikTok.
>
> **Para rescatarlo**, hazte con un VPS con ≥2GB de RAM (o corre el bucle en
> tu máquina de escritorio), integra las llamadas a los endpoints
> `/api/user/detail`, `/api/following/list` y `/api/follower/list` **a través
> de un navegador headless** que use tus cookies de sesión, y deja que el SDK
> de TikTok firme cada petición. Mantén el resto de la lógica (SQLite +
> Discord) tal cual.

Servicio **ultraligero** autohosteado en Docker que audita tu cuenta de TikTok
y envía alertas a un canal de Discord mediante un Bot. Detecta dos tipos de baja:

- **Baja de vuelta:** te dejan de seguir pero TÚ sigues. Aviso **siempre activo**.
- **Baja total:** te dejan de seguir y ya no los sigues. Aviso **configurable**
  por Discord con `/notificaciones on|off`.

Stack: **Python 3.13 (Debian Trixie) + requests + SQLite (WAL)**. Sin frameworks
web, sin dependencias pesadas, un único proceso con bucle de temporización +
jitter aleatorio. Incluye **Rich Presence en tiempo real** en Discord (contadores
actualizados de seguidos y seguidores).

> **Importante:** TikTok no dispone de un cliente Python fiable para listas de
> seguidores/seguidos. La autenticación es 100% cookie-based: se exportan las
> cookies del navegador tras iniciar sesión en tiktok.com.

## Contenido

| Fichero | Descripción |
|---------|-------------|
| `Dockerfile` | Imagen `python:3.13-slim` (Debian 13/Trixie), usuario sin privilegios, FS raíz de solo lectura |
| `docker-compose.yml` | Límites de CPU/RAM, volumen persistente `checker-data:/data`, hardening |
| `requirements.txt` | Solo `requests` y `websockets` — dependencias mínimas |
| `main.py` | Script autónomo: carga cookies, descarga following/followers vía API interna de TikTok, snapshots SQLite, comparación, alerta Discord, Rich Presence en tiempo real |
| `x_gnarly.py` | Generador de la firma **X-Gnarly** (anti-scraping) que TikTok exige en las llamadas de API webapp. Python puro, sin dependencias |
| `install.sh` | Instalador interactivo para Debian 13: Docker + config + primer arranque |
| `.env.example` | Plantilla de configuración |

## Qué hace `main.py` en cada ciclo

1. Carga las cookies exportadas del navegador para autenticarse en la API
   interna de TikTok **sin usar la contraseña**.
2. Obtiene el perfil del usuario (`/api/user/detail/`) para tener `secUid`
   e `id`, necesarios para las llamadas paginadas.
3. Descarga la lista completa de **seguidos** (`/api/following/list`) y de
   **seguidores** (`/api/follower/list`), con una pausa aleatoria de 30–90 s
   entre ambas llamadas.
4. Guarda ambos snapshots en SQLite y los compara con el ciclo anterior.
5. Detecta y avisa de dos tipos de baja:
   - **Baja de vuelta** (te dejan de seguir pero TÚ sigues): aviso
     **siempre activo**.
   - **Baja total** (te dejan de seguir Y ya no los sigues): aviso
     **configurable por Discord** con `/notificaciones on|off`.
6. Recuerda a quien volvió a seguirte: si vuelven a dejarte, avisa de nuevo.
7. Si un ciclo falla (problema de red, cookies caducadas o CAPTCHA de TikTok),
   envía un aviso de **error** al canal de Discord y reintenta en el siguiente
   ciclo. Para no saturar el canal solo alerta la primera caída seguida.
8. Actualiza la **Rich Presence** del bot en Discord con los conteos actualizados
   y un cronómetro en tiempo real desde el último chequeo.
9. Duerme el intervalo configurado (`CHECK_INTERVAL_HOURS`) + jitter aleatorio.

## Comandos slash de Discord

El bot registra automáticamente estos comandos al iniciar:

| Comando | Descripción |
|---------|-------------|
| `/status` | Muestra seguidos, seguidores, unfollows del último chequeo, próximo chequeo, total de comprobaciones y el estado de los avisos |
| `/check` | Fuerza una comprobación manual (misma lógica que `--once` pero sin reiniciar el contenedor) |
| `/notificaciones` | Activa (`on`) o desactiva (`off`) el aviso de "baja total" por Discord. Sin argumentos muestra el estado actual |

Los comandos se registran globalmente al conectar al Gateway y están disponibles
en todos los servidores donde esté el bot.

### Control de avisos por Discord

El checker distingue dos tipos de baja:

- **Baja de vuelta:** alguien te deja de seguir pero TÚ sí lo sigues. Este
  aviso está **siempre activo** y no se puede desactivar.
- **Baja total:** alguien te deja de seguir Y ya no lo sigues (dejó de
  seguirte del todo). Este aviso es **configurable** con
  `/notificaciones on|off`:

  - **on (por defecto):** cada ciclo envía un embed (fucsia) cuando se detecta
    una baja total.
  - **off:** el checker sigue registrando las bajas totales en SQLite, pero
    **no** envía el aviso a Discord. Puedes reactivarlo con `/notificaciones on`.

El estado se guarda de forma persistente y se conserva entre reinicios. `/status`
muestra el estado de ambos tipos de aviso.

## Desplegar en otra máquina

**Opción recomendada** (todo automático con `install.sh`, ver más abajo).

Si prefieres hacerlo paso a paso:

```bash
scp -r TikTokChecker usuario@IP_DEL_SERVIDOR:~/TikTokChecker
```

Importante:

- El primer arranque debe hacerse en la máquina destino.
- No subas `.env` ni los ficheros de cookies a ningún repositorio
  (`.dockerignore` ya los excluye de la imagen).

## 1. Exportar cookies de TikTok (OBLIGATORIO)

Este es el paso más importante. TikTok requiere cookies de sesión válidas.

### Usando la extensión del navegador

1. Instala una extensión para exportar cookies:
   - **Chrome/Edge:** ["Get cookies.txt LOCALLY"](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - **Firefox:** ["cookies.txt"](https://addons.mozilla.org/es/firefox/addon/cookies-txt/)
2. Inicia sesión en [tiktok.com](https://www.tiktok.com) con tu cuenta.
3. Navega a tiktok.com (debes estar logueado).
4. Haz clic en la extensión → **Export** → guarda como `cookies.txt`.
5. Alternativamente, puedes exportar en formato JSON si la extensión lo permite.

### Ficheros aceptados

- **Netscape/cookies.txt** (recomendado): formato de texto tab-separated.
- **JSON**: lista de objetos `{"name": "...", "value": "..."}` o dict plano.

El fichero **debe contener** al menos:
- `sessionid` — cookie de sesión principal
- `ttcsrf` o `csrf_token_id` — token CSRF (se auto-detecta)

### Copiar las cookies al servidor

```bash
# Opción A: con install.sh (pregunta por las cookies)
bash install.sh

# Opción B: manualmente
cp cookies.txt ~/TikTokChecker/
cd ~/TikTokChecker
docker compose up -d --build
docker compose cp cookies.txt tiktok-checker:/data/cookies.txt
docker compose restart
```

### Cookies caducadas

Las cookies de TikTok expiran periodicamente. Cuando esto ocurra:
1. Exporta nuevas cookies desde tu navegador.
2. Copia el fichero al volumen y reinicia:
   ```bash
   docker compose cp cookies.txt tiktok-checker:/data/cookies.txt
   docker compose restart
   ```

## 2. Crear el Bot de Discord

1. Entra en https://discord.com/developers/applications → *New Application*.
2. Pestaña **Bot** → *Reset Token* → copia el token (va a `DISCORD_BOT_TOKEN`).
3. En **OAuth2 → URL Generator**, marca scope `bot` y `applications.commands`,
   y permiso *Send Messages* → abre la URL generada e invita al bot a tu
   servidor/canal.
4. Obtén el ID del canal: *Configuración del usuario → Avanzado → Modo desarrollador*,
   clic derecho sobre el canal → *Copiar ID del canal* → `DISCORD_CHANNEL_ID`.

> **Nota:** el bot muestra Rich Presence en tiempo real automáticamente (no
> necesita permisos extra ni configuración en el Developer Portal).

## 3. Instalar Docker en Debian 13 (Trixie)

Debian 13 ya incluye Docker en sus repos oficiales:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker        # o cierra/abre sesión
docker --version && docker compose version
```

## 4. Configurar el proyecto

```bash
cd ~/TikTokChecker
cp .env.example .env
nano .env                    # rellena token de Discord y canal
```

`.env` es ignorado por Dockerfile (`.dockerignore`), así que el token nunca
entra en la imagen.

## 5. Primer arranque

```bash
docker compose up -d --build
docker compose logs -f
```

Si las cookies son válidas, verás en el log algo como:
```
tt-check — Sesión válida — @tu_usuario (id: 1234567890)
tt-check — Seguidos: 523
tt-check — Seguidores: 1042
```

> **Opcional:** ejecutar una única pasada manual (depuración):
> `docker compose exec tiktok-checker python main.py --once --debug`

## 6. Operación diaria

```bash
docker compose logs -f            # seguir los logs
docker compose restart            # reiniciar
docker compose down               # parar (conserva el volumen de datos)

# Backup del estado (cookies + audit.db)
docker run --rm \
  -v tiktokchecker_checker-data:/data \
  -v "$PWD":/backup alpine \
  sh -c "tar czf /backup/backup-$(date +%F).tar.gz -C /data ."
```

## 7. Solución de problemas

- **Cookies no encontradas / sessionid no encontrado** → exporta de nuevo las
  cookies desde tu navegador (ver sección 1) y cópialas al volumen.
- **Discord no recibe nada (403)** → el bot no está invitado a ese canal o
  falta el permiso *Send Messages*.
- **`Faltan variables...`** → revisa `.env` (el archivo debe existir, se carga
  con `env_file`).
- **TikTok requiere CAPTCHA (status_code: 8)** → TikTok ha detectado
  actividad sospechosa. Deja pasar unas horas y reintenta. No reduzcas el
  intervalo por debajo de 4 h.
- **`websockets` no instalado / Gateway no arranca** → la presencia del bot no
  se actualiza pero el servicio funciona normal. Reinstala dependencias:
  `docker compose up -d --build`.
- **API devuelve datos vacíos / 0 seguidos** → puede ser rate-limit de TikTok.
  Espera 1-2 horas y reintenta con `--once --debug`.

## 8. La API interna de TikTok

TikTok no ofrece una API pública para listas de seguidores/seguidos. Este
servicio utiliza los **endpoints internos** de la web de TikTok:

| Endpoint | Descripción |
|----------|-------------|
| `GET /api/user/detail/` | Perfil del usuario autenticado (id, secUid, uniqueId) |
| `GET /api/following/list` | Lista paginada de cuentas que sigues (cursor-based) |
| `GET /api/follower/list` | Lista paginada de seguidores (cursor-based) |

Todos los endpoints requieren:
- Cookies de sesión válidas (especialmente `sessionid`).
- Token CSRF enviado como header `x-csrf-token`.
- User-Agent y headers similares a los de un navegador real.
- **Firma `X-Gnarly`** en la query string (generada por `x_gnarly.py`). Sin
  ella, TikTok responde `200` con cuerpo vacío. Es el requisito anti-scraping
  actual de la web; TikTok lo rota periódicamente, así que si las llamadas
  vuelven a devolver `200 0`, probablemente hay que actualizar `x_gnarly.py`
  a una versión más reciente del algoritmo (el checker avisará por Discord
  al fallar la comprobación).

El cliente HTTP implementa retry automático ante errores transitorios (429,
403, 5xx) con backoff progresivo y pausas aleatorias entre páginas.

## Notas de uso responsable

Audita **solo tu propia cuenta**. Reducir el intervalo por debajo de ~4 h o
lanzar comprobaciones muy seguidas aumenta el riesgo de que TikTok marque la
cuenta como sospechosa o active CAPTCHA. Los retardos aleatorios ya incluidos
(jitter + pausas) están pensados para comportarse de forma orgánica.

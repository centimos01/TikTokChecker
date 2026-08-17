#!/usr/bin/env bash
# ==========================================================
#  TikTokChecker — Instalador rápido para Debian 13
#  Ejecútalo en la máquina destino:
#      bash install.sh
# ==========================================================
set -euo pipefail

REPO_URL="https://github.com/centimos01/TikTokChecker.git"
PROJECT_DIR="${HOME}/TikTokChecker"

echo "==========================================="
echo "  TikTokChecker — Instalador Debian 13    "
echo "==========================================="
echo

# ── 1. Docker ────────────────────────────────────────────
if command -v docker &>/dev/null; then
    echo "[1/4] Docker ya instalado"
else
    echo "[1/4] Instalando Docker..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq git docker.io docker-compose-v2
    sudo systemctl enable --now docker
    sudo usermod -aG docker "$USER"
    echo "  Docker instalado. Si 'docker' no funciona sin sudo,"
    echo "  cierra sesión y vuelve a entrar (o ejecuta: newgrp docker)."
fi
echo

# ── 2. Código fuente ────────────────────────────────────
echo "[2/4] Obteniendo el código..."
if [ -d "${PROJECT_DIR}/.git" ]; then
    echo "  Repo ya existe en ${PROJECT_DIR}"
else
    git clone "$REPO_URL" "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"
echo

# ── 3. Configurar .env ──────────────────────────────────
if [ -f .env ]; then
    echo "[3/4] .env ya existe — manteniendo configuración actual"
else
    echo "[3/4] Configuración inicial"
    cp .env.example .env

    echo
    echo "  ── Cookies de TikTok ──"
    echo "  Debes exportar las cookies de tu navegador mientras"
    echo "  estás logueado en tiktok.com (ver README)."
    echo "  Opciones:"
    echo "    1) Tengo el fichero listo — lo copiaré al volumen"
    echo "    2) Lo haré después manualmente"
    echo
    read -r -p "  Elige opción [1/2]: " COOKIE_OPT

    if [ "${COOKIE_OPT}" = "1" ]; then
        echo
        read -r -p "  Ruta completa al fichero de cookies: " COOKIE_SRC
        if [ -f "$COOKIE_SRC" ]; then
            COOKIE_NAME=$(basename "$COOKIE_SRC")
            COOKIE_EXT="${COOKIE_NAME##*.}"
            if [ "$COOKIE_EXT" = "json" ]; then
                cp "$COOKIE_SRC" cookies.json
                sed -i 's|^TIKTOK_COOKIES_FILE=.*|TIKTOK_COOKIES_FILE=/data/cookies.json|' .env
                echo "  Fichero JSON copiado."
            else
                cp "$COOKIE_SRC" cookies.txt
                echo "  Fichero Netscape copiado."
            fi
        else
            echo "  ⚠ Fichero no encontrado: $COOKIE_SRC"
            echo "  Copia el fichero de cookies a la carpeta del proyecto"
            echo "  y reinicia con: docker compose up -d"
        fi
    else
        echo "  Recuerda copiar el fichero de cookies antes del primer arranque."
    fi

    echo
    read -rs -p "  Discord bot token: " DC_TOKEN; echo
    read -r  -p "  Discord channel ID: " DC_CHANNEL
    echo

    # Preservar las líneas TIKTOK_ del .env original (copiado de .env.example).
    TIKTOK_LINES=$(grep -E '^TIKTOK_' .env 2>/dev/null || true)

    {
        printf 'TIKTOK_COOKIES_FILE=%s\n' "${TIKTOK_LINES##*=}"
        printf '\n'
        printf '# ===== Discord =====\n'
        printf 'DISCORD_BOT_TOKEN=%s\n'    "$DC_TOKEN"
        printf 'DISCORD_CHANNEL_ID=%s\n'   "$DC_CHANNEL"
        printf '\n'
        printf '# Auditoría (deja por defecto si no sabes qué poner)\n'
        printf 'CHECK_INTERVAL_HOURS=6\n'
        printf 'JITTER_MINUTES=30\n'
        printf 'DB_FILE=/data/audit.db\n'
        printf '\n'
        printf 'TZ=Europe/Madrid\n'
    } > .env

    echo "  .env creado."
fi
echo

# ── 4. Build + arranque ─────────────────────────────────
echo "[4/4] Construyendo e iniciando..."

# Añadir fichero de cookies al volumen si existe.
if [ -f cookies.txt ] || [ -f cookies.json ]; then
    docker compose up -d --build
    echo
    echo "Copiando fichero de cookies al volumen..."
    COOKIE_FILE="cookies.txt"
    [ -f cookies.json ] && COOKIE_FILE="cookies.json"
    docker compose cp "$COOKIE_FILE" tiktok-checker:/data/"$COOKIE_FILE"
    docker compose restart
else
    docker compose up -d --build
fi
echo

echo "==========================================="
echo "  ¡Listo! TikTokChecker está corriendo.    "
echo "  Comprobará cada 6 h y alerta por Discord."
echo "==========================================="
echo "  Ver logs:     docker compose logs -f"
echo "  Reiniciar:    docker compose restart"
echo "  Parar:        docker compose down"
echo
echo "  Si las cookies no están en el volumen:"
echo "    docker compose cp cookies.txt tiktok-checker:/data/"
echo "    docker compose restart"

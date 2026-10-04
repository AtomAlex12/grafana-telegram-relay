#!/usr/bin/env bash
# Grafana → Telegram Relay — one-line installer / updater
#
#   curl -fsSL https://raw.githubusercontent.com/AtomAlex12/grafana-telegram-relay/main/install.sh | bash
#
# Options (environment variables):
#   TG_RELAY_DIR    install directory      (default: /opt/tg-relay as root, ~/tg-relay otherwise)
#   TG_RELAY_PORT   host port for the UI   (default: 8095)
#   TG_RELAY_PROXY  proxy for Telegram     (e.g. socks5://host.docker.internal:1080)
#   TG_RELAY_REF    git branch or tag      (default: main)
#
# Re-running the same command updates the relay; data/ and .env are kept.
set -euo pipefail

REPO="AtomAlex12/grafana-telegram-relay"
REF="${TG_RELAY_REF:-main}"
if [ "$(id -u)" -eq 0 ]; then DEFAULT_DIR="/opt/tg-relay"; else DEFAULT_DIR="$HOME/tg-relay"; fi
DIR="${TG_RELAY_DIR:-$DEFAULT_DIR}"

if [ -t 1 ]; then
  B=$'\033[1m'; BLUE=$'\033[34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; N=$'\033[0m'
else
  B=""; BLUE=""; GREEN=""; YELLOW=""; RED=""; N=""
fi
info() { printf '%s==>%s %s\n' "$BLUE$B" "$N" "$*"; }
ok()   { printf '%s ✔ %s %s\n' "$GREEN" "$N" "$*"; }
warn() { printf '%s ! %s %s\n' "$YELLOW" "$N" "$*"; }
die()  { printf '%s ✘ %s %s\n' "$RED" "$N" "$*" >&2; exit 1; }

printf '\n%s  Grafana → Telegram Relay%s\n\n' "$B" "$N"

for cmd in curl tar; do
  command -v "$cmd" >/dev/null 2>&1 || die "Не найдена команда '$cmd' — установите её и повторите."
done

# ---------------------------------------------------------------- docker
command -v docker >/dev/null 2>&1 || die "Docker не найден. Установите его: curl -fsSL https://get.docker.com | sh"

SUDO=""
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    SUDO="sudo"
    warn "Нет прав на Docker у пользователя $(id -un) — использую sudo."
  else
    die "Docker не запущен или нет прав. Запустите Docker или добавьте пользователя в группу docker."
  fi
fi

if $SUDO docker compose version >/dev/null 2>&1; then
  COMPOSE="$SUDO docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="$SUDO docker-compose"
else
  die "Не найден Docker Compose. Установите плагин: https://docs.docker.com/compose/install/"
fi
ok "Docker $($SUDO docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?'), $($COMPOSE version --short 2>/dev/null || echo compose)"

# ---------------------------------------------------------------- download
FIRST_INSTALL=1
[ -f "$DIR/docker-compose.yml" ] && FIRST_INSTALL=0

info "Загружаю $REPO@$REF …"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C "$TMP" --strip-components=1 \
  || die "Не удалось скачать архив репозитория."

mkdir -p "$DIR" || die "Не удалось создать $DIR (запустите от root или задайте TG_RELAY_DIR)."
for f in app Dockerfile docker-compose.yml requirements.txt .env.example README.md LICENSE; do
  [ -e "$TMP/$f" ] || continue
  rm -rf "${DIR:?}/$f"
  cp -R "$TMP/$f" "$DIR/"
done
mkdir -p "$DIR/data"
ok "Файлы в $DIR"

# ---------------------------------------------------------------- .env
ENV_FILE="$DIR/.env"
set_env() {  # set_env KEY VALUE — replace or append
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" > "$tmp"
    cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}
get_env() { grep "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-; }

detect_tz() {
  local tz=""
  tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -z "$tz" ] && [ -f /etc/timezone ] && tz="$(cat /etc/timezone)"
  [ -z "$tz" ] && tz="$(readlink /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p')"
  echo "${tz:-UTC}"
}

if [ ! -f "$ENV_FILE" ]; then
  cp "$DIR/.env.example" "$ENV_FILE"
  set_env TZ "$(detect_tz)"
fi
[ -n "${TG_RELAY_PORT:-}" ] && set_env TG_RELAY_PORT "$TG_RELAY_PORT"
[ -n "${TG_RELAY_PROXY:-}" ] && set_env TG_RELAY_PROXY "$TG_RELAY_PROXY"
PORT="$(get_env TG_RELAY_PORT)"; PORT="${PORT:-8095}"

if [ "$FIRST_INSTALL" = 1 ] && command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]"; then
  die "Порт $PORT уже занят. Укажите другой: TG_RELAY_PORT=8096 и запустите установку снова."
fi
ok "Порт $PORT, часовой пояс $(get_env TZ)"

# ---------------------------------------------------------------- telegram reachability hint
if [ -z "$(get_env TG_RELAY_PROXY)" ]; then
  if curl -s -m 6 -o /dev/null https://api.telegram.org 2>/dev/null; then
    ok "api.telegram.org доступен напрямую — прокси не обязателен"
  else
    warn "api.telegram.org недоступен напрямую — укажите прокси в веб-интерфейсе (раздел «Подключение»)"
  fi
fi

# ---------------------------------------------------------------- start
info "Собираю и запускаю контейнер (первый раз — 1–3 минуты) …"
(cd "$DIR" && $COMPOSE up -d --build --remove-orphans) || die "docker compose завершился с ошибкой."

info "Жду готовности …"
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done
[ "${READY:-0}" = 1 ] || die "Релей не ответил за 60 с. Логи: cd $DIR && $COMPOSE logs"

printf '\n%s%s Готово!%s ' "$GREEN" "$B" "$N"
[ "$FIRST_INSTALL" = 1 ] && echo "Релей установлен." || echo "Релей обновлён."
echo
echo "  Веб-интерфейс:"
IPS="$(hostname -I 2>/dev/null || true)"
if [ -n "$IPS" ]; then
  for ip in $IPS; do
    case "$ip" in *:*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) continue ;; esac
    echo "    http://$ip:$PORT"
  done
fi
echo "    http://localhost:$PORT"
echo
[ "$FIRST_INSTALL" = 1 ] && echo "  При первом входе задайте пароль администратора, затем токен бота во вкладке «Подключение»."
echo "  Обновить:  повторите команду установки"
echo "  Логи:      cd $DIR && $COMPOSE logs -f"
echo "  Удалить:   cd $DIR && $COMPOSE down && rm -rf $DIR"
echo

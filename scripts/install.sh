#!/bin/sh
# Luna installer for macOS and Linux.
#   curl -LsSf https://raw.githubusercontent.com/Dushe4ka/luna-simple/main/scripts/install.sh | sh
# Options: --ref <branch|tag|sha>, --no-modify-path (or LUNA_REF, LUNA_NO_MODIFY_PATH=1).
set -eu

REPO="${LUNA_REPO:-https://github.com/Dushe4ka/luna-simple}"
REF="${LUNA_REF:-main}"
MODIFY_PATH=1
[ "${LUNA_NO_MODIFY_PATH:-0}" = "1" ] && MODIFY_PATH=0

say() { printf '%s\n' "luna-install: $*"; }
die() { printf '%s\n' "luna-install: ошибка: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) [ $# -ge 2 ] || die "--ref требует значение"; REF="$2"; shift 2 ;;
    --no-modify-path) MODIFY_PATH=0; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) die "неизвестный параметр: $1" ;;
  esac
done

fetch() {
  if command -v curl >/dev/null 2>&1; then curl -LsSf "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"
  else die "нужен curl или wget"; fi
}

if ! command -v uv >/dev/null 2>&1; then
  say "ставлю uv (менеджер Python от Astral)…"
  if [ "$MODIFY_PATH" = 1 ]; then fetch https://astral.sh/uv/install.sh | sh
  else fetch https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh; fi
  PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || die "uv установлен, но не найден в PATH"
fi

command -v git >/dev/null 2>&1 || die "нужен git (uv ставит Luna из git-репозитория)"

say "ставлю Luna ($REF)…"
uv tool install --python 3.12 --force "luna-simple[all] @ git+$REPO@$REF"

if [ "$MODIFY_PATH" = 1 ]; then uv tool update-shell >/dev/null 2>&1 || true; fi

BIN="$(uv tool dir --bin)"
"$BIN/luna" doctor || say "doctor нашёл проблемы — подсказки выше"
say "готово. Откройте новый терминал и наберите: luna"

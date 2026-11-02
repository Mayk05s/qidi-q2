#!/bin/bash
# Single entry point for the QIDI tools. Run it and pick an action from the menu.
# Printer address and login come only from .env next to this script.
set -euo pipefail

cd "$(dirname "$0")/box_patch"

HAVE_ENV=0
if [ -f ../.env ]; then
  HAVE_ENV=1
  chmod 600 ../.env
  # shellcheck disable=SC1091
  source ../.env
  export PRINTER_PASSWORD
  if [ -f "$HOME/.ssh/config" ]; then
    /usr/bin/python3 -B sync_ssh_alias.py "$HOME/.ssh/config" "$PRINTER_HOST"
  fi
  API="http://$PRINTER_HOST:7125"
  TARGET="$PRINTER_USER@$PRINTER_HOST"
fi

ASKPASS="$(mktemp)"
finish() {
  rm -f "$ASKPASS"
}
trap finish EXIT
chmod 700 "$ASKPASS"
printf '#!/bin/sh\nprintf "%%s\\n" "$PRINTER_PASSWORD"\n' > "$ASKPASS"
export SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

json_field() {
  /usr/bin/python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1:]: d=d[k]
print(d)' "$@"
}

require_idle() {
  local state
  state="$(curl -fsS -m 8 "$API/printer/objects/query?print_stats=state" \
    | json_field result status print_stats state 2>/dev/null)" || state=unknown
  case "$state" in
    printing|paused)
      echo "Принтер сейчас печатает ($state). Запусти скрипт после печати." >&2
      exit 1 ;;
  esac
}

# Installs and removes through the patcher, which reads box_patch/packages/*/package.json:
# no file list lives here. It restarts Klipper only when needed and rolls back on failure.
patcher() {
  /usr/bin/python3 -B ../installer/qidi_backend.py apply "$@" | /usr/bin/python3 -c '
import json, sys
ok = True
for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        print(line, end="")
        continue
    if message.get("type") in ("log", "done"):
        print(message["text"])
    if message.get("type") == "done":
        ok = message["ok"]
sys.exit(0 if ok else 1)'
}

run_gcode() {
  curl -fsS -m 75 -X POST "$API/printer/gcode/script" --get \
    --data-urlencode "script=$1" >/dev/null
}

# Sends a command to the given readers and prints what Klipper answered.
report() {
  local command="$1" reader since
  shift
  since="$(/bin/date +%s)"
  for reader in "$@"; do
    run_gcode "$command READER=$reader" 2>/dev/null || true
  done
  sleep 1
  curl -fsS -m 8 "$API/server/gcode_store?count=20" | /usr/bin/python3 -c '
import json, sys
since = float(sys.argv[1])
for item in json.load(sys.stdin)["result"]["gcode_store"]:
    if item["time"] >= since - 2 and item["type"] == "response":
        print(item["message"])' "$since"
}

# Succeeds when both chips answered with a real version number.
diag() {
  local output
  output="$(report BAMBU_RFID_DIAG card_reader_1 card_reader_2)"
  echo "--- Диагностика ---"
  echo "$output"
  [ "$(grep -E 'Version=0x' <<<"$output" | grep -cvE 'Version=0x(00|FF)')" -eq 2 ]
}

run_action() {
  ACTION="$1"
  if [ "$HAVE_ENV" -eq 0 ]; then
    echo "Нет файла .env рядом с setup.sh." >&2
    exit 1
  fi
  case "$ACTION" in
    install)
      patcher
      ;;
    diag)
      require_idle
      if diag; then
        echo "=== Готово: оба считывателя отвечают ==="
      else
        echo "=== Считыватели ответили не так, как ожидалось ===" >&2
        exit 1
      fi
      ;;
    read)
      require_idle
      read -r -p "Слот с катушкой (1-4): " SLOT
      case "$SLOT" in
        1|2) READER=card_reader_1 ;;
        3|4) READER=card_reader_2 ;;
        *) echo "Нет такого слота: $SLOT" >&2; exit 2 ;;
      esac
      echo "Жду метку 30 секунд ($READER). Медленно проверни катушку рукой на полный оборот."
      echo "Филамент в это время не вставляй: штатное чтение будет мешать."
      report "BAMBU_RFID_READ TIMEOUT=30" "$READER"
      echo "=== Чтение завершено, результат выше ==="
      ;;
    uninstall)
      INSTALLED="$(/usr/bin/python3 -B ../installer/qidi_backend.py status \
        | /usr/bin/python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)["features"]))')"
      patcher --remove "$INSTALLED"
      ;;
  esac
}

# The material list comes from the printer's own firmware file, so a firmware update
# that adds materials shows up here. Without a connection the last copy stays in use.
refresh_materials() {
  [ "$HAVE_ENV" -eq 1 ] || return 0
  local copy
  copy="$(mktemp)"
  if scp -q -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
      "$TARGET:/home/qidi/QIDI_Client/resource/filament_config/filament-0.4.json" "$copy" 2>/dev/null; then
    /usr/bin/python3 -B packages/panel/printer/qidi_tag_tool.py --import-materials "$copy" || true
  else
    echo "Принтер недоступен, использую сохранённый список материалов."
  fi
  rm -f "$copy"
}

open_tag_tool() {
  refresh_materials
  echo "Панель: http://127.0.0.1:18770 (вернуться в меню: Ctrl+C)"
  (sleep 1; open "http://127.0.0.1:18770") &
  trap ':' INT
  /usr/bin/python3 -B packages/panel/printer/qidi_tag_tool.py || true
  trap - INT
}

# Actions that change the printer run only after an explicit yes; Enter means no.
confirm() {
  local answer
  read -r -p "$1 (y/n): " answer || return 1
  case "$answer" in
    y|Y|д|Д|да|Да|ДА) return 0 ;;
  esac
  echo "Отменено."
  return 1
}

while true; do
  echo
  echo "QIDI Box: чтение меток Bambu"
  echo "  1) Установить или обновить модуль и панель на принтере"
  echo "  2) Запустить диагностику считывателей"
  echo "  3) Прочитать метку Bambu на принтере"
  echo "Запись меток (ридер на этом компьютере)"
  echo "  4) Открыть панель здесь"
  echo
  echo "  0) Удалить модуль и панель с принтера (откат)"
  echo "Выход: Ctrl+C"
  read -r -p "Выбор: " CHOICE || break
  case "$CHOICE" in
    1) confirm "Установить модуль? Klipper перезапустится" || continue; ACTION=install ;;
    2) ACTION=diag ;;
    3) ACTION=read ;;
    4) open_tag_tool; continue ;;
    0) confirm "Удалить модуль с принтера? Klipper перезапустится" || continue; ACTION=uninstall ;;
    *) echo "Нет такого пункта: $CHOICE" >&2; continue ;;
  esac
  # A subshell keeps a failed action from closing the menu.
  set +e
  (set -e; run_action "$ACTION")
  RC=$?
  set -e
  if [ "$RC" -ne 0 ]; then
    echo "=== Не выполнено: действие остановилось с ошибкой (код $RC) ===" >&2
  fi
done

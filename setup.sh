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
KEY="$HOME/.ssh/qidi/id_ed25519"

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

printer_state() {
  curl -fsS -m 5 "$API/printer/info" 2>/dev/null | json_field result state 2>/dev/null || echo unavailable
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

# An error state can be stale right after a restart, so it must persist to count.
wait_ready() {
  local errors=0
  for _ in $(seq 1 45); do
    case "$(printer_state)" in
      ready) return 0 ;;
      error|shutdown)
        errors=$((errors + 1))
        [ "$errors" -ge 8 ] && return 1 ;;
    esac
    sleep 2
  done
  return 1
}

# remote() restarts the klipper service, because a RESTART command keeps the
# process and with it the previously imported module code.
restart_klipper() {
  sleep 5
  wait_ready && return 0
  curl -fsS -m 10 -X POST "$API/printer/firmware_restart" >/dev/null || true
  sleep 8
  wait_ready
}

remote() {
  local info klipper cfg
  info="$(curl -fsS -m 8 "$API/printer/info")"
  klipper="$(json_field result klipper_path <<<"$info")"
  cfg="$(json_field result config_file <<<"$info")"
  # Everything goes into one folder, unpacked on the printer next to remote_setup.py.
  local bundle
  bundle="$(mktemp -d)"
  cp remote_setup.py screen_popup_setup.py bambu_rfid.py box_feed.py bambu_map.cfg qidi_panel_link.js qidi-panel.service \
     "$HOME/.ssh/qidi/id_ed25519.pub" ../tag_writer/qidi_tag_tool.py ../tag_writer/print_check.py ../tag_writer/fluidd_card.py ../tag_writer/screen_popup.py ../tag_writer/plastic_picture.py ../tag_writer/slot_font.json ../tag_writer/sample_model.py \
     ../tag_writer/sample_font.json ../tag_writer/sample_project_settings.json \
     ../tag_writer/qidi_materials.json "$bundle/"
  for name in my_materials.json my_colors.json; do
    [ -f "../tag_writer/$name" ] && cp "../tag_writer/$name" "$bundle/"
  done
  ssh "${SSH_OPTS[@]}" "$TARGET" "rm -rf /tmp/qidi_setup" </dev/null
  scp -q -r "${SSH_OPTS[@]}" "$bundle" "$TARGET:/tmp/qidi_setup"
  rm -rf "$bundle"
  printf '%s\n' "$PRINTER_PASSWORD" | ssh "${SSH_OPTS[@]}" "$TARGET" \
    "chmod -R a+rX /tmp/qidi_setup; sudo -S -p '' sh -c 'python3 /tmp/qidi_setup/remote_setup.py $1 --klipper \"$klipper\" --cfg \"$cfg\" \
     --module /tmp/qidi_setup/bambu_rfid.py --map /tmp/qidi_setup/bambu_map.cfg --pubkey /tmp/qidi_setup/id_ed25519.pub \
     --panel /tmp/qidi_setup --user \"$PRINTER_USER\" && systemctl restart klipper'; \
     rc=\$?; rm -rf /tmp/qidi_setup; exit \$rc"
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

ensure_key() {
  if [ ! -f "$KEY" ]; then
    mkdir -p -m 700 "$(dirname "$KEY")"
    ssh-keygen -q -t ed25519 -N '' -C 'mac-to-qidi-q2' -f "$KEY"
  fi
}

run_action() {
  ACTION="$1"
  if [ "$HAVE_ENV" -eq 0 ]; then
    echo "Нет файла .env рядом с setup.sh." >&2
    exit 1
  fi
  case "$ACTION" in
    install)
      require_idle
      ensure_key
      echo "Устанавливаю на $PRINTER_HOST..."
      remote install
      echo "Перезапускаю Klipper..."
      if ! restart_klipper; then
        echo "Klipper не запустился: $(curl -fsS -m 5 "$API/printer/info" \
          | json_field result state_message 2>/dev/null || echo 'нет ответа')" >&2
        echo "Откатываю изменения..." >&2
        remote uninstall
        restart_klipper || echo "Klipper не поднялся и после отката, проверь Fluidd." >&2
        exit 1
      fi
      if ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o IdentitiesOnly=yes -i "$KEY" "$TARGET" true 2>/dev/null; then
        echo "ssh qidi: вход по ключу работает."
      else
        echo "ssh qidi: вход по ключу пока не работает, на установку модуля это не влияет."
      fi
      if diag; then
        if curl -fsS -m 10 -o /dev/null "http://$PRINTER_HOST:18770/"; then
        echo "Панель: http://$PRINTER_HOST:18770 (кнопка внизу меню Fluidd)"
      else
        echo "Панель пока не отвечает на порту 18770." >&2
      fi
      echo "=== Готово: модуль установлен, Klipper работает, оба считывателя отвечают ==="
      else
        echo "=== Модуль установлен, но считыватели ответили не так, как ожидалось ===" >&2
        exit 1
      fi
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
      require_idle
      ensure_key
      remote uninstall
      restart_klipper || echo "Klipper не вернулся в ready, проверь Fluidd." >&2
      echo "=== Готово: модуль удалён, секции убраны из printer.cfg ==="
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
    /usr/bin/python3 -B ../tag_writer/qidi_tag_tool.py --import-materials "$copy" || true
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
  /usr/bin/python3 -B ../tag_writer/qidi_tag_tool.py || true
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

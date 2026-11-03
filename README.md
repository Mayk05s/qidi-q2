# Инструменты для QIDI Q2 и QIDI Box

Набор пакетов, которые добавляют QIDI Q2 с QIDI Box новые возможности, и программа **QIDI Q2 Patcher**, которая их ставит, обновляет и удаляет.

> **Неофициальный проект.** Он не связан с QIDI Tech и Bambu Lab и ими не поддерживается. Пакеты меняют настройки Klipper и поведение Box на твоём принтере. Всё делается на твой страх и риск, без каких-либо гарантий (см. [LICENSE](LICENSE)). Проверено только на прошивке `QD_Q2_01.01.03.02`.

## Что нужно

- Mac на Apple Silicon с macOS 13 или новее и Xcode Command Line Tools (`xcode-select --install`): из них берутся `swiftc` для сборки программы и Python 3. Или Windows, тогда см. [Windows](#windows).
- QIDI Q2 с QIDI Box в той же сети, что и Mac.
- Для записи меток на компьютере: ридер ACR122U (не обязательно).

## QIDI Q2 Patcher

Программа собирается из исходников, в репозитории готовой нет. Собери её один раз и после каждого обновления кода:

```bash
python3 -B installer/build_app.py
```

Появится `QIDI Q2 Patcher.app` в корне проекта. Открой её двойным кликом или из терминала:

```bash
open "QIDI Q2 Patcher.app"
```

Слева выбирается принтер: из найденных в сети или кнопкой «Добавить принтер» по адресу. Для входа используются стандартные логин и пароль QIDI, свои задаются карандашом у выбранного принтера. Адрес и свой вход хранятся в `.env` открытым текстом, не выкладывай этот файл.

Справа список пакетов. Отметь нужные и нажми «Установить» или «Применить»: всё выбранное ставится и удаляется за один раз. Пакеты, которые не менялись, заново не копируются. Klipper перезапускается, только если затронут пакет, который он читает, а если после этого он не поднялся, программа возвращает прежний набор. Устаревшие пакеты помечены «на принтере старая версия», кнопка тогда называется «Обновить». Менять можно только когда принтер не печатает. Посмотреть окно без принтера: `open "QIDI Q2 Patcher.app" --args --demo`.

Перед каждой установкой программа сама сохраняет резервную копию принтера на этом компьютере, в `~/Library/Application Support/QIDI Q2 Patcher/backups/<адрес принтера>`: настройки Klipper, файлы пакетов, панель, кнопку в Fluidd и окна экрана. Что копировать, берётся из описаний пакетов (их файлы и поле `backup`). Кнопка «Восстановить» справа сверху показывает копии, делает новую, загружает копию из файла (сделанную раньше и перенесённую куда-то ещё) и возвращает принтер к выбранной: сохранённые файлы встают на место, а то, что патч добавил после копии, удаляется. Хранится 15 последних копий и самая первая.

Справка по каждому пакету открывается в «Подробнее», по всем сразу кнопкой «?» справа сверху: что пакет делает, какие файлы добавляет на принтер и какие штатные меняет.

### Windows

Для Windows есть та же программа одним файлом: `QIDI Q2 Patcher.exe`. Окно то же, только сделано на tkinter (`installer/qidi_window.py`), а работу выполняет тот же `installer/qidi_backend.py`. Пакеты зашиты внутрь exe, поэтому папка проекта для работы не нужна. Python и ssh ставить не надо: к принтеру программа подключается сама, через paramiko.

Exe собирает GitHub: вкладка Actions → «Windows exe» → «Run workflow», готовый файл появится в Artifacts этого запуска. Сборка запускается и сама при теге `v*`. Собрать на своём компьютере с Windows (Python 3.9 или новее):

```bash
git config --global core.autocrlf false
pip install pyinstaller paramiko
python -B installer/build_exe.py
```

Первую строку нужно выполнить до клонирования: файлы для принтера должны остаться с переводами строк Unix, иначе сборка остановится. Готовый файл появится в `dist\`.

Программа не подписана, поэтому при первом запуске Windows покажет «Система Windows защитила ваш компьютер». Нажми «Подробнее» → «Выполнить в любом случае». Адрес принтера, вход, резервные копии и журнал лежат в `%APPDATA%\QIDI Q2 Patcher`. Без принтера окно открывается так: `"QIDI Q2 Patcher.exe" --demo`.

На настоящем Windows exe ещё не запускался. Окно проверено на Mac в демо-режиме, а подключение через paramiko проверено на принтере.

## Пакеты

| Пакет | Папка |
|---|---|
| Метки Bambu | [box_patch/packages/bambu](box_patch/packages/bambu/README.md) |
| NFC-метки | [box_patch/packages/nfc](box_patch/packages/nfc/README.md) |
| Мультиподача | [box_patch/packages/spools](box_patch/packages/spools/README.md) |
| Расход пластика | [box_patch/packages/usage](box_patch/packages/usage/README.md) |
| Быстрая печать | [box_patch/packages/mesh](box_patch/packages/mesh/README.md) |
| Справка по пластику | [box_patch/packages/guide](box_patch/packages/guide/README.md) |
| Модуль Klipper для Box, ставится вместе с пакетами, которым нужен | [box_patch/packages/rfid_core](box_patch/packages/rfid_core/README.md) |
| Панель на принтере, ставится вместе с пакетами, которым нужна | [box_patch/packages/panel](box_patch/packages/panel/README.md) |

Каждый пакет сам описывает себя в своей папке: `package.json` (версия, зависимости, какие файлы откуда и куда ставятся, опции Klipper, что меняется в чужих файлах) и `README.md` (справка). Патчер только читает эти описания. Правило записано в `CLAUDE.md`.

Новый пакет: папка `box_patch/packages/<id>/` с `package.json` и `README.md`, образец `box_patch/packages/spools/`. После правки пакета подними его `version` и общую версию в `installer/patcher.json`: исправление 1.0.1, новая возможность 1.1.0.

## Что меняется на принтере

Все наши секции Klipper лежат в своём файле `qidi_patcher.cfg` рядом с `printer.cfg`, а в `printer.cfg` появляется одна строка `[include qidi_patcher.cfg]`. Что установлено, версии и прошивка на момент установки записаны в `qidi_patcher.json` там же. Перед первой установкой рядом сохраняется копия `printer.cfg.before_bambu_rfid`.

Что меняет каждый пакет, написано в его `README.md` и `package.json` и видно в программе.

Файлы `box_*.so` и прошивка контроллера Box не меняются: модуль перехватывает их функции только в памяти Klipper. Поэтому всё зависит от версии прошивки: проверено только на `QD_Q2_01.01.03.02`, на более ранней или новой может не заработать. Обновление прошивки может стереть наши файлы, тогда нажми «Обновить».

## Меню в терминале

Прежний способ, без окна:

```bash
./setup.sh
```

Пункт 1 ставит или обновляет то, что уже стоит, через тот же патчер; пункт 0 удаляет всё. Пункты 2 и 3: диагностика считывателей и чтение метки Bambu. Пункт 4 открывает панель на этом компьютере, для записи меток через ридер ACR122U.

## Что где лежит

- `installer/`: QIDI Q2 Patcher. `patcher.json` хранит общую версию, проверенные прошивки и риски, остальное приходит из пакетов. Окно для Mac: `QidiInstaller.swift`, для Windows: `qidi_window.py`. Обе программы запускают `qidi_backend.py`, а `ssh_link.py` подключает его к принтеру.
- `box_patch/packages/<id>/`: пакеты. В корне пакета `README.md`, `package.json` и, если нужен, шаг установки; в `printer/` файлы для принтера, в `tests/` тесты, в `tools/` инструменты для Mac.
- `box_patch/remote_setup.py`: выполняется на принтере при установке, читает описания пакетов.
- Модули Klipper: `rfid_core/printer/bambu_rfid.py` (считыватели и метки Bambu), `nfc/printer/bambu_nfc.py` (запись своих меток), `usage/printer/bambu_spool_usage.py` (учёт остатка и окно экрана).
- Панель: `box_patch/packages/panel/printer/qidi_tag_tool.py`.
- `tag_writer/`: проверка пластика в QIDI Studio (`studio_check.py`, ставится `python3 -B tag_writer/install_studio_check.py`, сейчас не используется).
- `NEXT_STEPS.md`: текущее состояние и планы.
- `CLAUDE.md`: правила проекта для тех, кто делает пакеты, в том числе для ИИ-помощника.

Тесты: `python3 -B -m unittest` в `box_patch`, `tag_writer`, `installer` и в `tests/` каждого пакета.

## Лицензия и благодарности

Код распространяется по [GNU GPL v3](LICENSE), как и Klipper, в который ставятся модули.

Чтение меток Bambu Lab опирается на открытое исследование формата меток [Bambu Research Group, RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide).

## In English

Unofficial tools for the QIDI Q2 printer with the QIDI Box: reading Bambu Lab RFID tags, writing your own NFC tags, parallel spool feeding, filament usage tracking and an adaptive bed mesh. They are installed by **QIDI Q2 Patcher**, a macOS app built from this repository with `python3 -B installer/build_app.py`, or a Windows .exe built by the "Windows exe" GitHub Actions workflow (`installer/build_exe.py`). The interface and documentation are in Russian. Not affiliated with QIDI Tech or Bambu Lab. Tested only on firmware `QD_Q2_01.01.03.02`. Use at your own risk. License: GPL-3.0.

# holotek — CO₂ Monitor Notification Daemon / Мониторинг CO₂ с уведомлениями

**EN:** Python CLI daemon that reads CO₂ ppm from a USB zyTemp (Holtek) HID device on macOS and fires native notifications on zone transitions. [🌐 Landing page](https://dimasmagadan.github.io/holotek/) · [📦 Device on Ozon](https://www.ozon.ru/product/detektor-uglekislogo-gaza-dadzhet-izmeritel-co2-datchik-co2-analizator-vozduha-227430204/)

---

Python-демон для macOS, который читает концентрацию CO₂ с USB-датчика zyTemp (Holtek) и отправляет системные уведомления при переходе между зонами (норма → повышение → опасно).

🌐 [Лендинг](https://dimasmagadan.github.io/holotek/) · 🛒 [Датчик на Ozon](https://www.ozon.ru/product/detektor-uglekislogo-gaza-dadzhet-izmeritel-co2-datchik-co2-analizator-vozduha-227430204/) · 💻 [Репозиторий](https://github.com/Dimasmagadan/holotek)

## История

Искал датчик CO₂. Сначала хотел найти что-то с Wi-Fi и оповещениями, но ничего подходящего в адекватном ценовом диапазоне не нашёл. Плюс начитался статей, что в дешёвых Wi-Fi-датчиках ставят плохие сенсоры.

Поэтому выбрал, как мне казалось, «тупой» прибор — только с цветовой индикацией на корпусе и цифрами на экране, но с качественным сенсором. Подключил его к компьютеру через USB и забыл.

Через некоторое время заметил, что в списке доступных устройств OrbStack появилось новое USB-устройство. Полез разбираться — оказалось, датчик не только питание по USB получает, но и данные передаёт.

Дописал эту программу — теперь приходят уведомления.

## Установка (macOS)

```bash
brew install libusb hidapi
pip install -r requirements.txt
```

Проверить, что датчик определяется:

```bash
python3 -c "import co2meter; m=co2meter.CO2monitor(); print(m.read_data_raw())"
# Ожидается: (datetime, co2_int, temp_float)
```

Если ошибка доступа — macOS HID-драйвер занял устройство. Запустите ту же команду с `sudo` один раз, затем отключите и снова подключите датчик. После этого доступ из пользовательского пространства работает без `sudo`.

Если зависает — установите `"bypass_decrypt": true` в `config.json`.

## Использование

```bash
# Меню-бар — любой из способов:
#   двойной клик по «Holotek Launcher.app» в Finder   ← самый простой
./start.sh                              # то же из терминала, отвязан

python3 holotek.py [--config ...]       # фоновый режим в терминале
```

- **Меню-бар**: показывает цветной кружок CO₂ в строке меню (🟢 норма, 🟡 повышение, 🔴 опасно). Клик по иконке показывает текущее показание (`CO₂: NNN ppm`) и время замера; выход — через Quit. Отправляет нативные уведомления macOS. Процесс отвязан от терминала — окно можно закрывать.
  - **`Holotek Launcher.app`** — двойной клик в Finder. Это AppleScript-обёртка, собранная `osacompile`; в отличие от рукотворного bundle она зарегистрирована в LaunchServices, поэтому запуск двойным кликом работает надёжно. Запускает `start.sh` отвязанно и сразу завершается. Собрать/пересобрать: `./make-launcher.sh`.
  - **`./start.sh`** — то же из терминала: процесс через `nohup`/`disown`, логи в `/tmp/holotek_app.log`.
- **Фоновый режим** (`holotek.py`): работает в терминале, отправляет `osascript`-уведомления, Ctrl+C для выхода.
- Меню-бар построен на «сыром» AppKit/PyObjC (`NSStatusItem`, `NSTimer`), не на `rumps`: статус-иконка создаётся в `applicationDidFinishLaunching_`, а таймер и пункт Quit нацелены на `NSObject`-делегат (PyObjC доставляет селекторы только Cocoa-объектам).
- Одиночный экземпляр контролируется lock-файлом с PID (оба режима): «мёртвый» lock завершившегося процесса автоматически перехватывается.

## Конфигурация

| Ключ | По умолчанию | Диапазон | Описание |
|---|---|---|---|
| `thresholds.green_max` | 800 | целое ≥ 0 | верхняя граница зелёной зоны (ppm) |
| `thresholds.yellow_max` | 1200 | целое ≥ green_max | верхняя граница жёлтой зоны (ppm) |
| `poll_interval_seconds` | 120 | > 0 | секунд между опросами датчика |
| `notification_cooldown_seconds` | 1800 | ≥ 0 | секунд между повторными уведомлениями в одной зоне |
| `green_reentry_drop_ppm` | 200 | ≥ 0 | падение ppm, при котором «возврат в норму» срабатывает мгновенно даже внутри задержки |
| `bypass_decrypt` | false | boolean | отключить XOR-дешифровку для устройств без шифрования |

Конфиг горячо перезагружается при каждом опросе — можно менять на лету.

## Зоны CO₂

| Зона | Условие |
|---|---|
| 🟢 Зелёная | `ppm ≤ green_max` |
| 🟡 Жёлтая | `green_max < ppm ≤ yellow_max` |
| 🔴 Красная | `ppm > yellow_max` |

## Политика уведомлений

- Первый замер после запуска не отправляет уведомление.
- Эскалация (зелёный→жёлтый, зелёный→красный, жёлтый→красный) срабатывает сразу.
- Повторное уведомление в той же зоне (жёлтой/красной) подавляется в течение задержки, срабатывает снова после.
- Улучшение в пределах тревоги (красный→жёлтый) ограничено по частоте и сохраняет исходное показание.
- Возврат в зелёную зону с падением ≥ `green_reentry_drop_ppm` не учитывает задержку.
- Возврат в зелёную зону с малым падением учитывает задержку.

## Структура проекта

```
holotek/
├── holotek.py        # основной цикл демона (argparse, сигналы, lockfile, опрос)
├── menubar.py        # меню-бар на AppKit/PyObjC (HolotekApp, NSStatusItem)
├── core.py           # общая логика (зоны, конфиг, уведомления)
├── config.json       # пороги и тайминги (горячая перезагрузка)
├── start.sh          # запуск меню-бара отвязанно от терминала
├── make-launcher.sh  # сборка «Holotek Launcher.app» через osacompile
├── test_core.py      # модульные тесты
├── requirements.txt  # зависимости
└── docs/             # лендинг GitHub Pages
```

> «Holotek Launcher.app» собирается локально (`./make-launcher.sh`) и не хранится в git.

## Тесты

```bash
pip install pytest
python3 -m pytest test_core.py -v
```

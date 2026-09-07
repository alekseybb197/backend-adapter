# Настройка [CC] для работы через backend-adapter

> Руководство по настройке клиента [CC] ([CC]) на работу через
> backend-adapter. Типовые файлы — в `docs/claude_code/`:
> `global.settings.json` (профиль пользователя), `project.settings.json`
> (проектные настройки), `statusline.sh` (скрипт статус-строки).
> Установка и настройка самого адаптера — `docs/install.md`.

---

## 1. Как [CC] подключается к адаптеру

Адаптер поднимает локальный HTTP-эндпоинт (по умолчанию
`http://127.0.0.1:9999`), реализующий Anthropic Messages API
(см. `docs/install.md`, раздел 2). Чтобы клиент ходил в адаптер, а не в
официальный API Anthropic, в его окружении задаются:

| Переменная | Значение | Назначение |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://localhost:9999` | Точка подключения — адрес адаптера |
| `ANTHROPIC_API_KEY` | пусто | Авторизацию на бэкенде выполняет адаптер |
| `ANTHROPIC_AUTH_TOKEN` | `dummy` | Заглушка токена для клиентов, требующих непустой токен |

Переменные задаются в блоке `env` файлов настроек (см. ниже) либо в
окружении shell. **Адаптер эти переменные не читает** — ему нужны только
собственные `ADAPTER_*` (см. `docs/environment.md`). `ANTHROPIC_*` —
настройки процесса клиента.

---

## 2. Файлы настроек: расположение и формат

[CC] читает настройки из нескольких файлов `settings.json`:

| Файл | Уровень | Назначение | В git |
|---|---|---|---|
| `~/.claude/settings.json` | Пользовательский | Общие настройки для всех проектов на машине | не коммитится (личный) |
| `<project>/.claude/settings.json` | Проектный (shared) | Настройки команды проекта | **коммитится** |
| `<project>/.claude/settings.local.json` | Проектный, личный | Персональные поверх проектных | не коммитится |

Правила:

- настройки **сливаются** от пользовательских к проектным к локальным
  (приоритет у более конкретного файла); `env`-блоки объединяются;
- **формат — строгий JSON**: комментарии `//`, хвостовые запятые — ошибка
  синтаксиса ([CC] покажет `[Settings Error]` при старте). Поэтому в этом
  руководстве для каждого файла дан копируемый JSON без комментариев,
  а пояснения по ключам — таблицей в тексте;
- в репозиторий backend-adapter каталог `.claude/` **не коммитится**
  (записан в `.gitignore`), поэтому типовые файлы лежат в
  `docs/claude_code/` и копируются вручную (раздел 5). Для **других**
  проектов проектный `.claude/settings.json`, наоборот, обычно коммитят;
- для автодополнения можно подключить JSON-схему:
  `"$schema": "https://json.schemastore.org/claude-code-settings.json"`.

---

## 3. Пользовательский профиль — `~/.claude/settings.json`

Типовой файл — [`docs/claude_code/global.settings.json`](claude_code/global.settings.json)
(в нём в качестве имени модели стоит `qwen36` — значение вашего бэкенда;
ниже в примере имя заменено на плейсхолдер `model-name`). Скопируйте в
`~/.claude/settings.json` и при необходимости поправьте:

```json
{
  "plansDirectory": "./tmp/plans",
  "enableAllProjectMcpServers": true,
  "language": "russian",
  "autoUpdatesChannel": "stable",
  "cleanupPeriodDays": 7,
  "attribution": {
    "commit": "",
    "pr": ""
  },
  "spinnerTipsEnabled": false,
  "respectGitignore": false,
  "alwaysThinkingEnabled": false,
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:9999",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "dummy",

    "ANTHROPIC_MODEL": "model-name",
    "ANTHROPIC_DEFAULT_MODEL": "model-name",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "262144",

    "ANTHROPIC_DEFAULT_OPUS_MODEL": "model-name",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "model-name",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "model-name",
    "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",

    "CLAUDE_CODE_HIDE_ACCOUNT_INFO": "1",

    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",

    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_COST_WARNINGS": "1",

    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
    "ENABLE_TOOL_SEARCH": "auto:5",
    "MAX_THINKING_TOKENS": "50000",
    "MAX_MCP_OUTPUT_TOKENS": "50000",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    "BASH_DEFAULT_TIMEOUT_MS": "300000",
    "BASH_MAX_TIMEOUT_MS": "600000",
    "MCP_TIMEOUT": "60000",
    "MCP_TOOL_TIMEOUT": "120000",
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1"
  },
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh",
    "padding": 0
  }
}
```

### 3.1 Комментарии по ключам

#### Собственные настройки клиента

| Ключ | Значение | Назначение |
|---|---|---|
| `plansDirectory` | `"./tmp/plans"` | Куда режим планирования пишет файлы планов. `./` — относительно текущего проекта: планы остаются внутри репозитория |
| `enableAllProjectMcpServers` | `true` | Автоматически одобрять все MCP-серверы из проектных `.mcp.json`, не спрашивая |
| `language` | `"russian"` | Язык интерфейса и ответов |
| `autoUpdatesChannel` | `"stable"` | Канал обновлений клиента |
| `cleanupPeriodDays` | `7` | Сколько дней хранить транскрипты сессий, после чего удалять |
| `attribution.commit` | `""` | Подпись-трейлер, добавляемая в коммиты (пусто — не добавлять/скрыть) |
| `attribution.pr` | `""` | Строка атрибуции в описаниях pull request (пусто — не добавлять/скрыть) |
| `spinnerTipsEnabled` | `false` | Прятать подсказки в спиннере, пока клиент работает |
| `respectGitignore` | `false` | Показывать в `@`-пикере файлов в том числе gitignored-файлы (иначе они скрыты) |
| `alwaysThinkingEnabled` | `false` | Управление расширенным мышлением по умолчанию: `true` — включать для всех сессий, `false` — выключено по умолчанию (включается по запросу). Детальную семантику сверяйте с официальным settings-reference |
| `env` | объект | Переменные окружения для каждой сессии и её подпроцессов (см. 3.2) |
| `statusLine` | объект | Рисовать свою строку состояния (см. раздел 6) |

#### Блок `env`

**Подключение к адаптеру:**

| Переменная | Значение | Назначение |
|---|---|---|
| `ANTHROPIC_BASE_URL` | `http://localhost:9999` | Адрес адаптера (раздел 1) |
| `ANTHROPIC_API_KEY` | `""` | Пусто — авторизацию делает адаптер |
| `ANTHROPIC_AUTH_TOKEN` | `"dummy"` | Заглушка токена |

**Модель и роли.** Имя модели должно приниматься бэкендом (напрямую или
через маппинг моделей адаптера — `ADAPTER_MODELS_MAPPING`). В типовом
файле это `qwen36`; для другого бэкенда подставьте своё имя. Если бэкенд
один, всем «ролям» можно задать одну модель.

| Переменная | Назначение |
|---|---|
| `ANTHROPIC_MODEL` | Модель по умолчанию |
| `ANTHROPIC_DEFAULT_MODEL` | То же (алиас) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Модель для роли «opus» (сложные задачи) |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Модель для роли «sonnet» (основной режим) |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Модель для роли «haiku» (быстрые задачи) |
| `CLAUDE_CODE_SUBAGENT_MODEL` | Модель для субагентов (дочерних агентов) |

**Размер контекста:**

| Переменная | Значение | Назначение |
|---|---|---|
| `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `262144` | Локальный размер контекстного окна (клиент считает компактизацию от него) |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | `80` | Автокомпактизация при заполнении окна на 80% |

**Конфиденциальность и трафик** (при работе через локальный адаптер
внешние сервисы Anthropic не используются, поэтому отключается всё
лишнее):

| Переменная | Назначение |
|---|---|
| `CLAUDE_CODE_HIDE_ACCOUNT_INFO` | Скрыть информацию об аккаунте |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Отключить необязательный трафик |
| `DISABLE_TELEMETRY` | Не отправлять метрики использования |
| `DISABLE_ERROR_REPORTING` | Не отправлять отчёты об ошибках |
| `DISABLE_AUTOUPDATER` | Отключить автообновления |
| `DISABLE_COST_WARNINGS` | Не показывать предупреждения о расходе средств |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | Не добавлять заголовок атрибуции |
| `DISABLE_NON_ESSENTIAL_MODEL_CALLS` | Не делать фоновые (необязательные) вызовы модели |

**Прочее:**

| Переменная | Значение | Назначение |
|---|---|---|
| `ENABLE_TOOL_SEARCH` | `auto:5` | Автоматический поиск инструментов (до 5 результатов) |
| `MAX_THINKING_TOKENS` | `50000` | Лимит токенов на extended thinking |
| `MAX_MCP_OUTPUT_TOKENS` | `50000` | Лимит вывода MCP-инструментов |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `32000` | Лимит ответа модели |
| `BASH_DEFAULT_TIMEOUT_MS` | `300000` | Таймаут bash-команд по умолчанию (5 мин) |
| `BASH_MAX_TIMEOUT_MS` | `600000` | Максимальный таймаут bash-команд (10 мин) |
| `MCP_TIMEOUT` | `60000` | Таймаут запросов к MCP-серверам |
| `MCP_TOOL_TIMEOUT` | `120000` | Таймаут выполнения MCP-инструмента |

> Примечание: при необходимости в `settings.json` можно добавить блок
> `mcpServers` (внешние MCP-серверы, например chrome-devtools). В типовой
> файл он не включён — у каждого пользователя свой набор.

---

## 4. Проектные настройки — `<project>/.claude/settings.json`

Типовой файл — [`docs/claude_code/project.settings.json`](claude_code/project.settings.json).
Для backend-adapter каталог `.claude/` в репозитории игнорируется, поэтому
файл используется как образец для **других** проектов. Копируемый JSON:

```json
{
  "alwaysThinkingEnabled": true,
  "env": {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
```

### 4.1 Комментарии по ключам

| Ключ | Значение | Назначение |
|---|---|---|
| `alwaysThinkingEnabled` | `true` | Для этого проекта расширенное мышление включено по умолчанию — в отличие от пользовательского профиля, где оно выключено (`false`, раздел 3). Проектные значения при слиянии имеют приоритет |
| `env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `"1"` | Проектное значение: отключать необязательный трафик (телеметрию). Сливается с глобальным `env` |

---

## 5. Установка типовых файлов

```bash
# 1. Пользовательский профиль
cp docs/claude_code/global.settings.json ~/.claude/settings.json
#    поправьте в нём имена моделей (раздел 3.2) — файл должен остаться
#    валидным JSON (без комментариев)

# 2. Скрипт статус-строки (путь в statusLine.command должен совпадать)
cp docs/claude_code/statusline.sh ~/.claude/statusline.sh
chmod +x ~/.claude/statusline.sh

# 3. Проектные настройки — для конкретного проекта (не для этого репозитория)
mkdir -p /path/to/project/.claude
cp docs/claude_code/project.settings.json /path/to/project/.claude/settings.json
```

---

## 6. Статус-строка — `statusline.sh`

Типовой скрипт — [`docs/claude_code/statusline.sh`](claude_code/statusline.sh).

**Назначение:** рисует внизу интерфейса строку состояния — модель, ветку
git, заполненность контекстного окна, токены, лимиты rate limits.

**Как работает:** клиент запускает команду из `statusLine.command` и
подаёт ей на stdin JSON с состоянием сессии; скрипт разбирает его (`jq`)
и печатает одну строку — она и отображается. Статус-строка обновляется
при старте/возобновлении сессии, после ответа ассистента, компактизации
и т.п. Скрипт должен быть исполняемым; работает локально, без обращения
к API.

**Входной JSON** (ключи, которые использует скрипт):

| Ключ | Что содержит |
|---|---|
| `model.display_name` | Имя текущей модели |
| `workspace.current_dir` | Рабочая директория |
| `context_window.total_input_tokens` | Отправлено модели (prompt) |
| `context_window.total_output_tokens` | Получено от модели (completion) |
| `context_window.used_percentage` | Заполненность контекстного окна, % (в начале сессии может быть `null`) |
| `rate_limits.five_hour.used_percentage` | Использовано от лимита за 5 часов (есть у Pro/Max после первого ответа) |
| `rate_limits.seven_day.used_percentage` | Использовано от лимита за 7 дней (там же) |

**Что рисует строка** (слева направо):

- **модель** — `display_name`, обрезанный до части после `/`;
- **ветка git** — из `workspace.current_dir` через `git branch
  --show-current` (если не git-репозиторий — `no-git`);
- **заполненность контекста** — процент и цветной прогресс-бар: зелёный
  (<70%), жёлтый (<90%), красный (≥90%);
- **токены** — `↑` отправлено (prompt), `↓` получено (completion), в
  человекочитаемом виде (`1.5M`, `250k`, `900`);
- **rate limits** — использовано от лимитов за 5 часов и 7 дней, если
  данные доступны.

**Требования и поведение:** `bash` + `jq`; если `jq` нет — выводит
диагностику и завершается, не ломая клиент. Если входной JSON не удалось
разобрать (`jq` вернул пусто) — пишет `[statusline: jq parse failed]`.
Скрипт правок не требует: путь к нему задаётся в `statusLine.command`
(раздел 3), он же указывается в `statusLine.padding` — отступ строки от
края (в примере `0`).

---

## 7. Проверка

```bash
# 1. Адаптер слушает и отвечает
curl -s http://127.0.0.1:9999/v1/models | head

# 2. Клиент запускается
claude --version

# 3. В интерфейсе внизу — статус-строка с моделью, веткой и контекстом;
#    ответы на запросы приходят от бэкенда через адаптер
```

---

## 8. Официальная документация

- Настройки: файлы, пути, приоритет слияния —
  <https://code.anthropic.com/docs/en/settings>
- Все ключи `settings.json` — <https://code.anthropic.com/docs/en/settings-reference>
- Примеры файлов с аннотациями — <https://code.anthropic.com/docs/en/settings-example>
- Статус-строка (входной JSON, примеры) — <https://code.anthropic.com/docs/en/statusline>
- JSON-схема для автодополнения — <https://json.schemastore.org/claude-code-settings.json>

Связанные руководства репозитория: установка и конфигурация адаптера —
[`docs/install.md`](install.md); переменные окружения —
[`docs/environment.md`](environment.md); WEBUI и API —
[`docs/webui.md`](webui.md).

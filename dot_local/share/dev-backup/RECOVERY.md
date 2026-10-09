# Бекапы dev: восстановление рабочих агентов через herdr-ops

Сценарий: dev потерян; на Mac запускается пустой Herdr с новым ops. Пользователь
передаёт ему эту заметку и разблокированный rbw. Ops самостоятельно извлекает данные,
восстанавливает рабочие деревья и продолжает **те же разговоры Codex**.
Это аварийный перенос активных агентов, а не обычный Restore архивированной задачи
из скилла ops: здесь используются состояние на момент бекапа и сохранённый HEAD,
а не текущий trunk. Архивированные агенты остаются архивированными.

## Что сохраняется

- Вся авторитетная `~/.config/herdr-history`: активные и архивные YAML-записи.
- Вся `~/.local/share/herdr-transcripts`: HTML разговоров и story `index.html`;
  контрольные суммы из history проверяются при каждом сборе.
- Живые **управляемые Codex** из активных history-записей: точные имена/UUID,
  разговоры JSONL, их нативные дочерние разговоры и отфильтрованное состояние
  SQLite (threads, история turns/items, goals и очередь). UUID сверяется с Herdr.
- Фактические branch/HEAD и cwd каждого рабочего агента, модель и reasoning effort,
  роли, ограничения и состояние parked. Несколько менеджеров могут делить main mount.
- По возможности: staged/unstaged binary patches, итоговые dirty/untracked файлы,
  удаления и локальные `.herdr` checkpoints. Ошибка этого слоя даёт предупреждение;
  потеря обязательной истории, разговора или HEAD даёт только `partial` снимок.
- Неуправляемые chezmoi либо локально изменённые пользовательские systemd-юниты
  и небольшие домашние скрипты из их команд: `recovery/manual-units.json`.
  На Mac это справочный материал для переноса нужной ручной настройки, не автозапуск.

Arc хранит коммиты на сервере: достаточно сохранённого полного hash.
Object store/refs, физические checkout, сборочные кэши **не копируются**.
Скиллы восстанавливаются из `~/arcadia/junk/dubiner/ai-global/install.sh`, обычные
юниты/установка — через chezmoi. OpenCode/watchdog, ops и произвольные табы главного
workspace не возобновляются. Для архивированных агентов сохранены HTML и history,
а не обещание восстановить их нативную Codex-сессию.

## Где данные и ключ

Зашифрованный restic repository: `~/Backups/dev/repository`.
Ключ в rbw: **`dev-backup-restic`**, UUID `8ccb43f0-6c8c-470b-8d08-182c1ea90c8e`.
Локальный файл для таймера: `~/.config/dev-backup/password`, права 600.
Ключ не хранится на dev, в git или в этой заметке. Не заменять его новым паролем.

```sh
# Только если локальный файл ключа утрачен; rbw уже разблокирован.
umask 077
mkdir -p ~/.config/dev-backup
rbw get dev-backup-restic > ~/.config/dev-backup/password.recovered && \
  mv ~/.config/dev-backup/password.recovered ~/.config/dev-backup/password
chmod 600 ~/.config/dev-backup/password
```

## Инструкция новому ops

Пользовательский стартовый промпт:

> $herdr-ops Ты herdr-ops. Прочитай заметку «Бекапы dev и восстановление» в
> Obsidian work/97. ya. rbw разблокирован. Восстанови на этой машине рабочие
> Codex-агенты из последнего полного бекапа dev по аварийному сценарию заметки.
> Сохрани их разговоры, роли, ограничения и parked-состояния; архивные агенты
> и мои произвольные ops-табы не запускай. Новые задачи этим не разрешаются.

До восстановления нужны корпоративная сеть/VPN, работающий Arc на Mac,
macOS-версии Herdr/Codex/ya и доступ к текущей авторизации Codex. Если сеть/Arc
не работают, извлечь локальные данные, записать блокер и сохранить прогресс;
не запускать агента в случайной папке и не менять существующие mount.
При отсутствии скиллов: из **текущего доверенного** main checkout выполнить
`bash ~/arcadia/junk/dubiner/ai-global/install.sh --agent codex`.
Не устанавливать Linux бинарники и не импортировать Linux Codex config/hooks.

1. Проверить `dev-backup status` и `dev-backup snapshots`; выбрать последний снимок
   с **обоими** тегами `complete,managed-codex`. Старые широкие снимки `complete`
   без `managed-codex` относятся к первой схеме и не подходят этому автоматическому
   сценарию. Предыдущие снимки не зависят от доступности dev.
2. Извлечь в новую папку:

   ```sh
   dev-backup restore --target ~/dev-recovery
   # Если скрипт отсутствует: brew install restic, затем
   restic -r ~/Backups/dev/repository -p ~/.config/dev-backup/password \
     restore latest --host dev --tag complete,managed-codex \
     --target ~/dev-recovery --verify
   ```

3. Прочитать `recovery/manifest.json`, `restore-plan.json`, `history.json` и
   предупреждения. `complete` подтверждает обязательные данные; `diff_complete`
   проверяется отдельно для каждого Arc root. Исходный snapshot не менять.
4. Подготовить переносимые данные и план:

   ```sh
   python3 ~/dev-recovery/recovery/prepare-recovery.py ~/dev-recovery \
     --target ~/dev-recovery-prepared
   ```

   Все пути можно явно задать через `--arc-root`, `--worktrees-root`,
   `--codex-home`. По умолчанию: `~/arcadia-dev-recovery`,
   `~/arcadia-wt-dev-recovery`, `~/.codex-dev-recovery` — **новые** места.
   Скрипт проверяет SHA256, переносит SQLite `rollout_path`/cwd и YAML-пути,
   сохраняет JSONL без переписывания, подключает нативный конфиг/авторизацию мака
   ссылками на текущий `~/.codex`, выдаёт `ops-plan.json` с готовыми `resume_argv`
   и `resume_shell`. Существующие настройки, history и панели он не меняет.
5. Создать `~/dev-recovery-prepared/restore-progress.json`; обновлять после каждого
   успешно созданного ресурса. При повторном запуске читать этот файл и существующий
   `ops-plan.json`; не повторять подготовку поверх уже созданных папок и не создавать
   второй workspace/таб/агент для той же пары story/task.
6. По `ops-plan.json` создать **новые** Arc mount/worktree, по одному на каждый
   уникальный `source_root`. Узнать текущую команду у `ya tool arc-wt --help` / Arc;
   не делать reset/checkout в существующем дереве. Для задачи восстановить записанную
   ветку на **`arc.hash`**, для общего main mount менеджеров — отдельное дерево на
   общем сохранённом hash. Не заменять его trunk. Если имя ветки занято на другой
   ревизии, создать отдельную recovery-ветку от сохранённого hash и записать исходное
   имя в `recovery.source_branch`; чужую ветку не двигать. Проверить `arc info --json`
   и записать фактическое имя/HEAD. Если hash недоступен — сохранить план и остановить
   только затронутый агент, не подменять ревизию молча.
7. В новых task-деревьях выполнить обычные `gitarc` и проектный setup по скиллу ops:
   AISuite для direct/grut/umr, `pnpm i` для adv/frontend. Для менеджеров не менять
   разрешение на код: они остаются analysis-only. Скиллы брать из junk.
8. Если `diff_complete: true`, восстановить dirty-файлы в соответствующее новое
   дерево после проверки HEAD:

   ```sh
   python3 ~/dev-recovery/recovery/restore-worktree.py \
     ~/dev-recovery/recovery/arc/<arc_export> <target_root>
   python3 ~/dev-recovery/recovery/restore-worktree.py \
     ~/dev-recovery/recovery/arc/<arc_export> <target_root> --apply
   ```

   При incomplete diff продолжать от сохранённого HEAD, сообщив агенту, что последние
   незакоммиченные действия могли не сохраниться. Не применять неполный экспорт.
9. Перенести `dev-recovery-prepared/codex-home` в **новый** `install_codex_home` из
   плана. Скопировать подготовленные `home/.config/herdr-history` и
   `home/.local/share/herdr-transcripts` в соответствующие места HOME.
   Обрабатывать совпадения поштучно: одинаковые файлы оставить, несовпадающие
   сохранить отдельно и разрешить конфликт до регистрации затронутого агента.
   Не заменять активный `~/.codex` или Herdr `session.json`.
   Linux ручные юниты автоматически не включать; перечень и исходники уже сохранены.
   Перед запуском агентов проверить их чтение нативным Codex без inference:

   ```sh
   python3 ~/dev-recovery/recovery/verify-sessions.py \
     --codex-home ~/.codex-dev-recovery --plan ~/dev-recovery-prepared/ops-plan.json
   ```

   При других путях взять `install_codex_home` из плана. Проверяются UUID,
   перенесённый cwd и чтение последнего turn; чужие сессии/процессы не используются.
10. Валидировать импортированную history:
    `python3 ~/.agents/skills/herdr-ops/scripts/validate_history.py`.
    На этом Mac PyYAML установлен в отдельное окружение проверки:

    ```sh
    export PATH="$HOME/.local/share/dev-backup/validation-venv/bin:$PATH"
    python3 ~/.agents/skills/herdr-ops/scripts/validate_history.py
    ```

    Если окружение утрачено, создать его `python3 -m venv
    ~/.local/share/dev-backup/validation-venv` и установить PyYAML его pip;
    глобальный Python менять не требуется.
    Старые Herdr IDs — только provenance, не адреса новых панелей.
    При создании каждого нового ресурса атомарно обновлять его запись и снова
    валидировать **всю** history до следующей lifecycle-операции. Сохранить прежние
    IDs/cwd/native session в `herdr.previous_runs`, новые ID получать из JSON CLI.
11. Создать workspace по story и табы с записанными labels/layout: Codex сверху,
    shell снизу, оба в `target_cwd`. Первым восстановить workers, затем managers.
    Для каждого агента выполнить его `resume_shell` через
    `herdr pane run <NEW-agent-pane-id> <resume_shell>`; дождаться определения `codex`,
    затем `herdr agent rename <NEW-agent-pane-id> <agent_name>`.
    Отдельный `CODEX_HOME` уже задан в команде. Не менять UUID, модель, sandbox/approval
    ради удобства. Если модель недоступна, сохранить запись и сообщить блокер;
    новую беседу вместо старой не начинать.
12. Проверить через `herdr agent list`, что каждый новый агент имеет **ровно**
    `session_id` из плана и новый cwd. Только после этого пометить `recovery.state`
    как `restored`, обновить координаты, session.path, branch/HEAD и timestamps;
    снова валидировать history. Ошибка resume — не успех восстановления.
13. Направить каждому короткое служебное сообщение с требуемым ops-префиксом,
    `$herdr-worker` или `$herdr-story-manager`, story/task, путём к history/checkpoint,
    новым cwd, исходными разрешениями и состоянием диффа. Старые абсолютные Linux
    пути в разговоре устарели. Менеджерам повторить analysis-only/no-code.
    **Parked остаётся parked:** сообщение требует только перечитать контекст;
    не возобновлять задачи до записанного условия. Не отдавать новых продуктовых
    поручений от имени пользователя и не править Tracker/PR при восстановлении.
14. Сверить план и живой Herdr: имена, UUID, число рабочих агентов, cwd, истории,
    checksum архивных HTML. Отчитаться о восстановленных и заблокированных агентах,
    отдельно — о неполных dirty diffs. Успех отмечать только по проверенным UUID.

## Расписание и проверка

`local.dev-backup`: каждые 30 минут, при входе macOS, пропущенный календарный запуск
после пробуждения. Пока ноутбук выключен/спит или dev/SSH/VPN недоступны, свежей
копии нет; предыдущие сохранены. Сбор не останавливает агентов и не меняет ветки.
Лог: `~/Library/Logs/dev-backup.log`; состояние: `dev-backup status`.

Сохраняются 48 часовых, 14 дневных, 8 недельных и 6 месячных точек.
Таймер большого Arc-архива удалён. Ошибка подключения не создаёт новую «частичную»
копию из старого staging и не обновляет `last_success`. Недоступность диффа видна
в warnings; неисправность обязательных данных — `partial`, с сохранением прежнего
полного снимка. Раз в неделю читаются все блоки restic и выполняется prune.
Ручная проверка: `dev-backup check`.

SQLite согласованы каждая отдельно; JSONL зафиксированы по начальной длине,
недописанный последний record отрезается. Старые повреждённые внутренние строки
сохраняются без исправления, отражаются в warnings; проверять чтение нативным Codex.
Это бекап работающих агентов, не общая транзакционная точка для всей машины.
Рабочие plaintext-копии защищены правами 700; repository дополнительно зашифрован.

Скрипты/таймер управляются chezmoi (`~/.local/share/chezmoi`):
`~/.local/bin/dev-backup`, `~/.local/libexec/dev-backup/`,
`~/Library/LaunchAgents/local.dev-backup.plist`. Ключ и repository в git не попадают.
Нативное состояние Codex и способ resume проверяются по установленной версии;
[официальная документация Codex](https://learn.chatgpt.com/docs/codex/cli) и
[CODEX_HOME](https://learn.chatgpt.com/docs/config-file/config-advanced) поясняют
хранение локального состояния и возобновление беседы; перенос проверяется отдельно.

## Проверка новой схемы 9 октября 2026

Снимок `771ce420d568af783fbc03ca8edba775c65b7dc5c3fd59b202746e4f79fc4664`
извлечён с restic `--verify`: 267 файлов, 1,10 ГиБ исходных данных.
Сохранены 37 history-записей, архивные HTML, 12 управляемых рабочих агентов,
68 разговоров с нативными дочерними thread и 9 уникальных Arc root.
Проверены четыре SQLite-базы (`quick_check=ok`), подготовлены новые пути и
точные resume-команды. Нативный Codex app-server прочитал все 12 основных UUID
и последние turns с новыми cwd **без запуска inference**.
В одной старой сессии есть четыре ранее повреждённые JSONL-строки: сохранены
без исправления, чтение этого разговора нативным Codex также прошло.

Это проверка извлечения, переноса состояния и чтения разговоров. Новые Arc
mount и рабочие агенты в действующем Herdr в рамках проверки не запускались;
их создание и проверка реально возобновлённых UUID выполняются ops при переносе.
Актуальный возраст бекапа всегда смотреть через `dev-backup status`.
Подробный результат проверки: `~/Backups/dev/restore-proof-schema2.json`.

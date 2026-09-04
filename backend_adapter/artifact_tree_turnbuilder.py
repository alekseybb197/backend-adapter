"""artifact_tree_turnbuilder — связывание openai_body и fetch_raw в ходы.

=== Инкрементальная сборка ===
build_turns() умеет ПРОДОЛЖАТЬ с места последнего обработанного part-файла
вместо того, чтобы каждый раз заново парсить и обрабатывать ВСЕ
openai_body/fetch_raw в сессии — при долгой работе (сотни-тысячи part-
файлов) именно повторный парсинг уже виденных файлов был основным
источником замедления генерации дерева.

Состояние, которое нужно для продолжения (registry, pending-очередь
непогашенных openai_body, накопленные turns/orphans/resolution_edges,
last_processed_part_id) сериализуется вызывающим (см.
artifact_tree.generate()) между запусками. Здесь — только сама логика
"обработать НОВЫЙ срез part-файлов и доклеить к уже накопленному состоянию",
устроенная так, чтобы результат совпадал с холодной пересборкой с нуля
(единственная разница — сколько файлов пришлось перечитать)."""

import os

from .artifact_tree_parse import (
    classify_kind,
    compute_inline_labels,
    determine_fetch_raw_kind,
    discover_parts,
    fetch_raw_has_tool_calls,
    load_json,
    process_fetch_raw,
    process_openai_body,
)
from .artifact_tree_registry import ArtifactRegistry

# ==================== СВЯЗЫВАНИЕ openai_body <-> fetch_raw В ХОДЫ ====================


def _process_parts(
    parts_dir: str, registry: ArtifactRegistry, merged: list, resolution_edges: list
):
    """Парсит и обрабатывает СРЕЗ part-файлов (``merged`` — уже
    отфильтрованный и отсортированный список (etype, part_id, path)) в
    ob_records/fr_records. Мутирует registry и resolution_edges (списком
    добавляет новые связи toolcall->tool_result) — то же самое, что раньше
    делал начальный цикл build_turns(), просто вынесено отдельно, чтобы
    вызываться и на полном списке (холодный старт), и на срезе новых
    файлов (продолжение)."""
    ob_records = []  # {part_id, kind, input_names, raw_file}
    fr_records = []  # {part_id, kind, reasoning_name, decision_names, raw_file}
    for etype, part_id, path in merged:
        data = load_json(path)
        # Имя ИСХОДНОГО part-файла (в .yaml, а не в .json — так же, как
        # адаптер и так пишет оба варианта рядом) — для гиперссылок из
        # панели хода на "сырой" дамп в папке сессии, а не только на уже
        # извлечённые артефакты в artefacts/. Берём basename не самого
        # path (может быть .json), а его yaml-соседа по имени.
        raw_file = os.path.splitext(os.path.basename(path))[0] + ".yaml"
        if etype == "ob":
            kind = classify_kind(data)
            names = process_openai_body(data, str(part_id), registry, resolution_edges)
            ob_records.append(
                {"part_id": part_id, "kind": kind, "input_names": names, "raw_file": raw_file}
            )
        else:
            kind = determine_fetch_raw_kind(data)
            # tool_calls в ответе — ТОЧНЫЙ, а не эвристический признак: раз
            # они есть, это определённо agent_turn (structured_output-
            # запрос отправляется вообще без tools[], бэкенду физически
            # неоткуда взять tool_call на него). В отличие от классификации
            # по форме текста (looks_like_structured_output — тоже эвристика,
            # уже дважды чинили) здесь fallback на другой kind в матчинге
            # ниже не должен разрешаться вообще — иначе есть шанс отдать
            # реальный tool_call чужому 0-tools запросу (ровно так один раз
            # и произошло на реальных данных).
            definite = fetch_raw_has_tool_calls(data)
            fr_out = process_fetch_raw(data, str(part_id), registry)
            fr_records.append(
                {
                    "part_id": part_id,
                    "kind": kind,
                    "definite": definite,
                    "raw_file": raw_file,
                    **fr_out,
                }
            )
    return ob_records, fr_records


def _match_records(ob_records: list, fr_records: list, pending: dict, turns: list, orphans: list):
    """Жадное сопоставление openai_body<->fetch_raw. ``pending`` — очередь
    непогашенных openai_body НА ВХОДЕ (может быть непустой при продолжении
    — например, ход из прошлого запуска, чей fetch_raw ещё не пришёл);
    мутируется и остаётся актуальной на выходе (то, что всё ещё не
    погашено — сохраняется в чекпойнт для следующего продолжения). ``turns``
    и ``orphans`` — тоже мутируются добавлением новых записей поверх уже
    накопленных."""
    all_events = sorted(
        [("ob", r["part_id"], r) for r in ob_records]
        + [("fr", r["part_id"], r) for r in fr_records],
        key=lambda t: t[1],
    )

    for etype, part_id, rec in all_events:
        if etype == "ob":
            pending[rec["kind"]].append(rec)
        else:
            # Определённый по СОДЕРЖИМОМУ kind этого fetch_raw — сначала
            # ищем непогашенный openai_body ТОГО ЖЕ kind (наиболее свежий,
            # LIFO). Если такого нет (нить началась до окна дампов, либо
            # kind определён неверно из-за нетипичного ответа) — как
            # запасной вариант пробуем другой kind, ТОЛЬКО если исходная
            # классификация была эвристической (по форме текста), а не
            # точной (по наличию tool_calls) — см. комментарий выше.
            kind = rec["kind"]
            if rec["definite"]:
                search_order = [kind]
            else:
                search_order = [kind] + [
                    k for k in ("agent_turn", "structured_output") if k != kind
                ]
            match_kind = next((k for k in search_order if pending[k]), None)
            if match_kind is None:
                orphans.append(rec)
                continue
            ob_rec = pending[match_kind].pop()  # LIFO — самый свежий
            turns.append(
                {
                    "ob_part_id": ob_rec["part_id"],
                    "fr_part_id": rec["part_id"],
                    "kind": match_kind,
                    "input_names": ob_rec["input_names"],
                    "reasoning_name": rec["reasoning_name"],
                    "decision_names": rec["decision_names"],
                    "ob_raw_file": ob_rec["raw_file"],
                    "fr_raw_file": rec["raw_file"],
                }
            )


def _finalize(turns: list, registry: ArtifactRegistry, inline_labels: dict):
    """Финальная агрегация — сегментация по реальным запросам, Finish/
    Superseded/SessionTitle и т.д. Всегда пересчитывается заново над ПОЛНЫМ
    (резюмированным + новым) состоянием — это дешёвая фильтрация уже
    готовых списков, а не повторный парсинг файлов, так что инкрементальность
    выше по стеку сюда не распространяется (и не должна: тут нечего
    экономить)."""
    # === Сегментация по РЕАЛЬНЫМ запросам пользователя ===
    # В сессии может быть НЕСКОЛЬКО настоящих обращений человека (не только
    # самое первое) — каждый неинлайненный user-артефакт (т.е. НЕ
    # "<system-reminder>"-инжекция) это отдельный, самостоятельный запрос.
    real_user_entries = sorted(
        (int(e["first_part_id"]), e["name"])
        for e in registry.by_hash.values()
        if e["domain"] == "user" and e["name"] not in inline_labels
    )
    start_target = real_user_entries[0][1] if real_user_entries else None

    agent_turns = [t for t in turns if t["kind"] == "agent_turn"]
    final_text_turns = [
        t
        for t in agent_turns
        if any(n.startswith("response-") for n in t["decision_names"])
        and not any(n.startswith("toolcall-") for n in t["decision_names"])
    ]

    request_answers = {}  # user_artifact_name -> response, который на него отвечает
    superseded_targets = []
    boundaries = [pid for pid, _ in real_user_entries] + [float("inf")]
    for i, (pid, uname) in enumerate(real_user_entries):
        next_pid = boundaries[i + 1]
        segment = [t for t in final_text_turns if pid <= t["ob_part_id"] < next_pid]
        if not segment:
            continue  # запрос виден, но финального текстового ответа на него в окне дампов нет
        *earlier, last = segment
        request_answers[uname] = next(
            n for n in last["decision_names"] if n.startswith("response-")
        )
        for t in earlier:
            superseded_targets.append(
                next(n for n in t["decision_names"] if n.startswith("response-"))
            )

    # Finish — ответ на САМЫЙ ПОЗДНИЙ по part_id реальный запрос (последнее
    # слово в сессии). Остальные запросы получают свою собственную стрелку
    # "answers" обратно к своему user-артефакту (см. рендер) — так видно,
    # чем закончился КАЖДЫЙ запрос, а не только сессия целиком.
    finish_source = None
    if real_user_entries:
        last_uname = real_user_entries[-1][1]
        finish_source = request_answers.get(last_uname)

    # "next request" рёбра: ответ на запрос i -> следующий реальный user-
    # артефакт (i+1).
    next_request_edges = []
    for i in range(len(real_user_entries) - 1):
        uname_i = real_user_entries[i][1]
        uname_next = real_user_entries[i + 1][1]
        answer_i = request_answers.get(uname_i)
        if answer_i:
            next_request_edges.append((answer_i, uname_next))

    title_targets = []
    for t in turns:
        if t["kind"] == "structured_output":
            title_targets += [n for n in t["decision_names"] if n.startswith("response-")]

    # Границы страниц (см. п.2/пагинация): страница N покрывает
    # ob_part_id в [pid_N, pid_{N+1}) — те же границы, что и сегментация
    # request_answers выше, просто отданные явно вызывающему.
    page_boundaries = [
        (pid, boundaries[i + 1], uname) for i, (pid, uname) in enumerate(real_user_entries)
    ]

    return (
        start_target,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
        page_boundaries,
    )


def build_turns(parts_dir: str, registry: ArtifactRegistry, resume_state: dict | None = None):
    """Строит (или ПРОДОЛЖАЕТ строить) дерево ходов для ``parts_dir``.

    ``resume_state`` — None для холодной сборки с нуля (как раньше), либо
    чекпойнт вида {"last_processed_part_id": int, "pending": {...},
    "turns": [...], "orphans": [...], "resolution_edges": [[..],..]} —
    тогда обрабатываются ТОЛЬКО part-файлы с part_id БОЛЬШЕ
    last_processed_part_id, а остальное состояние продолжается с того, на
    чём остановились. registry ТОЖЕ должен быть заранее восстановлен
    вызывающим через ArtifactRegistry.from_dict() при resume — эта функция
    сама реестр не восстанавливает, только пополняет.

    Возвращает тот же кортеж, что и раньше, ПЛЮС предпоследним элементом —
    page_boundaries (см. _finalize), и последним — checkpoint: dict для
    сохранения на диск (см. artifact_tree.generate()), готовый скормить
    обратно в следующий вызов как resume_state."""
    found = discover_parts(parts_dir)

    last_pid = (resume_state or {}).get("last_processed_part_id", -1)

    merged = sorted(
        [("ob", pid, path) for pid, path in found["openai_body"] if pid > last_pid]
        + [("fr", pid, path) for pid, path in found["fetch_raw"] if pid > last_pid],
        key=lambda t: t[1],
    )

    resolution_edges: list = [tuple(e) for e in (resume_state or {}).get("resolution_edges", [])]
    pending: dict[str, list] = (resume_state or {}).get(
        "pending", {"agent_turn": [], "structured_output": []}
    )
    turns: list = list((resume_state or {}).get("turns", []))
    orphans: list = list((resume_state or {}).get("orphans", []))

    ob_records, fr_records = _process_parts(parts_dir, registry, merged, resolution_edges)
    _match_records(ob_records, fr_records, pending, turns, orphans)

    turns.sort(key=lambda t: t["ob_part_id"])

    # Дедупликация resolution_edges: тот же tool_result (тот же tool_call_id)
    # ретранслируется в КАЖДОМ последующем openai_body по мере роста истории
    # (тот же эффект, что мы уже разбирали для JSONL-трейса адаптера) — без
    # дедупликации одна и та же связка toolcall->tool_result задваивалась бы
    # на каждый ход, где она просто "проезжает" в контексте.
    resolution_edges = list(dict.fromkeys(resolution_edges))

    # system-артефакты целиком классифицируются в inline_labels (Session/
    # Workflow) и вписываются в блок Ход — отдельного узла Harness для них
    # больше нет. Пересчитывается по ПОЛНОМУ registry каждый раз — дёшево
    # (просто фильтрация уже собранных артефактов, не парсинг файлов).
    inline_labels = compute_inline_labels(registry)

    (
        start_target,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
        page_boundaries,
    ) = _finalize(turns, registry, inline_labels)

    new_last_pid = max([pid for _, pid, _ in merged], default=last_pid)
    checkpoint = {
        "last_processed_part_id": new_last_pid,
        "pending": pending,
        "turns": turns,
        "orphans": orphans,
        "resolution_edges": [list(e) for e in resolution_edges],
    }

    return (
        turns,
        orphans,
        resolution_edges,
        start_target,
        inline_labels,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
        page_boundaries,
        checkpoint,
    )

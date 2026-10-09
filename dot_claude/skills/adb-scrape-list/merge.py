#!/usr/bin/env python3
"""Склейка uiautomator-дампов (из scrape.py) в одну упорядоченную последовательность элементов списка.

Модель экрана: прямые дети контейнера списка по порядку.
  - TextView с текстом              -> заголовок-разделитель (например, дата);
  - любой другой ребёнок (обёртка)  -> элемент; поля = {resource-id: text} всех потомков,
                                       '_desc' = content-desc'и потомков через ' | '.
Элемент без ключа (частично видимый у края экрана) пропускается — он целиком есть на соседнем экране.

Склейка: экраны идут в порядке съёмки; каждый следующий сдвинут в направлении скролла. Ключи
заголовков и элементов уникальны, поэтому экраны сшиваются по последнему общему ключу. Нет общего
ключа между соседними экранами -> AssertionError (значит, свайп проскочил элементы).

Как библиотека:
    from merge import load_screens, merge
    rows = merge(load_screens('dumps', 'rvDetalization', key_fn), direction='up')
    # rows: [(header, fields), ...] сверху вниз по списку

CLI (JSON Lines сверху вниз по списку):
    python3 merge.py dumps rvDetalization --key-field tv_additional_info \\
        --key-regex 'ID: (\\d+)' --key-extra tv_value > items.jsonl
"""
import argparse, glob, json, os, re, sys, xml.etree.ElementTree as ET


def _rid(n):
    return n.get('resource-id', '').split('/')[-1]


def parse_screen(xml, container, key_fn):
    """-> [('H', header_text, None) | ('I', key, fields)] или None, если контейнера нет в дампе."""
    root = ET.fromstring(xml)
    lst = next((n for n in root.iter('node') if _rid(n) == container), None)
    if lst is None:
        return None
    seq = []
    for ch in lst:
        if ch.get('class') == 'android.widget.TextView' and ch.get('text'):
            seq.append(('H', ch.get('text'), None))
            continue
        fields, descs = {}, []
        for n in ch.iter('node'):
            k = _rid(n)
            if n.get('text') and k:
                fields.setdefault(k, n.get('text'))
            if n.get('content-desc'):
                descs.append(n.get('content-desc'))
        fields['_desc'] = ' | '.join(descs)
        key = key_fn(fields)
        if key is not None:
            seq.append(('I', key, fields))
    return seq


def load_screens(dump_dir, container, key_fn):
    screens = []
    for p in sorted(glob.glob(os.path.join(dump_dir, '*.xml'))):
        with open(p) as f:
            s = parse_screen(f.read(), container, key_fn)
        if s:
            screens.append(s)
    return screens


def merge(screens, direction='up'):
    """direction — куда листали при съёмке. Возвращает [(header, fields)] сверху вниз по списку."""
    if direction == 'down':  # приводим к «каждый следующий экран выше предыдущего»
        screens = screens[::-1]
    best = {}  # одна и та же запись на разных экранах: берём версию с наибольшим числом полей
    for s in screens:
        for t, k, f in s:
            if t == 'I' and (k not in best or len(f) > len(best[k])):
                best[k] = f
    kinds = {k: t for s in screens for t, k, _ in s}

    order = [k for _, k, _ in screens[-1]]  # самый верхний экран
    seen = set(order)
    for n, s in enumerate(reversed(screens[:-1])):
        keys = [k for _, k, _ in s]
        idx = max((i for i, k in enumerate(keys) if k in seen), default=None)
        assert idx is not None, f'нет перекрытия между соседними экранами (экран {len(screens) - 2 - n}) — пропуск'
        for k in keys[idx + 1:]:
            if k not in seen:
                order.append(k)
                seen.add(k)

    rows, header = [], None
    for k in order:
        if kinds[k] == 'H':
            header = k
        else:
            rows.append((header, best[k]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dump_dir')
    ap.add_argument('container', help='resource-id контейнера списка без пакета')
    ap.add_argument('--direction', choices=['up', 'down'], default='up')
    ap.add_argument('--key-field', required=True, help='resource-id поля с ключом элемента')
    ap.add_argument('--key-regex', default=None, help='регэксп с группой, вырезающей ключ из поля')
    ap.add_argument('--key-extra', nargs='*', default=[],
                    help='поля, добавляемые к ключу (если ID не уникален, напр. возврат с тем же ID)')
    args = ap.parse_args()

    def key_fn(f):
        v = f.get(args.key_field)
        if v is None:
            return None
        if args.key_regex:
            m = re.search(args.key_regex, v)
            if not m:
                return None
            v = m.group(1)
        return '|'.join([v] + [f.get(e, '') for e in args.key_extra])

    rows = merge(load_screens(args.dump_dir, args.container, key_fn), args.direction)
    for header, fields in rows:
        print(json.dumps({'header': header, **fields}, ensure_ascii=False))
    print(f'{len(rows)} элементов', file=sys.stderr)


if __name__ == '__main__':
    main()

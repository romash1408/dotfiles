#!/usr/bin/env python3
"""Дампы выписки О!Банка (scrape.py) -> операции в JSON, сверху вниз (от новых к старым).

    python3 obank.py dumps > bank.json
    python3 obank.py dumps --today 2026-10-09 > bank.json

Поля операции: date (ISO), time, type, cp (контрагент), amount (float, со знаком),
raw_amount, status («В обработке»), refund (пометка «Возврат»), struck (зачёркнутая
сумма — в дампе она без знака: «8 252,83 c»), id (ID банка).

Год в заголовках дат («26 августа») не пишется: восстанавливается от сегодняшнего
дня вниз по списку — дата не может быть позже предыдущей, иначе это прошлый год.
"""
import argparse, collections, datetime, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from merge import load_screens, merge  # noqa: E402

CONTAINER = 'rvDetalization'
MONTHS = {m: i + 1 for i, m in enumerate(
    'января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split())}
KNOWN = {'tv_title', 'tv_value', 'tv_subtitle', 'tv_status', 'tv_additional_info', 'tv_time', '_desc'}


def key_fn(f):
    m = re.search(r'ID: (\d+)', f.get('tv_additional_info', ''))
    # возврат приходит с тем же ID, что и списание, поэтому к ключу добавляется знак суммы
    return m.group(1) + f.get('tv_value', '')[:1] if m else None


def header_date(h, prev):
    """prev — дата предыдущего (более нового) заголовка."""
    h = h.strip()
    if h == 'Сегодня':
        return prev
    if h == 'Вчера':
        return prev - datetime.timedelta(days=1)
    parts = h.split()
    day, month = int(parts[0]), MONTHS[parts[1].lower()]
    if len(parts) > 2 and parts[2].isdigit():
        return datetime.date(int(parts[2]), month, day)
    d = datetime.date(prev.year, month, day)
    return d if d <= prev else datetime.date(prev.year - 1, month, day)


def parse_amount(v):
    s = v.replace('c', '').replace('с', '').replace(' ', '').replace('\xa0', '').replace(',', '.')
    return float(s)


def load(dump_dir, today):
    rows = merge(load_screens(dump_dir, CONTAINER, key_fn), direction='down')
    unknown = collections.Counter()
    out, cur, prev_header = [], today, None
    for header, f in rows:
        if header != prev_header:
            cur = header_date(header, today if header in ('Сегодня', 'Вчера') else cur)
            prev_header = header
        for k in f:
            if k not in KNOWN:
                unknown[k] += 1
        v = f.get('tv_value', '').strip()
        out.append({
            'date': cur.isoformat(),
            'time': f.get('tv_time', ''),
            'type': f.get('tv_title', ''),
            'cp': f.get('tv_subtitle', ''),
            'amount': parse_amount(v),
            'raw_amount': v,
            'status': f.get('tv_status', ''),
            'refund': 'Возврат' in f.get('_desc', ''),
            'struck': v[:1] not in '-+',
            'id': key_fn(f)[:-1],
        })
    if unknown:
        print(f'незнакомые поля: {dict(unknown)}', file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dump_dir')
    ap.add_argument('--today', default=datetime.date.today().isoformat())
    args = ap.parse_args()
    ops = load(args.dump_dir, datetime.date.fromisoformat(args.today))
    json.dump(ops, sys.stdout, ensure_ascii=False, indent=1)
    print(f'{len(ops)} операций: {ops[0]["date"]} .. {ops[-1]["date"]}' if ops else '0 операций',
          file=sys.stderr)


if __name__ == '__main__':
    main()

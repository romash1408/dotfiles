#!/usr/bin/env python3
"""Сверка выписки (bank.json из obank.py) с листом «Операции» таблицы money-sync.

Команды (по умолчанию всё, что пишет в таблицу, — сухой прогон; запись только с --go):
    money.py sync                         POST /sync и проверка статуса в D1; код 1 при ошибке
    money.py reconcile bank.json          отчёт по дням + reconcile.json (решение «стоп/дальше»)
    money.py plan bank.json > plan.json   черновик новых строк для непокрытых операций
    money.py insert plan.json [--go]      вставить строки плана в «Операции»
    money.py fix-amount ID AMOUNT [--account-sum] [--go]
                                          поменять «Сумму» (или «Сумму в валюте счёта») строки + метка

Настройки (секреты лежат рядом со скриптом, в chezmoi — зашифрованными):
    credentials.json   ключ сервисного аккаунта Google (или GOOGLE_CREDENTIALS_FILE)
    .env               SPREADSHEET_ID и SYNC_SECRET (переменные окружения важнее)
    SYNC_URL           адрес синка (по умолчанию http://hp.wg:8090/sync)
    MONEY_DIR          запасной источник credentials.json/.env — репозиторий money
    MONEY_ACCOUNT      счёт в таблице (по умолчанию «O!Bank R *3720»)
    BANK_TZ_HOURS      смещение времени выписки от UTC (по умолчанию 6, Бишкек)
"""
import argparse, collections, datetime, itertools, json, os, sys, urllib.request

ACCOUNT = os.environ.get('MONEY_ACCOUNT', 'O!Bank R *3720')
TZ = datetime.timezone(datetime.timedelta(hours=float(os.environ.get('BANK_TZ_HOURS', '6'))))
SHEET = 'Операции'
MARK_COL = 17          # колонка R — личная пометка пользователя, синк её не читает
MARK = 'suggested'
EPOCH = datetime.date(1899, 12, 30)
DATE_WINDOW = 2        # операции «плывут» по дате: ищем пару в ±2 днях
GROUP_MAX = 5          # максимум частей у разбитой/склеенной операции


# ---------- окружение ----------

HERE = os.path.dirname(os.path.abspath(__file__))


def config_dir():
    """Каталог с credentials.json и .env: сам скилл, иначе MONEY_DIR или репозиторий money."""
    cands = [HERE, os.environ.get('MONEY_DIR'), os.path.expanduser('~/projects/money')]
    for d in filter(None, cands):
        if os.path.exists(os.path.join(d, 'credentials.json')):
            return d
    return None


def dotenv():
    d = config_dir()
    env = {}
    if d and os.path.exists(os.path.join(d, '.env')):
        for line in open(os.path.join(d, '.env'), encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"\'')
    return env


def setting(name, default=None):
    return os.environ.get(name) or dotenv().get(name) or default


_svc = None


def svc():
    global _svc
    if _svc is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        cred = os.environ.get('GOOGLE_CREDENTIALS_FILE') or os.path.join(config_dir() or HERE, 'credentials.json')
        if not os.path.exists(cred):
            sys.exit(f'нет ключа {cred}: положите credentials.json рядом со скиллом')
        creds = service_account.Credentials.from_service_account_file(
            cred, scopes=['https://www.googleapis.com/auth/spreadsheets'])
        _svc = build('sheets', 'v4', credentials=creds).spreadsheets()
    return _svc


def sid():
    s = setting('SPREADSHEET_ID')
    if not s:
        sys.exit('не найден SPREADSHEET_ID: положите .env рядом со скиллом или задайте SPREADSHEET_ID')
    return s


def get(rng, render='UNFORMATTED_VALUE'):
    return svc().values().get(spreadsheetId=sid(), range=rng,
                              valueRenderOption=render).execute().get('values', [])


def sheet_id(title):
    meta = svc().get(spreadsheetId=sid(), fields='sheets.properties').execute()
    return next(s['properties']['sheetId'] for s in meta['sheets'] if s['properties']['title'] == title)


# ---------- лист «Операции» ----------

class Ops:
    """Снимок листа: строка 1 служебная (D1 — статус синка), 2 — заголовок, данные с 3-й."""

    def __init__(self):
        v = get(f'{SHEET}!A1:{chr(ord("A") + MARK_COL)}')
        self.status = v[0][3] if len(v[0]) > 3 else ''
        self.header = v[1]
        self.col = {name: i for i, name in enumerate(self.header)}
        self.data = [r + [''] * (MARK_COL + 1 - len(r)) for r in v[2:]]

    def c(self, r, name):
        return r[self.col[name]]

    def row_date(self, r):
        d = self.c(r, 'Дата')
        return EPOCH + datetime.timedelta(days=int(d)) if isinstance(d, (int, float)) else None

    def raw(self, r):
        try:
            return json.loads(self.c(r, 'raw'))
        except Exception:
            return {}

    def created(self, r):
        c = self.raw(r).get('created')
        if not c:
            return None
        return c / 1000 if c > 1e11 else c  # изредка created в миллисекундах

    def deleted(self, r):
        return self.c(r, 'Удалена') in (True, 'TRUE')

    def account_rows(self, lo, hi):
        out = []
        for i, r in enumerate(self.data):
            d = self.row_date(r)
            if self.c(r, 'Счёт') != ACCOUNT or self.deleted(r) or d is None or not lo <= d <= hi:
                continue
            m = self.c(r, 'Сумма в валюте счёта')
            amount = m if isinstance(m, (int, float)) else self.c(r, 'Сумма')
            raw = self.raw(r)
            out.append({
                'row': i + 3, 'date': d, 'amount': round(float(amount), 2), 'id': self.c(r, 'id'),
                'category': self.c(r, 'Категория'), 'comment': self.c(r, 'Описание'),
                'payee': self.c(r, 'Получатель'), 'currency': self.c(r, 'Валюта'),
                'op_amount': self.c(r, 'Сумма'), 'orig': raw.get('originalPayee') or raw.get('payee') or '',
                'converted': raw.get('incomeInstrument') != raw.get('outcomeInstrument')
                or raw.get('opIncome') is not None or raw.get('opOutcome') is not None,
            })
        return out

    def payee_map(self, days=365):
        """Сырая строка банка -> «Получатель», если в истории счёта соответствие однозначно."""
        since = datetime.date.today() - datetime.timedelta(days=days)
        cnt = collections.defaultdict(collections.Counter)
        for r in self.data:
            d = self.row_date(r)
            if d is None or d < since or self.deleted(r):
                continue
            orig = self.raw(r).get('originalPayee')
            if orig:
                cnt[norm(orig)][self.c(r, 'Получатель')] += 1
        out = {}
        for orig, c in cnt.items():
            (top, n), total = c.most_common(1)[0], sum(c.values())
            if top and n >= 2 and n / total >= 0.8:
                out[orig] = top
        return out


def norm(s):
    return ' '.join(s.upper().replace('\xa0', ' ').split())


def bank_ts(op):
    h, m = map(int, op['time'].split(':'))
    y, mo, d = map(int, op['date'].split('-'))
    return datetime.datetime(y, mo, d, h, m, tzinfo=TZ).timestamp()


# ---------- сверка ----------

def skipped(bank):
    """Индекс -> причина: возвраты, возвращённые ими списания, зачёркнутые, пары отмены."""
    skip = {}
    for i, b in enumerate(bank):
        if b['struck']:
            skip[i] = 'зачёркнута'
    for i, b in enumerate(bank):
        if b['refund'] and i not in skip:
            skip[i] = 'возврат'
            j = next((j for j, c in enumerate(bank) if j not in skip and c['id'] == b['id']
                      and c['amount'] == -b['amount']), None)
            if j is not None:
                skip[j] = 'возвращённое списание'
    # приход и расход на одну сумму у одного контрагента в один день без пометки — отмена
    for i, b in enumerate(bank):
        if i in skip or b['amount'] <= 0:
            continue
        j = next((j for j, c in enumerate(bank) if j not in skip and j != i and c['date'] == b['date']
                  and c['cp'] == b['cp'] and c['amount'] == -b['amount']), None)
        if j is not None:
            skip[i] = skip[j] = 'пара отмены (приход и расход на одну сумму)'
    return skip


def reconcile(bank, ops):
    dates = [datetime.date.fromisoformat(b['date']) for b in bank]
    lo, hi = min(dates) - datetime.timedelta(DATE_WINDOW), max(dates) + datetime.timedelta(DATE_WINDOW)
    sheet = ops.account_rows(lo, hi)
    skip = skipped(bank)
    bmatch, sused = {}, set()          # индекс банка -> описание сопоставления
    bdate = lambda i: datetime.date.fromisoformat(bank[i]['date'])
    near = lambda i, j, tol: abs((sheet[j]['date'] - bdate(i)).days) <= tol

    # 1. точная сумма: тот же день, потом ±1, ±2
    for tol in range(DATE_WINDOW + 1):
        for i, b in enumerate(bank):
            if i in skip or i in bmatch:
                continue
            for j, s in enumerate(sheet):
                if j not in sused and s['amount'] == round(b['amount'], 2) and abs((s['date'] - bdate(i)).days) == tol:
                    bmatch[i] = {'kind': 'сумма', 'rows': [s['row']], 'shift': (s['date'] - bdate(i)).days}
                    sused.add(j)
                    break

    def free_b():
        return [i for i in range(len(bank)) if i not in skip and i not in bmatch]

    def free_s():
        return [j for j in range(len(sheet)) if j not in sused]

    # 2. разбитые/склеенные: сумма нескольких операций одной стороны = одна операция другой
    for tol in (0, 1):
        for size in range(2, GROUP_MAX + 1):
            # несколько операций банка -> одна строка таблицы (2×17 = 34, пополнения одной строкой)
            for j in free_s():
                cand = [i for i in free_b() if near(i, j, tol)]
                if len(cand) > 22:
                    continue
                for combo in itertools.combinations(cand, size):
                    if round(sum(bank[i]['amount'] for i in combo), 2) == sheet[j]['amount']:
                        for i in combo:
                            bmatch[i] = {'kind': f'склеена с {size - 1} др. в одну строку', 'rows': [sheet[j]['row']]}
                        sused.add(j)
                        break
            # одна операция банка -> несколько строк таблицы (разбивка, кешбэк вычтен строкой)
            for i in free_b():
                cand = [j for j in free_s() if near(i, j, tol)]
                if len(cand) > 22:
                    continue
                for combo in itertools.combinations(cand, size):
                    if round(sum(sheet[j]['amount'] for j in combo), 2) == round(bank[i]['amount'], 2):
                        bmatch[i] = {'kind': f'разбита на {size} строк', 'rows': [sheet[j]['row'] for j in combo]}
                        sused.update(combo)
                        break

    # 3. оплата бонусами через О!: в выписке часть суммы, в таблице полная покупка с той же кассой
    for i in free_b():
        b = bank[i]
        if not b['cp'].startswith('Покупка на кассе'):
            continue
        for j in free_s():
            s = sheet[j]
            if near(i, j, 1) and norm(s['orig']) == norm(b['cp']) and s['amount'] <= b['amount'] < 0:
                bmatch[i] = {'kind': 'оплата бонусами (в таблице полная сумма)', 'rows': [s['row']]}
                sused.add(j)
                break

    # 4. кандидаты на правку суммы: та же сторона, ±1 день, отличие до 5%, строка с конвертацией
    fixes = []
    for i in free_b():
        b = bank[i]
        for j in free_s():
            s = sheet[j]
            if near(i, j, 1) and s['converted'] and s['amount'] * b['amount'] > 0 \
                    and abs(s['amount'] - b['amount']) <= 0.05 * abs(b['amount']):
                fixes.append({'bank': i, 'row': s['row'], 'id': s['id'], 'sheet_amount': s['amount'],
                              'bank_amount': b['amount'], 'currency': s['currency']})
                bmatch[i] = {'kind': 'сумма отличается (конвертация)', 'rows': [s['row']], 'fix': True}
                sused.add(j)
                break

    # статус по дням (сверху вниз — от новых к старым)
    days = []
    for d in sorted({b['date'] for b in bank}, reverse=True):
        idx = [i for i, b in enumerate(bank) if b['date'] == d]
        live = [i for i in idx if i not in skip]
        missing = [i for i in live if i not in bmatch]
        fix = [i for i in live if bmatch.get(i, {}).get('fix')]
        days.append({'date': d, 'ops': len(idx), 'live': len(live), 'missing': missing, 'fix': fix,
                     'covered': bool(live) and not missing and not fix})
    # последний день выгрузки может быть обрезан снизу — решение по нему не принимаем
    complete = [d for d in days[:-1]]
    stop = next((d['date'] for d in complete if d['covered']), None)
    corrections = [s for j, s in enumerate(sheet) if s['category'] == 'Корректировка']
    return {'sheet': sheet, 'skip': skip, 'match': bmatch, 'sused': sorted(sused), 'fixes': fixes,
            'days': days, 'stop': stop, 'corrections': corrections, 'status': ops.status}


def print_report(bank, res):
    sheet, skip, match = res['sheet'], res['skip'], res['match']
    print('статус синка:', res['status'])
    srow = {s['row']: s for s in sheet}
    shown_rows = set()
    for day in res['days']:
        d = day['date']
        flag = 'ВСЁ ЕСТЬ' if day['covered'] else (f'нет {len(day["missing"])}' if day['live'] else 'пусто')
        print(f'## {d}  [{flag}]')
        for i, b in enumerate(bank):
            if b['date'] != d:
                continue
            if i in skip:
                st = f'пропуск: {skip[i]}'
            elif i in match:
                m = match[i]
                st = f'есть ({m["kind"]}; строки {",".join(map(str, m["rows"]))})'
                if m.get('shift'):
                    st += f' сдвиг {m["shift"]:+d} дн.'
            else:
                st = 'НЕТ В ТАБЛИЦЕ'
            print(f'  B {b["time"]} {b["raw_amount"]:>14} {b["cp"][:40]:40} {b["status"]:12} {st}')
        for s in sheet:
            if s['date'].isoformat() == d:
                used = s['row'] in {r for m in match.values() for r in m['rows']}
                shown_rows.add(s['row'])
                print(f'  S {"   " if used else "-- "}стр.{s["row"]:<6} {s["amount"]:>12} {s["currency"]} | '
                      f'{s["category"]} | {str(s["comment"])[:40]} | {s["payee"]}')
    if res['fixes']:
        print('\nкандидаты на правку суммы (конвертация):')
        for f in res['fixes']:
            print(f'  стр.{f["row"]} id {f["id"]}: в таблице {f["sheet_amount"]}, в выписке {f["bank_amount"]}')
    if res['corrections']:
        print('\nкорректировки баланса в диапазоне (после заполнения дыры их, возможно, надо поправить):')
        for s in res['corrections']:
            print(f'  стр.{s["row"]} {s["date"]} {s["amount"]} {s["comment"]}')
    print('\nСТОП: полностью покрытый день', res['stop'] if res['stop'] else '— не найден, листать дальше')


# ---------- план и вставка ----------

def plan(bank, res, ops):
    """Черновик строк для операций новее дня остановки, которых нет в таблице."""
    payees = ops.payee_map()
    stop = res['stop']
    rows = []
    for i, b in enumerate(bank):
        if i in res['skip'] or i in res['match'] or (stop and b['date'] <= stop):
            continue
        payee = payees.get(norm(b['cp']), '')
        notes = []
        if not payee:
            notes.append(f'{b["cp"]} в {b["time"]}')
        if b['cp'].startswith('Покупка на кассе'):
            notes.append('возможно, часть оплачена бонусами')
        if b['status']:
            notes.append(b['status'])
        rows.append({'date': b['date'], 'time': b['time'], 'amount': b['amount'], 'currency': 'KGS',
                     'account_amount': b['amount'], 'category': '', 'payee': payee,
                     'comment': '; '.join(notes), 'bank_id': b['id'], 'bank_cp': b['cp']})
    return rows


def insert(rows, go):
    ops = Ops()
    data = ops.data
    col = ops.col

    def anchor(serial, ts):
        block = [i for i, r in enumerate(data) if ops.c(r, 'Дата') == serial]
        if block:
            for i in range(block[0], block[-1] + 1):
                c = ops.created(data[i])
                if ops.c(data[i], 'Дата') == serial and c is not None and c < ts:
                    return i
            return block[-1] + 1
        for i, r in enumerate(data):
            d = ops.c(r, 'Дата')
            if isinstance(d, (int, float)) and d < serial:
                return i
        return len(data)

    def cell(x):
        if isinstance(x, bool):
            return {'userEnteredValue': {'boolValue': x}}
        if isinstance(x, (int, float)):
            return {'userEnteredValue': {'numberValue': x}}
        return {'userEnteredValue': {'stringValue': x}} if x != '' else {}

    groups = collections.defaultdict(list)
    for r in rows:
        serial = (datetime.date.fromisoformat(r['date']) - EPOCH).days
        ts = bank_ts(r)
        vals = [''] * (MARK_COL + 1)
        for name, v in (('Дата', serial), ('Категория', r['category']), ('Описание', r['comment']),
                        ('Получатель', r['payee']), ('Счёт', ACCOUNT), ('Валюта', r['currency']),
                        ('Сумма', r['amount']), ('Новая', True), ('Удалена', False),
                        ('Сумма в валюте счёта', r['account_amount'])):
            vals[col[name]] = v
        vals[MARK_COL] = MARK
        groups[anchor(serial, ts)].append((serial, ts, r, vals))

    gid = sheet_id(SHEET)
    reqs = []
    for a in sorted(groups, reverse=True):          # снизу вверх: индексы выше не сдвигаются
        g = sorted(groups[a], key=lambda x: (x[0], x[1]), reverse=True)
        row0 = a + 2
        reqs.append({'insertDimension': {'range': {'sheetId': gid, 'dimension': 'ROWS', 'startIndex': row0,
                                                   'endIndex': row0 + len(g)},
                                         'inheritFromBefore': a > 0}})  # под заголовком — от строки ниже
        reqs.append({'updateCells': {'start': {'sheetId': gid, 'rowIndex': row0, 'columnIndex': 0},
                                     'rows': [{'values': [cell(x) for x in v]} for *_, v in g],
                                     'fields': 'userEnteredValue'}})
        nb = lambda i: f'{ops.row_date(data[i])} {ops.c(data[i], "Счёт")} {ops.c(data[i], "Сумма")}' \
            if 0 <= i < len(data) else '—'
        print(f'перед строкой {a + 3} ({nb(a)}), после ({nb(a - 1)}):')
        for _, _, r, _ in g:
            print(f'    {r["date"]} {r["time"]} {r["amount"]:>12} | {r["category"]} | {r["comment"]} | {r["payee"]}')
    print(f'{len(rows)} строк, {len(groups)} мест вставки; статус синка: {ops.status}')
    if go:
        svc().batchUpdate(spreadsheetId=sid(), body={'requests': reqs}).execute()
        check = Ops()
        marked = [r for r in check.data if r[MARK_COL] == MARK and check.c(r, 'id') == '']
        print(f'вставлено; строк с меткой {MARK} и пустым id на листе: {len(marked)}')
    else:
        print('сухой прогон — для записи добавьте --go')


def fix_amount(tid, amount, go, account_sum=False):
    ops = Ops()
    hits = [i for i, r in enumerate(ops.data) if ops.c(r, 'id') == tid and ops.c(r, 'Счёт') == ACCOUNT]
    if len(hits) != 1:
        sys.exit(f'строк счёта {ACCOUNT} с id {tid}: {len(hits)}')
    i = hits[0]
    r = ops.data[i]
    # валюта строки = валюте счёта: правим «Сумму», старое значение остаётся в «Сумме в валюте счёта»;
    # строка в чужой валюте: сумма счёта живёт в «Сумме в валюте счёта» — правим её
    target, keep = ('Сумма в валюте счёта', 'Сумма') if account_sum else ('Сумма', 'Сумма в валюте счёта')
    print(f'стр.{i + 3}: {ops.row_date(r)} {ops.c(r, "Категория")} {target} {ops.c(r, target)} -> {amount} '
          f'(«{keep}» {ops.c(r, keep)} не трогаем)')
    if go:
        letter = lambda c: chr(ord('A') + c)
        svc().values().batchUpdate(spreadsheetId=sid(), body={'valueInputOption': 'RAW', 'data': [
            {'range': f'{SHEET}!{letter(ops.col[target])}{i + 3}', 'values': [[amount]]},
            {'range': f'{SHEET}!{letter(MARK_COL)}{i + 3}', 'values': [[MARK]]}]}).execute()
        print('записано')
    else:
        print('сухой прогон — для записи добавьте --go')


def sync():
    url = setting('SYNC_URL', 'http://hp.wg:8090/sync')
    req = urllib.request.Request(url, method='POST')
    if setting('SYNC_SECRET'):
        req.add_header('X-Sync-Secret', setting('SYNC_SECRET'))
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            print(resp.status, resp.read().decode())
    except Exception as e:
        sys.exit(f'синк {url} не удался: {e}')
    status = Ops().status
    print('статус:', status)
    if not status.startswith('OK'):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('sync')
    p = sub.add_parser('reconcile'); p.add_argument('bank')
    p = sub.add_parser('plan'); p.add_argument('bank')
    p = sub.add_parser('insert'); p.add_argument('plan'); p.add_argument('--go', action='store_true')
    p = sub.add_parser('fix-amount'); p.add_argument('id'); p.add_argument('amount', type=float)
    p.add_argument('--account-sum', action='store_true', help='править «Сумму в валюте счёта» (строка в чужой валюте)')
    p.add_argument('--go', action='store_true')
    a = ap.parse_args()

    if a.cmd == 'sync':
        sync()
    elif a.cmd in ('reconcile', 'plan'):
        bank = json.load(open(a.bank, encoding='utf-8'))
        ops = Ops()
        res = reconcile(bank, ops)
        if a.cmd == 'reconcile':
            print_report(bank, res)
            json.dump({'stop': res['stop'], 'fixes': res['fixes'],
                       'days': [{k: v for k, v in d.items()} for d in res['days']]},
                      open('reconcile.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        else:
            json.dump(plan(bank, res, ops), sys.stdout, ensure_ascii=False, indent=1)
    elif a.cmd == 'insert':
        insert(json.load(open(a.plan, encoding='utf-8')), a.go)
    elif a.cmd == 'fix-amount':
        fix_amount(a.id, a.amount, a.go, a.account_sum)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Листает скроллируемый список в Android-приложении и сохраняет uiautomator-дамп каждого экрана.

Пример:
    python3 scrape.py rvDetalization --dir dumps --direction up

Останавливается, когда два дампа подряд дают одинаковое содержимое списка (достигнут край).
Если контейнер списка не найден (экран погас / открыто другое окно) — ждёт, не свайпая.
Разбор дампов в CSV делается отдельно, под конкретное приложение.
"""
import argparse, os, subprocess, time, xml.etree.ElementTree as ET


def dump():
    for _ in range(5):
        out = subprocess.run("adb exec-out uiautomator dump /dev/tty", shell=True,
                             capture_output=True, text=True).stdout
        i, j = out.find('<?xml'), out.rfind('</hierarchy>')
        if i >= 0 and j > 0:
            return out[i:j + len('</hierarchy>')]
        time.sleep(1)
    raise RuntimeError(out)


def list_signature(xml, container):
    """Тексты и content-desc всех узлов внутри контейнера списка; None, если контейнера нет."""
    root = ET.fromstring(xml)
    lst = next((n for n in root.iter('node')
                if n.get('resource-id', '').split('/')[-1] == container), None)
    if lst is None:
        return None
    return tuple((n.get('text', ''), n.get('content-desc', '')) for n in lst.iter('node'))


def screen_size():
    out = subprocess.run("adb shell wm size", shell=True, capture_output=True, text=True).stdout
    w, h = out.strip().split()[-1].split('x')
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('container', help='resource-id контейнера списка (без пакета), напр. rvDetalization')
    ap.add_argument('--dir', default='dumps', help='куда сохранять дампы')
    ap.add_argument('--direction', choices=['up', 'down'], default='up', help='куда листать список')
    ap.add_argument('--max-screens', type=int, default=2000)
    ap.add_argument('--swipe-ms', type=int, default=1500, help='длительность свайпа; быстрее — риск инерции и пропусков')
    ap.add_argument('--pause', type=float, default=0.8, help='пауза после свайпа, сек')
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    w, h = screen_size()
    x = w // 2
    top, bottom = int(h * 0.31), int(h * 0.76)  # ~45% высоты: заметно меньше видимой части списка
    y1, y2 = (top, bottom) if args.direction == 'up' else (bottom, top)  # палец вниз = скролл вверх

    prev = None
    for i in range(args.max_screens):
        while True:
            xml = dump()
            sig = list_signature(xml, args.container)
            if sig is not None:
                break
            print('список не виден (разблокируйте телефон / вернитесь в приложение), жду...', flush=True)
            time.sleep(3)
        with open(os.path.join(args.dir, f'{i:04d}.xml'), 'w') as f:
            f.write(xml)
        first = next((t or d for t, d in sig if t or d), '')
        print(i, first[:80], flush=True)
        if sig == prev:
            print('край списка достигнут')
            break
        prev = sig
        subprocess.run(f"adb shell input swipe {x} {y1} {x} {y2} {args.swipe_ms}", shell=True)
        time.sleep(args.pause)


if __name__ == '__main__':
    main()

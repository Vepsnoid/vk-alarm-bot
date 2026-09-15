# -*- coding: utf-8 -*-
"""Проверка пагинации wall.get в VKService.get_posts_since_last.

Регрессия: раньше забиралась только одна страница (30 постов) и курсор прыгал
на максимальный id — при всплеске новых постов «середина» терялась навсегда.

Запуск из каталога backend:  python tests/test_wall_pagination.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.vk_service import VKService  # noqa: E402

OWNER = -100001


def _post(pid: int, pinned: bool = False) -> dict:
    item = {
        "id": pid,
        "date": 1700000000 + pid,
        "text": f"post {pid}",
        "likes": {"count": 1},
        "comments": {"count": 2},
        "reposts": {"count": 3},
        "views": {"count": 4},
    }
    if pinned:
        item["is_pinned"] = 1
    return item


def _make_vk(full_order, page_size: int = 100):
    """full_order: посты от новых к старым, как их отдаёт VK."""
    vk = VKService()
    calls = {"n": 0, "offsets": []}

    async def fake_get_wall_posts(owner_id, count=100, offset=0, filter_type="owner"):
        calls["n"] += 1
        calls["offsets"].append(offset)
        return full_order[offset:offset + count]

    vk.get_wall_posts = fake_get_wall_posts
    return vk, calls


def test_burst_larger_than_page():
    """Всплеск > одной страницы: собираются все новые посты, без пропусков."""
    order = [_post(pid) for pid in range(150, 0, -1)]  # ids 150..1
    vk, calls = _make_vk(order)
    posts = asyncio.run(vk.get_posts_since_last(OWNER, last_id=49, max_posts=500))
    ids = sorted(p["id"] for p in posts)
    assert ids == list(range(50, 151)), f"пропуски/лишние: {ids[:3]}..{ids[-3:]}"
    assert calls["n"] == 2, f"пагинация сработала не так: запросов {calls['n']}"


def test_whole_page_old_stops():
    """Страница целиком из уже известных постов — пагинация останавливается."""
    order = [_post(pid) for pid in range(100, 0, -1)]
    vk, calls = _make_vk(order)
    posts = asyncio.run(vk.get_posts_since_last(OWNER, last_id=120, max_posts=500))
    assert posts == [], f"ожидалось 0, получено {len(posts)}"
    assert calls["n"] == 1, f"должен быть 1 запрос, было {calls['n']}"


def test_monotonic_single_page():
    """Обычный случай: одна неполная страница, всё новое собирается."""
    order = [_post(pid) for pid in range(60, 0, -1)]
    vk, calls = _make_vk(order)
    posts = asyncio.run(vk.get_posts_since_last(OWNER, last_id=40, max_posts=500))
    assert sorted(p["id"] for p in posts) == list(range(41, 61))
    assert calls["n"] == 1


def test_pinned_old_skipped():
    """Закреплённый старый пост не попадает в результат."""
    order = [_post(5, pinned=True)] + [_post(pid) for pid in range(30, 0, -1)]
    vk, calls = _make_vk(order)
    posts = asyncio.run(vk.get_posts_since_last(OWNER, last_id=10, max_posts=500))
    ids = sorted(p["id"] for p in posts)
    assert 5 not in ids, "закреплённый старый пост попал в результат"
    assert ids == list(range(11, 31)), ids
    assert calls["n"] == 1


def test_first_run_baseline():
    """Первый запуск (last_id=None) берёт только новейшую страницу, без пагинации."""
    order = [_post(pid) for pid in range(200, 0, -1)]
    vk, calls = _make_vk(order)
    posts = asyncio.run(vk.get_posts_since_last(OWNER, last_id=None, max_posts=30))
    assert len(posts) == 30, f"ожидалось 30, получено {len(posts)}"
    assert max(p["id"] for p in posts) == 200
    assert calls["n"] == 1, "первый запуск не должен пагинировать"

    # Даже при большом лимите первый запуск ограничен небольшой базовой выборкой.
    vk2, calls2 = _make_vk(order)
    posts2 = asyncio.run(vk2.get_posts_since_last(OWNER, last_id=None, max_posts=200))
    assert len(posts2) == 30, f"первый запуск должен ограничиваться 30, получено {len(posts2)}"


def test_burst_over_cap_is_reported():
    """Переполнение лимита max_posts видно в stats (иначе посты теряются молча)."""
    order = [_post(pid) for pid in range(300, 0, -1)]  # ids 300..1, все новее курсора
    vk, calls = _make_vk(order)
    stats = {}
    posts = asyncio.run(vk.get_posts_since_last(OWNER, last_id=0, max_posts=200, stats=stats))
    assert len(posts) == 200, len(posts)
    assert max(p["id"] for p in posts) == 300
    assert stats["fetched"] == 200, stats
    assert stats["skipped"] == 100, stats

    # Без переполнения предупреждать не о чем.
    vk2, _ = _make_vk(order)
    stats2 = {}
    posts2 = asyncio.run(vk2.get_posts_since_last(OWNER, last_id=250, max_posts=200, stats=stats2))
    assert len(posts2) == 50, len(posts2)
    assert stats2["skipped"] == 0, stats2


_TESTS = [
    ("всплеск больше страницы: без пропусков", test_burst_larger_than_page),
    ("страница целиком старая: остановка", test_whole_page_old_stops),
    ("монотонная лента: одна страница", test_monotonic_single_page),
    ("закреплённый старый пост пропускается", test_pinned_old_skipped),
    ("первый запуск: базовая страница", test_first_run_baseline),
    ("переполнение лимита сообщается в stats", test_burst_over_cap_is_reported),
]

if __name__ == "__main__":
    failures = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"OK    {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    total = len(_TESTS)
    print(f"{total - failures}/{total} тестов пройдено")
    sys.exit(1 if failures else 0)

# -*- coding: utf-8 -*-
"""Проверка формулы ER и причин отсева постов в фильтрах.

Запуск из каталога backend:  python tests/test_er_and_filters.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.vk_service import VKService  # noqa: E402


def _er(likes, reposts, comments, views):
    return round((likes + comments * 5 + reposts * 10) / (views + 2000) * 100, 2)


def test_er_formula():
    """ER = (лайки + комментарии*5 + репосты*10) / (просмотры + 2000) * 100"""
    assert VKService.calculate_er(100, 10, 5, 1000) == _er(100, 10, 5, 1000) == 7.5
    # denominator is views + 2000, so zero views is safe
    assert VKService.calculate_er(200, 0, 0, 0) == 10.0
    # reposts weight 10, comments weight 5
    assert VKService.calculate_er(0, 20, 0, 0) == 10.0
    assert VKService.calculate_er(0, 0, 40, 0) == 10.0
    assert VKService.calculate_er(0, 0, 0, 0) == 0.0


def test_filter_reasons():
    """Фильтры возвращают категорию и человеческую причину отсева."""
    ok, category, detail = VKService.check_filters("реклама и спам", [], ["реклама"], 0, 100, 1, 0, 0, 100)
    assert (ok, category) == (False, "minus"), (ok, category)
    assert "реклама" in (detail or ""), detail

    ok, category, detail = VKService.check_filters("просто текст без ключевых слов", ["новости"], [], 0, 100, 1, 0, 0, 100)
    assert (ok, category) == (False, "keywords"), (ok, category)

    ok, category, detail = VKService.check_filters("новости города", ["новости"], [], 0, 1, 100, 10, 5, 1000)
    assert (ok, category) == (False, "er"), (ok, category)
    assert "ER" in (detail or ""), detail

    ok, category, detail = VKService.check_filters("новости города", ["новости"], [], 0, 100, 100, 10, 5, 1000)
    assert (ok, category, detail) == (True, None, None), (ok, category, detail)


def test_ai_skip_marker():
    """Ответ-маркер от ИИ (SKIP) означает «пост не публикуем»."""
    from app.services.ai_service import _is_skip_answer

    assert _is_skip_answer("SKIP") is True
    assert _is_skip_answer(" skip. ") is True
    assert _is_skip_answer("Пропустить") is True
    assert _is_skip_answer("НЕРЕЛЕВАНТНО") is True
    assert _is_skip_answer("не относится") is True
    assert _is_skip_answer("Нет") is True
    assert _is_skip_answer("-") is True
    # обычный переписанный текст пропуском не считается
    assert _is_skip_answer("В Петрозаводске закрыли ФАП, врачи уволены") is False
    assert _is_skip_answer("") is False


_TESTS = [
    ("ER = (l + c*5 + r*10) / (v + 2000) * 100", test_er_formula),
    ("причины отсева: минус-слово / ключевые / ER", test_filter_reasons),
    ("маркер SKIP от ИИ отсеивает пост", test_ai_skip_marker),
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

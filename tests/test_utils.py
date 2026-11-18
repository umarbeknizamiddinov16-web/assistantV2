from app.utils import extract_json, extract_skills, split_message, strip_html


def test_extract_json_with_fence_and_noise():
    raw = 'Вот ответ:\n```json\n{"match_score": 75, "missing_skills": ["FastAPI"]}\n```'
    assert extract_json(raw)["match_score"] == 75


def test_extract_skills():
    skills = extract_skills("Нужен Python, git и знание SQL. Опыт с C++ плюс.")
    assert {"Python", "Git", "SQL", "C++"} <= set(skills)


def test_strip_html():
    assert strip_html("<p>Привет <highlighttext>мир</highlighttext></p>&amp;") == "Привет мир\n&"


def test_split_message_limit():
    text = "\n".join(["строка " * 20] * 100)
    parts = split_message(text, 500)
    assert all(len(p) <= 500 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")

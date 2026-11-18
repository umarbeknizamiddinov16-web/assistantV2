import html
import json
import re

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(br|p|li|ul|ol|div)[^>]*>", re.I)

KNOWN_SKILLS = [
    "Python", "Django", "Flask", "FastAPI", "aiogram", "SQL", "PostgreSQL", "MySQL", "MongoDB",
    "Redis", "Docker", "Kubernetes", "Git", "GitHub", "Linux", "JavaScript", "TypeScript",
    "React", "Vue", "Node.js", "Java", "Kotlin", "C++", "C#", ".NET", "Golang", "PHP", "Laravel",
    "HTML", "CSS", "REST", "API", "AWS", "Pandas", "NumPy", "Excel", "Figma", "1C", "Selenium",
    "Celery", "RabbitMQ", "Kafka", "CI/CD", "Jira", "Scrum", "Agile", "ООП", "OOP",
    "Machine Learning", "Data Science", "Power BI", "Tableau",
]


def format_salary(salary: dict | None) -> str:
    """Зарплата из ответа hh.ru в одну строку: «от 1 000 до 2 000 UZS». Пустая строка, если не указана."""
    if not isinstance(salary, dict):
        return ""
    low, high = salary.get("from"), salary.get("to")
    money = lambda v: f"{int(v):,}".replace(",", "\u00a0")  # noqa: E731
    if low and high:
        text = f"{money(low)}–{money(high)}"
    elif low:
        text = f"от {money(low)}"
    elif high:
        text = f"до {money(high)}"
    else:
        return ""
    return f"{text} {salary.get('currency') or ''}".strip()


def strip_html(text: str) -> str:
    text = _BLOCK_RE.sub("\n", text or "")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def extract_skills(text: str) -> list[str]:
    found = []
    for skill in KNOWN_SKILLS:
        pattern = r"(?<![\w+#.])" + re.escape(skill) + r"(?![\w+#])"
        if re.search(pattern, text or "", re.I):
            found.append(skill)
    return found


def extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (с учётом ```json-обёрток и лишнего текста)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON не найден в ответе модели")
    return json.loads(text[start : end + 1])


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Режет длинный текст на части под лимит Telegram (4096), стараясь по строкам."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts

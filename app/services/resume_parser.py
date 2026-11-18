import asyncio
import io
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_RESUME_CHARS = 20_000
MIN_RESUME_CHARS = 50
SUPPORTED = {".pdf", ".docx", ".txt"}


class ResumeParseError(ValueError):
    """Ошибка с сообщением, безопасным для показа пользователю."""


class ResumeParser:
    """Извлекает текст из .pdf / .docx / .txt. Тяжёлая работа уходит в поток, event loop не блокируется."""

    async def parse(self, data: bytes, filename: str) -> str:
        ext = Path(filename or "").suffix.lower()
        if ext not in SUPPORTED:
            raise ResumeParseError("Поддерживаются файлы .pdf, .docx и .txt")
        if len(data) > MAX_FILE_BYTES:
            raise ResumeParseError("Файл слишком большой (максимум 5 МБ)")
        try:
            text = await asyncio.to_thread(self._extract, data, ext)
        except ResumeParseError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ResumeParseError("Не удалось прочитать файл. Проверьте, что он не повреждён, или отправьте текстом.") from e
        return self.normalize(text)

    @staticmethod
    def normalize(text: str) -> str:
        text = (text or "").replace("\x00", "").strip()
        if len(text) < MIN_RESUME_CHARS:
            raise ResumeParseError("Текста слишком мало (возможно, это скан). Отправьте резюме текстом.")
        return text[:MAX_RESUME_CHARS]

    @staticmethod
    def _extract(data: bytes, ext: str) -> str:
        if ext == ".txt":
            for enc in ("utf-8-sig", "cp1251"):
                try:
                    return data.decode(enc)
                except UnicodeDecodeError:
                    continue
            return data.decode("utf-8", errors="ignore")
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        from docx import Document

        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)

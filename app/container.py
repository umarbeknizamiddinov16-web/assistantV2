"""Контейнер зависимостей: единственные экземпляры сервисов на всё приложение."""
from app.services.ai_service import AIService
from app.services.analysis_service import AnalysisService
from app.services.chat_service import ChatService
from app.services.resume_parser import ResumeParser
from app.services.scraper import HHScraper

ai = AIService()
scraper = HHScraper()
resume_parser = ResumeParser()
analysis = AnalysisService(ai, scraper)
chat = ChatService(ai, scraper, analysis)

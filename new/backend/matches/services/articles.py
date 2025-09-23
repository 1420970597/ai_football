from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Dict, List, Optional

from bs4 import BeautifulSoup
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from search_tools.sogou_searcher import SogouSearcher

from matches.models import Article, Match, SiteConfiguration
from matches.services.ai_analysis import AiModelClient, ArticleAnalyzer, MatchInsightBuilder

logger = logging.getLogger(__name__)


@dataclass
class ArticleResult:
    title: str
    url: str
    summary: str
    author: str
    source: str
    publish_datetime: Optional[timezone.datetime]
    publish_time_text: str
    image_url: str


class SogouArticleService:
    def __init__(self) -> None:
        self.searcher = SogouSearcher()

    def search_recent(self, keyword: str, hours: int = 48, max_pages: int = 6) -> List[ArticleResult]:
        raw_articles = self.searcher.search_recent_articles(keyword, hours=hours, max_pages=max_pages)
        results: List[ArticleResult] = []
        for article in raw_articles or []:
            publish_dt = article.get('publish_datetime')
            if publish_dt and timezone.is_naive(publish_dt):
                publish_dt = timezone.make_aware(publish_dt, timezone.get_current_timezone())
            results.append(
                ArticleResult(
                    title=article.get('title') or 'Untitled Article'[:512],
                    url=article.get('url', ''),
                    summary=article.get('summary', '')[:2000],
                    author=article.get('author') or ''[:255],
                    source=article.get('source') or ''[:255],
                    publish_datetime=publish_dt,
                    publish_time_text=article.get('publish_time', ''),
                    image_url=article.get('image_url', ''),
                )
            )
        return results


class ArticleCrawlerPool:
    def __init__(self, thread_count: int, fetch_timeout: int = 45) -> None:
        self.thread_count = max(1, thread_count)
        self.fetch_timeout = fetch_timeout
        self.queue: "Queue[int]" = Queue()
        self.threads: List[threading.Thread] = []
        self.stop_event = threading.Event()
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        for index in range(self.thread_count):
            thread = threading.Thread(target=self._worker, name=f"article-worker-{index+1}", daemon=True)
            thread.start()
            self.threads.append(thread)
        logger.info("Article crawler pool started with %s threads", self.thread_count)

    def submit(self, article_id: int) -> None:
        if not self.started:
            self.start()
        self.queue.put(article_id)

    def shutdown(self) -> None:
        self.stop_event.set()
        for _ in self.threads:
            self.queue.put_nowait(-1)
        for thread in self.threads:
            thread.join(timeout=2)
        self.started = False
        logger.info("Article crawler pool stopped")

    def _worker(self) -> None:
        driver = None
        try:
            driver = self._create_driver()
            wait = WebDriverWait(driver, self.fetch_timeout)
            while not self.stop_event.is_set():
                try:
                    article_id = self.queue.get(timeout=1)
                except Empty:
                    continue
                if article_id == -1:
                    break
                try:
                    self._process_article(driver, wait, article_id)
                except Exception as exc:  # article might already belong to another match; keep original link
                    logger.exception("Failed to process article %s", article_id, exc_info=exc)
                finally:
                    self.queue.task_done()
        finally:
            if driver:
                try:
                    driver.quit()
                except WebDriverException:
                    pass

    def _create_driver(self) -> webdriver.Chrome:
        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1280,1080')
        options.add_argument('--blink-settings=imagesEnabled=false')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-notifications')
        driver_path = settings.ROOT_DIR / 'chromedriver.exe'
        service = Service(str(driver_path)) if driver_path.exists() else Service()
        return webdriver.Chrome(service=service, options=options)

    def _process_article(self, driver: webdriver.Chrome, wait: WebDriverWait, article_id: int) -> None:
        close_old_connections()
        try:
            article = Article.objects.select_related('match').get(id=article_id)
        except Article.DoesNotExist:
            logger.warning("Article %s no longer exists", article_id)
            return

        logger.info("Fetching article content: %s", article.url)
        try:
            driver.get(article.url)
            wait.until(lambda drv: drv.execute_script('return document.readyState') == 'complete')
            time.sleep(1.5)
            html = driver.page_source
        except TimeoutException:
            logger.warning("Loading article timeout: %s", article.url)
            article.status = Article.STATUS_FAILED
            article.save(update_fields=['status', 'updated_at'])
            return
        except WebDriverException as exc:
            logger.error("Webdriver error: %s", exc)
            article.status = Article.STATUS_FAILED
            article.save(update_fields=['status', 'updated_at'])
            return

        text_content = self._extract_text(html)

        Article.objects.filter(id=article.id).update(
            content_html=html,
            content_text=text_content,
            status=Article.STATUS_FETCHED,
            updated_at=timezone.now(),
        )

        config = SiteConfiguration.load()
        ai_client = AiModelClient(config)
        analyzer = ArticleAnalyzer(ai_client, config)
        match_builder = MatchInsightBuilder(ai_client, config)

        if ai_client.is_enabled():
            analysis = analyzer.analyze(article, text_content)
            if analysis:
                Article.objects.filter(id=article.id).update(status=Article.STATUS_ANALYZED, updated_at=timezone.now())
                try:
                    match_builder.build(article.match)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.exception("Failed to update match insight for %s", article.match_id, exc_info=exc)
        else:
            logger.debug("AI disabled, skipping analysis for article %s", article.id)

    @staticmethod
    def _extract_text(html: str) -> str:
        soup = BeautifulSoup(html, 'lxml')
        article_block = soup.find('article') or soup
        paragraphs = article_block.find_all(['p', 'h1', 'h2', 'h3', 'li'])
        text_parts = [p.get_text(strip=True) for p in paragraphs]
        return '\n'.join(part for part in text_parts if part)


def sync_articles_for_match(match: Match, keyword: Optional[str] = None, *, pool: ArticleCrawlerPool) -> None:
    config = SiteConfiguration.load()
    if not config.sogou_enabled:
        logger.debug("Sogou search disabled; skipping")
        return

    keyword = keyword or f"{match.home_team.name} vs {match.away_team.name}"
    search_service = SogouArticleService()
    articles = search_service.search_recent(keyword)

    new_articles = 0
    for item in articles:
        if not item.url:
            continue
        article, created = Article.objects.get_or_create(
            url=item.url,
            defaults={
                'match': match,
                'title': item.title,
                'summary': item.summary,
                'author': item.author,
                'source': item.source,
                'publish_datetime': item.publish_datetime,
                'publish_time_text': item.publish_time_text,
                'image_url': item.image_url,
                'status': Article.STATUS_PENDING,
            }
        )
        if created:
            new_articles += 1
            pool.submit(article.id)
        elif article.match_id != match.id:
            # article might already belong to another match; keep original link
            logger.debug("Article %s already linked to match %s", article.id, article.match_id)

    if new_articles:
        Match.objects.filter(id=match.id).update(latest_article_synced_at=timezone.now())
        logger.info("Match %s synced %s new articles", match.id, new_articles)


__all__ = ['ArticleCrawlerPool', 'sync_articles_for_match']



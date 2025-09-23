from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from django.conf import settings

from matches.services.articles import ArticleCrawlerPool
from matches.services.updater import UpdateCoordinator

logger = logging.getLogger(__name__)

_article_pool: Optional[ArticleCrawlerPool] = None
_update_coordinator: Optional[UpdateCoordinator] = None


def start_runtime_services() -> None:
    global _article_pool, _update_coordinator  # pylint: disable=global-statement
    if _article_pool is None:
        _article_pool = ArticleCrawlerPool(
            thread_count=settings.ARTICLE_SCRAPER_THREADS,
            fetch_timeout=settings.ARTICLE_FETCH_TIMEOUT,
        )
        _article_pool.start()
    if _update_coordinator is None:
        _update_coordinator = UpdateCoordinator(article_pool=_article_pool)
        _update_coordinator.start()
    logger.info("Runtime services initialized")


def get_article_pool() -> ArticleCrawlerPool:
    if _article_pool is None:
        raise RuntimeError('Article pool not initialized')
    return _article_pool


def get_update_coordinator() -> UpdateCoordinator:
    if _update_coordinator is None:
        raise RuntimeError('Update coordinator not initialized')
    return _update_coordinator


def should_start_services() -> bool:
    argv = sys.argv
    if 'runserver' in argv or os.environ.get('MATCH_SERVICE_FORCE_START') == '1':
        if os.environ.get('RUN_MAIN') == 'true' or not settings.DEBUG:
            return True
    return False


__all__ = ['start_runtime_services', 'get_article_pool', 'get_update_coordinator', 'should_start_services']

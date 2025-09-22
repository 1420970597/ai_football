from rest_framework.routers import DefaultRouter

from .views import (
    AIInsightViewSet,
    ArticleViewSet,
    MatchViewSet,
    RecommendationViewSet,
    TeamViewSet,
)

router = DefaultRouter()
router.register('teams', TeamViewSet)
router.register('matches', MatchViewSet)
router.register('articles', ArticleViewSet)
router.register('ai-insights', AIInsightViewSet)
router.register('recommendations', RecommendationViewSet)

urlpatterns = router.urls


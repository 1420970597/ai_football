from django.urls import include, path
from rest_framework.routers import DefaultRouter

from matches.views import ManualUpdateTriggerView, MatchViewSet, SiteConfigurationView

router = DefaultRouter()
router.register('matches', MatchViewSet, basename='matches')

urlpatterns = [
    path('', include(router.urls)),
    path('settings/', SiteConfigurationView.as_view(), name='site-configuration'),
    path('updates/trigger/', ManualUpdateTriggerView.as_view(), name='manual-update-trigger'),
]

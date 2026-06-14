from rest_framework.routers import DefaultRouter

from .views import (
    AuditEventViewSet,
    ContentViewSet,
    ContextViewSet,
    EducationalTopicViewSet,
)

router = DefaultRouter()
router.register("topics", EducationalTopicViewSet, basename="wims-topic")
router.register("contexts", ContextViewSet, basename="wims-context")
router.register("contents", ContentViewSet, basename="wims-content")
router.register("audit", AuditEventViewSet, basename="wims-audit")

urlpatterns = router.urls

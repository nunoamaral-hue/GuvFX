from django.urls import path
from .views import ValidateMt5View, Mt5StatusView

urlpatterns = [
    path("validate/", ValidateMt5View.as_view()),
    path("status/", Mt5StatusView.as_view()),
]

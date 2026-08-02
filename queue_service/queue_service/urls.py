"""Root URLconf. The layout lives in api/urls.py; this file only mounts it."""

from django.urls import include, path

urlpatterns = [
    path("", include("api.urls")),
]

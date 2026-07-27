from django.urls import path

from .views import BookingView

urlpatterns = [
    # No trailing slash: this is a POST endpoint, and APPEND_SLASH cannot redirect a POST without
    # losing its body — so clients must hit this exact path.
    path("events/<slug:event_id>/book", BookingView.as_view(), name="book"),
]

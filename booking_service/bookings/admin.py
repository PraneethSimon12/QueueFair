from django.contrib import admin

from .models import Booking, Event


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "name", "tickets_booked", "capacity", "created_at")
    search_fields = ("event_id", "name")


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "user_id", "token_jti", "created_at")
    list_filter = ("event",)
    search_fields = ("user_id", "token_jti")

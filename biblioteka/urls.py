from django.contrib.auth.views import LogoutView
from django.urls import path
from .views import *

urlpatterns = [
    path('booking/', booking, name = "booking"),
    path('catalog/', catalog, name = "catalog"),
    path('location/', location, name = "location"),
    path('', main, name = "main"),
    path('profile/', profile_view, name = "profile"),
    path('profile/cancel-booking/<int:booking_id>/',cancel_booking_view, name='cancel_booking'),
    path('reg/', reg, name = "reg"),
    path('book/<int:book_id>/', book, name='book'),
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),

    path('api/rooms/', get_rooms, name='api_rooms'),
    path('api/availability/', get_availability, name='api_availability'),
    path('api/book/', create_booking, name='api_book'),
    path('api/books/', api_books, name='api_books'),
    path('loans/<int:loan_id>/mark-lost/', mark_book_lost, name='mark_book_lost'),
    path('loans/<int:loan_id>/pay-fine/', create_payment, name='pay_fine'),
    path('fines/<int:fine_id>/status/', check_fine_status, name='check_fine_status'),
    path('yookassa/webhook/', yookassa_webhook, name='yookassa_webhook'),
    path('profile/cancel-booking/<int:booking_id>/', cancel_booking_view, name='cancel_booking'),
    path('books/<int:book_id>/', book_detail, name='book_detail'),
    path('books/<int:book_id>/book/', book_book, name='book_book'),
    path('books/booking/<int:booking_id>/cancel/', cancel_book_booking, name='cancel_book_booking'),



]
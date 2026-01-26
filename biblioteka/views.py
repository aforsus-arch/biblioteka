import base64
import json
import uuid
from datetime import datetime, timedelta

import requests
from _decimal import Decimal
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from pip._internal.configuration import Configuration
#from yookassa_api.schemas import Payment

from .models import Profile, BookLoan, RoomBooking, ReadingRoom, Category, Book, Fine, BookBooking, BookCopy

import json
from datetime import datetime, time, timedelta
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required


from .models import Branch, ReadingRoom, RoomBooking
from .utils import update_fine_status_from_yookassa, get_yookassa_auth_headers


def booking(request):
    branches = Branch.objects.filter(is_active=True).order_by('name')
    # если нужно, можно отдать и список залов, но мы их подгружаем через API
    return render(request, 'biblioteka/booking.html', {'branches': branches})


def catalog(request):
    branches = Branch.objects.filter(is_active=True)
    genres = Category.objects.all()

    # Получаем все книги с авторами
    books = Book.objects.prefetch_related('bookauthor_set__author').all()

    context = {
        'branches': branches,
        'genres': genres,
        'books': books,
    }

    return render(request, 'biblioteka/catalog.html', context)

def location(request):
    return render(request, 'biblioteka/location.html')

def main(request):
    return render(request, 'biblioteka/main.html')

def reg(request):
    return render(request, 'biblioteka/reg.html')


def book(request, book_id):
    """Страница детальной информации о книге"""
    book = get_object_or_404(Book, id=book_id)

    # Подсчет доступных экземпляров
    copies = BookCopy.objects.filter(book=book, status='active')
    total_count = 0
    for copy in copies:
        total_count += copy.book_count  # Суммируем book_count

    print(f"=== ДЕБАГ: Книга '{book.title}' ===")
    print(f"Экземпляров в БД: {copies.count()}")
    print(f"Всего книг (сумма): {total_count}")

    # Проверяем брони пользователя
    has_active_booking = False
    if request.user.is_authenticated:
        has_active_booking = BookBooking.objects.filter(
            user=request.user,
            book_copy__book=book,
            status__in=['pending', 'ready']
        ).exists()

    # Категории книги
    book_categories = Category.objects.filter(
        bookcategory__book=book
    ).distinct()

    # Похожие книги
    similar_books = Book.objects.filter(
        bookcategory__category__in=book_categories
    ).exclude(id=book.id).distinct()[:2]

    context = {
        'book': book,
        'available_copies': total_count,  # Будет 40!
        'available_book_copies': copies,
        'book_categories': book_categories,
        'similar_books': similar_books,
        'has_active_booking': has_active_booking,
    }

    # Используем СУЩЕСТВУЮЩИЙ шаблон book.html
    return render(request, 'biblioteka/book.html', context)


def login_view(request):
    if request.user.is_authenticated:
        return redirect('profile')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        remember_me = request.POST.get('remember_me')  # Чекбокс "Запомнить меня"

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)

            # Обработка "Запомнить меня"
            if not remember_me:
                request.session.set_expiry(0)  # Сессия закончится при закрытии браузера

            return redirect('profile')
        else:
            messages.error(request, 'Неверный логин или пароль')
            return redirect('reg')

    return render(request, 'biblioteka/profile.html')


def logout_view(request):
    logout(request)
    return redirect('reg')


@login_required
def profile_view(request):
    profile, created = Profile.objects.get_or_create(user=request.user)

    if created:
        profile.save()

    # Получаем историю выданных книг для текущего пользователя
    book_loans = BookLoan.objects.filter(user=request.user).select_related(
        'book_copy__book',
        'book_copy__branch'
    ).order_by('-issue_date')

    # Получаем бронирования читального зала для текущего пользователя
    room_bookings = RoomBooking.objects.filter(user=request.user).select_related(
        'room',
        'room__branch'
    ).order_by('-booking_date', '-start_time')

    # ✅ ДОБАВЛЯЕМ: Получаем бронирования книг
    book_bookings = BookBooking.objects.filter(
        user=request.user
    ).select_related(
        'book_copy__book',
        'branch'
    ).order_by('-created_at')

    context = {
        'profile': profile,
        'user': request.user,
        'book_loans': book_loans,
        'room_bookings': room_bookings,
        'book_bookings': book_bookings,  # ✅ Добавляем в контекст
    }

    return render(request, 'biblioteka/profile.html', context)

@require_http_methods(["GET"])
def get_rooms(request):
    branch_id = request.GET.get('branch_id')
    if not branch_id:
        return JsonResponse({'rooms': []})
    rooms = ReadingRoom.objects.filter(branch_id=branch_id, is_active=True)
    data = []
    for r in rooms:
        data.append({
            'id': r.id,
            'name': r.name,
            'total_seats': r.total_seats,
            'has_computers': r.has_computers,
        })
    return JsonResponse({'rooms': data})

@require_http_methods(["GET"])
def get_availability(request):
    """
    Параметры:
      branch - id филиала (branch id)
      hall - "reading" или "computer"
      date - YYYY-MM-DD
    Возвращает:
      { rooms: [ {id, name, total_seats, slots: [{start, end, free}] }, ... ] }
    """
    branch_param = request.GET.get('branch')
    hall = request.GET.get('hall')  # "reading" или "computer"
    date_str = request.GET.get('date')

    if not branch_param or not hall or not date_str:
        return HttpResponseBadRequest("Missing parameters")

    try:
        booking_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return HttpResponseBadRequest("Invalid date")

    # Найдём филиал
    try:
        branch = Branch.objects.get(id=branch_param)
    except Branch.DoesNotExist:
        return HttpResponseBadRequest("Branch not found")

    # Выбираем залы по выбранному типу
    if hall == 'computer':
        rooms_qs = ReadingRoom.objects.filter(branch=branch, has_computers=True, is_active=True)
    else:  # 'reading' or default
        rooms_qs = ReadingRoom.objects.filter(branch=branch, is_active=True, has_computers=False)

    rooms_data = []

    # Генерируем слоты с шагом 1 час от 08:00 до 20:00
    start_hour = 8
    end_hour = 17

    for room in rooms_qs:
        slots = []
        for hour in range(start_hour, end_hour):
            slot_start = time(hour=hour, minute=0)
            slot_end = (datetime.combine(booking_date, slot_start) + timedelta(hours=1)).time()

            # находим подтверждённые брони, которые пересекаются с этим слотом
            overlapping = RoomBooking.objects.filter(
                room=room,
                booking_date=booking_date,
                status='confirmed',
                start_time__lt=slot_end,
                end_time__gt=slot_start
            )
            occupied = sum(b.seats_count for b in overlapping)
            free = max(room.total_seats - occupied, 0)

            slots.append({
                'start': slot_start.strftime("%H:%M"),
                'end': slot_end.strftime("%H:%M"),
                'free': free
            })

        rooms_data.append({
            'id': room.id,
            'name': room.name,
            'total_seats': room.total_seats,
            'slots': slots
        })

    return JsonResponse({'rooms': rooms_data})
@csrf_exempt
@require_http_methods(["POST"])
@login_required
def create_booking(request):
    try:
        data = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    room_id = data.get('room_id')
    date_str = data.get('date')
    start_str = data.get('start')
    end_str = data.get('end')
    seats = int(data.get('seats', 1))

    if not room_id or not date_str or not start_str or not end_str:
        return HttpResponseBadRequest("Missing fields")

    # Парсим
    try:
        booking_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()
    except ValueError:
        return HttpResponseBadRequest("Invalid date/time format")

    # Не позволяем брони в прошлом
    now_date = datetime.now().date()
    if booking_date < now_date:
        return JsonResponse({'success': False, 'message': 'Нельзя бронировать на прошедшую дату'}, status=400)

    room = get_object_or_404(ReadingRoom, id=room_id)

    # Считаем занятые места на пересекающие брони
    overlapping = RoomBooking.objects.filter(
        room=room,
        booking_date=booking_date,
        status='confirmed',
        start_time__lt=end_time,
        end_time__gt=start_time
    )
    occupied = sum(b.seats_count for b in overlapping)

    if occupied + seats > room.total_seats:
        return JsonResponse({'success': False, 'message': 'Недостаточно свободных мест в этом слоте'}, status=400)

    # Дополнительная проверка: у пользователя уже может быть бронь в тот же интервал
    user_conflict = RoomBooking.objects.filter(
        user=request.user,
        booking_date=booking_date,
        status='confirmed',
        start_time__lt=end_time,
        end_time__gt=start_time
    ).exists()
    if user_conflict:
        return JsonResponse({'success': False, 'message': 'У вас уже есть бронь в этот интервал'}, status=400)

    # Создаём бронь
    booking = RoomBooking.objects.create(
        user=request.user,
        room=room,
        booking_date=booking_date,
        start_time=start_time,
        end_time=end_time,
        seats_count=seats,
        status='confirmed'
    )

    return JsonResponse({'success': True, 'booking_id': booking.id})

@require_GET
def api_books(request):
    branch_id = request.GET.get('branch')
    genre_id = request.GET.get('genre')
    search = request.GET.get('search', '').strip()

    books = Book.objects.all()

    if branch_id and branch_id != 'all':
        books = books.filter(bookcopy__branch_id=branch_id).distinct()

    if genre_id and genre_id != 'all':
        books = books.filter(bookcategory__category_id=genre_id).distinct()

    if search:
        books = books.filter(title__icontains=search)  # можно добавить author__full_name__icontains через BookAuthor

    data = []
    for book in books:
        data.append({
            'id': book.id,
            'title': book.title,
            'authors': book.get_authors_display(),
            'description': book.description[:300],  # длина краткого описания
            'publication_year': book.publication_year,
            'pages': book.pages,
        })

    return JsonResponse({'books': data})


@login_required
def create_payment(request, loan_id):
    """Создание платежа для штрафа за утерю книги"""
    print(f"=== Создание платежа для loan_id: {loan_id} ===")

    # Получаем книгу со статусом 'lost'
    loan = get_object_or_404(BookLoan, id=loan_id, user=request.user, status='lost')
    print(f"Книга: {loan.book_copy.book.title}")

    # Ищем или создаем неоплаченный штраф
    fine = Fine.objects.filter(loan=loan, status='unpaid').first()
    if not fine:
        book_price = loan.book_copy.book.price
        if not book_price or book_price <= 0:
            book_price = Decimal('500.00')

        fine = Fine.objects.create(
            user=request.user,
            loan=loan,
            amount=book_price,
            reason=f"Утеря книги: {loan.book_copy.book.title}",
            status='unpaid'
        )
        print(f"Создан новый штраф ID: {fine.id}, сумма: {fine.amount}")
    else:
        print(f"Найден существующий штраф ID: {fine.id}, сумма: {fine.amount}")

    # Форматируем сумму для ЮKassa
    amount_str = str(fine.amount.quantize(Decimal('0.00')))

    # Подготовка данных для ЮKassa
    url = "https://api.yookassa.ru/v3/payments"
    headers = get_yookassa_auth_headers()
    headers["Idempotence-Key"] = str(uuid.uuid4())

    data = {
        "amount": {
            "value": amount_str,
            "currency": "RUB"
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": settings.YOOKASSA_RETURN_URL
        },
        "description": f"Штраф за утерю книги: {loan.book_copy.book.title}",
        "metadata": {
            "loan_id": str(loan_id),
            "fine_id": str(fine.id),
            "user_id": str(request.user.id),
            "type": "book_fine"
        }
    }

    try:
        response = requests.post(url, headers=headers, data=json.dumps(data), timeout=30)
        print(f"Статус ответа ЮKassa: {response.status_code}")

        if response.status_code in (200, 201):
            payment = response.json()
            print(f"Платеж создан: {payment['id']}")

            # Сохраняем ID платежа
            fine.yookassa_payment_id = payment['id']
            fine.save()

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'payment_url': payment["confirmation"]["confirmation_url"],
                    'fine_id': fine.id,
                    'payment_id': payment['id']
                })
            else:
                return redirect(payment["confirmation"]["confirmation_url"])

        else:
            error_text = response.text[:200] if response.text else "Нет текста ошибки"
            print(f"Ошибка ЮKassa: {response.status_code} - {error_text}")

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': f'Ошибка ЮKassa ({response.status_code})'
                }, status=400)
            else:
                messages.error(request, f"Ошибка создания платежа: {response.status_code}")
                return redirect('profile')

    except requests.exceptions.Timeout:
        print("Таймаут при запросе к ЮKassa")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'Таймаут при соединении с платежной системой'
            }, status=408)
        else:
            messages.error(request, "Таймаут при соединении с платежной системой")
            return redirect('profile')

    except Exception as e:
        print(f"Исключение: {str(e)}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': f'Ошибка: {str(e)}'
            }, status=500)
        else:
            messages.error(request, f"Ошибка: {str(e)}")
            return redirect('profile')


@login_required
def check_fine_status(request, fine_id):
    """Проверка статуса оплаты штрафа с запросом к ЮKassa"""
    fine = get_object_or_404(Fine, id=fine_id, user=request.user)

    # Если штраф не оплачен и есть ID платежа, проверяем в ЮKassa
    if fine.status == 'unpaid' and fine.yookassa_payment_id:
        print(f"Проверяем статус платежа {fine.yookassa_payment_id} в ЮKassa...")
        updated = update_fine_status_from_yookassa(fine)
        if updated:
            print(f"Статус обновлен: {fine.status}")

    return JsonResponse({
        'status': fine.status,
        'paid': fine.status == 'paid',
        'paid_at': fine.paid_at.strftime("%d.%m.%Y %H:%M") if fine.paid_at else None,
        'payment_id': fine.yookassa_payment_id
    })


@csrf_exempt
def yookassa_webhook(request):
    """Обработчик вебхуков от ЮKassa"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body.decode('utf-8'))
            event = data.get("event")

            if event == "payment.succeeded":
                payment = data.get("object", {})
                payment_id = payment.get("id")
                metadata = payment.get("metadata", {})
                fine_id = metadata.get("fine_id")

                print(f"Вебхук: Платеж {payment_id} успешен, штраф ID: {fine_id}")

                if fine_id:
                    try:
                        fine = Fine.objects.get(id=fine_id)
                        if fine.status != 'paid':
                            fine.mark_as_paid(payment_id)
                            print(f"✅ Штраф {fine_id} помечен как оплаченный")
                        else:
                            print(f"⚠️ Штраф {fine_id} уже оплачен")

                    except Fine.DoesNotExist:
                        print(f"❌ Штраф {fine_id} не найден в БД")

            elif event == "payment.canceled":
                payment = data.get("object", {})
                metadata = payment.get("metadata", {})
                fine_id = metadata.get("fine_id")

                if fine_id:
                    try:
                        fine = Fine.objects.get(id=fine_id)
                        fine.status = 'cancelled'
                        fine.save()
                        print(f"❌ Штраф {fine_id} помечен как отмененный")
                    except Fine.DoesNotExist:
                        print(f"❌ Штраф {fine_id} не найден в БД")

        except Exception as e:
            print(f"❌ Ошибка обработки вебхука: {e}")

    return JsonResponse({"status": "ok"})


@login_required
def mark_book_lost(request, loan_id):
    """Пометить книгу как утерянную"""
    loan = get_object_or_404(BookLoan, id=loan_id, user=request.user)

    # Проверяем, можно ли пометить как утерянную
    if loan.status in ['active', 'overdue']:
        loan.status = 'lost'
        loan.save()

        # Создаем запись о штрафе
        fine = Fine.objects.create(
            user=request.user,
            loan=loan,
            amount=loan.book_copy.book.price or Decimal('500.00'),
            reason=f"Утеря книги: {loan.book_copy.book.title}",
            status='unpaid'
        )

        return JsonResponse({
            'success': True,
            'book_price': float(loan.book_copy.book.price or 500),
            'fine_id': fine.id
        })

    return JsonResponse({'success': False, 'error': 'Невозможно пометить книгу как утерянную'}, status=400)

@login_required
def cancel_booking_view(request, booking_id):
    """Простое удаление бронирования через AJAX"""
    try:
        # Находим бронирование текущего пользователя
        booking = get_object_or_404(RoomBooking, id=booking_id, user=request.user)

        # Просто удаляем - никаких лишних проверок
        booking.delete()

        return JsonResponse({
            'success': True
        })

    except Exception as e:
        # В случае любой ошибки возвращаем false
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)


def book_detail(request, book_id):
    """Жестко фиксированная версия"""
    book = get_object_or_404(Book, id=book_id)

    # Жестко фиксируем значение для теста
    total_count = 40

    # Или из базы, но с проверкой
    try:
        copy = BookCopy.objects.get(book=book, status='active')
        total_count = copy.book_count
    except BookCopy.DoesNotExist:
        total_count = 0
    except BookCopy.MultipleObjectsReturned:
        total_count = sum(c.book_count for c in BookCopy.objects.filter(book=book, status='active'))

    context = {
        'book': book,
        'available_copies': total_count,  # Гарантированно число
        'test_value': 123,  # Тестовая переменная
    }

    return render(request, 'biblioteka/book_detail.html', context)
@login_required
def book_book(request, book_id):
    """Бронирование книги"""
    if request.method == 'POST':
        book = get_object_or_404(Book, id=book_id)

        # Проверяем, есть ли у пользователя уже бронь на эту книгу
        existing_booking = BookBooking.objects.filter(
            user=request.user,
            book_copy__book=book,
            status__in=['pending', 'ready']  # активные брони
        ).exists()

        if existing_booking:
            return JsonResponse({
                'success': False,
                'error': 'У вас уже есть активная бронь на эту книгу'
            })

        # Находим доступный экземпляр
        book_copy = BookCopy.objects.filter(
            book=book,
            status='active'
        ).first()

        if not book_copy:
            return JsonResponse({
                'success': False,
                'error': 'Нет доступных экземпляров книги'
            })

        # Создаем бронирование
        from django.utils import timezone
        booking = BookBooking.objects.create(
            user=request.user,
            book_copy=book_copy,
            branch=book_copy.branch,
            status='ready',
            ready_by=timezone.now(),
            pickup_deadline=timezone.now() + timedelta(days=3)  # 3 дня на забрать
        )

        # Меняем статус экземпляра (но не 'returned'!)
        book_copy.status = 'active'  # оставляем активным
        book_copy.save()

        return JsonResponse({
            'success': True,
            'message': f'Книга "{book.title}" забронирована. Заберите до {booking.pickup_deadline.strftime("%d.%m.%Y %H:%M")}',
            'booking_id': booking.id
        })

    return JsonResponse({'success': False, 'error': 'Неверный запрос'})


@login_required
def cancel_book_booking(request, booking_id):
    """Отмена бронирования книги"""
    if request.method == 'POST':
        booking = get_object_or_404(BookBooking, id=booking_id, user=request.user)

        # Можно отменять только брони в статусе pending или ready
        if booking.status not in ['pending', 'ready']:
            return JsonResponse({
                'success': False,
                'error': 'Невозможно отменить это бронирование'
            })

        # Освобождаем экземпляр книги
        book_copy = booking.book_copy
        book_copy.status = 'active'  # Делаем снова доступной
        book_copy.save()

        # Меняем статус брони
        booking.status = 'cancelled'
        booking.save()

        return JsonResponse({'success': True})

    return JsonResponse({'success': False, 'error': 'Неверный запрос'})


def get_yookassa_auth_headers():
    """Заглушка вместо настоящих заголовков ЮКассы"""
    return {
        'Authorization': 'Basic stub',
        'Content-Type': 'application/json',
        'Idempotence-Key': 'stub'
    }


def update_fine_status_from_yookassa(fine):
    """Заглушка - всегда возвращает False"""
    print(f"Заглушка: проверка статуса платежа {fine.id}")
    return False


# Переопределяем функции чтобы они не падали
@login_required
def create_payment(request, loan_id):
    """Заглушка для платежей на Railway"""
    print(f"ЗАГЛУШКА: Платеж для loan_id: {loan_id}")

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': False,
            'error': 'Платежная система временно недоступна'
        }, status=503)
    else:
        messages.info(request, "Платежная система временно недоступна. Пожалуйста, обратитесь в библиотеку.")
        return redirect('profile')


@login_required
def check_fine_status(request, fine_id):
    """Заглушка для проверки статуса"""
    fine = get_object_or_404(Fine, id=fine_id, user=request.user)
    return JsonResponse({
        'status': 'unpaid',  # всегда unpaid
        'paid': False,
        'paid_at': None,
        'payment_id': None
    })


@csrf_exempt
def yookassa_webhook(request):
    """Заглушка для вебхуков"""
    return JsonResponse({"status": "ok"})
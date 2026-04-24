"""
Представления (views) для сайта инструктора по вождению.
"""
import json
import time as time_module
import calendar
import hashlib
from datetime import date, time, timedelta

from django.contrib.auth import login, logout
from django.views.decorators.cache import never_cache
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages

from .models import TimeSlot, Booking, Service
from .forms import BookingForm, AddSlotsForm, FindBookingForm, OwnerNoteForm, ManualBookingForm
from .emails import notify_client_action
from .constants import ALL_DAY_SLOTS


# ─── Публичные страницы ───────────────────────────────────────────────────────

def index(request):
    """Главная страница лендинга."""
    services = Service.objects.filter(is_active=True).order_by("order", "id")
    return render(request, "core/index.html", {"services": services})


def privacy(request):
    """Политика конфиденциальности."""
    return render(request, "core/privacy.html")


def robots_txt(request):
    """robots.txt — отдаётся Django в dev; в продакшне перехватывается веб-сервером."""
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /dashboard/\n"
        "Disallow: /login/\n"
        "Disallow: /logout/\n"
        "Disallow: /api/\n\n"
        "Sitemap: https://ivan-gunichev.ru/sitemap.xml\n"
    )
    return HttpResponse(content, content_type="text/plain")


def sitemap_xml(request):
    """sitemap.xml — отдаётся Django в dev; в продакшне перехватывается веб-сервером."""
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://ivan-gunichev.ru/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://ivan-gunichev.ru/booking/</loc>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://ivan-gunichev.ru/my-booking/</loc>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://ivan-gunichev.ru/privacy/</loc>
    <changefreq>yearly</changefreq>
    <priority>0.3</priority>
  </url>
</urlset>"""
    return HttpResponse(content, content_type="application/xml")


def booking_page(request):
    """Страница онлайн-записи с календарём."""
    form = BookingForm()
    return render(request, "core/booking.html", {"form": form})


def booking_success(request):
    """Страница подтверждения успешной записи."""
    booking_data = request.session.pop("last_booking", None)
    if not booking_data:
        return redirect("booking")
    return render(request, "core/booking_success.html", {"booking": booking_data})


# ─── AJAX API для публичного календаря ───────────────────────────────────────

@require_GET
def available_dates(request):
    """
    Возвращает даты с доступными слотами для указанного месяца.
    GET: year, month → {YYYY-MM-DD: count}
    """
    today = date.today()
    try:
        year  = int(request.GET.get("year",  today.year))
        month = int(request.GET.get("month", today.month))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Некорректные параметры"}, status=400)

    start_date = max(date(year, month, 1), today)
    _, last_day = calendar.monthrange(year, month)
    end_date = date(year, month, last_day)

    free_slots = (
        TimeSlot.objects.filter(
            date__range=(start_date, end_date),
            is_available=True,
        )
        .exclude(booking__isnull=False)
        .values("date")
    )

    dates_map = {}
    for slot in free_slots:
        d = slot["date"].strftime("%Y-%m-%d")
        dates_map[d] = dates_map.get(d, 0) + 1

    return JsonResponse(dates_map)


@require_GET
def slots_for_date(request):
    """
    Возвращает слоты на конкретную дату.
    GET: date → [{id, time, available, status}, ...]
    """
    date_str = request.GET.get("date")
    if not date_str:
        return JsonResponse({"error": "Параметр date обязателен"}, status=400)

    try:
        selected_date = date.fromisoformat(date_str)
    except ValueError:
        return JsonResponse({"error": "Некорректный формат даты"}, status=400)

    slots = TimeSlot.objects.filter(date=selected_date).prefetch_related("booking")

    result = []
    for slot in slots:
        is_free = slot.is_available and not slot.is_booked and not slot.is_past
        result.append({
            "id": slot.pk,
            "time": slot.get_time_range(),
            "start": slot.start_time.strftime("%H:%M"),
            "end": slot.end_time.strftime("%H:%M"),
            "available": is_free,
            "status": slot.status_class,
        })

    return JsonResponse(result, safe=False)


@require_POST
def create_booking(request):
    """
    AJAX: создать запись.
    Принимает JSON или form-data.
    """
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Некорректный JSON"}, status=400)
    else:
        data = request.POST

    form = BookingForm(data)

    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        return JsonResponse({"success": False, "error": first_error}, status=400)

    cd = form.cleaned_data

    try:
        slot = TimeSlot.objects.select_for_update().get(
            pk=cd["slot_id"], is_available=True,
        )
    except TimeSlot.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Слот больше недоступен. Выберите другое время."},
            status=409,
        )

    if slot.is_booked:
        return JsonResponse(
            {"success": False, "error": "Этот слот уже занят. Выберите другое время."},
            status=409,
        )

    booking = Booking.objects.create(
        slot=slot,
        name=cd["name"],
        phone=cd["phone"],
        comment=cd.get("comment", ""),
        service="",
        slot_date=slot.date,
        slot_time_str=slot.get_time_range(),
    )

    request.session["last_booking"] = {
        "name": booking.name,
        "phone": booking.phone,
        "date": slot.date.strftime("%d.%m.%Y"),
        "time": slot.get_time_range(),
        "comment": booking.comment,
    }

    # Уведомляем владельца — только клиентское действие
    try:
        notify_client_action(booking, "created")
    except Exception:
        pass  # Уведомление не критично для операции

    return JsonResponse({
        "success": True,
        "redirect": "/booking/success/",
        "message": "Запись успешно создана!",
    })


# ─── Самообслуживание клиента ─────────────────────────────────────────────────

@never_cache
def my_booking_page(request):
    """
    Страница самообслуживания: найти, отменить, перенести запись.
    @never_cache + no-store headers: защита от показа чужих данных из кеша браузера.
    """
    # Результат предыдущего действия (отмена / перенос) — показываем экран успеха
    action_result = request.session.pop("mybooking_result", None)
    if action_result:
        response = render(request, "core/my_booking.html", {"action_result": action_result})
        _set_no_cache_headers(response)
        return response

    # Если запись была найдена в текущей сессии — показываем её
    found_booking = _get_session_booking(request)
    form = FindBookingForm()
    response = render(request, "core/my_booking.html", {
        "form": form,
        "found_booking": found_booking,
    })
    _set_no_cache_headers(response)
    return response


def _set_no_cache_headers(response):
    """Выставляет HTTP-заголовки запрета кеширования."""
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"]        = "no-cache"
    response["Expires"]       = "0"


@never_cache
@require_POST
def find_my_booking(request):
    """
    POST: ищет запись по номеру телефона.
    Rate-limiting через сессию: не более 8 попыток за 10 минут.
    """
    now = time_module.time()
    lookups = [t for t in request.session.get("booking_lookups", []) if now - t < 600]
    if len(lookups) >= 8:
        messages.error(request, "Слишком много попыток. Подождите немного или позвоните: +7 (905) 560-96-96")
        return redirect("my_booking")

    lookups.append(now)
    request.session["booking_lookups"] = lookups

    form = FindBookingForm(request.POST)
    if not form.is_valid():
        return render(request, "core/my_booking.html", {"form": form, "found_booking": None})

    phone = form.cleaned_data["phone"]
    booking = (
        Booking.objects.filter(phone=phone, slot__date__gte=date.today())
        .select_related("slot")
        .order_by("slot__date", "slot__start_time")
        .first()
    )

    if not booking:
        messages.warning(
            request,
            f"Запись для номера {phone} не найдена. "
            "Возможно, занятие уже прошло или запись была отменена.",
        )
        return render(request, "core/my_booking.html", {"form": form, "found_booking": None})

    token = _make_booking_token(booking.id, phone)
    request.session["client_booking"] = {
        "booking_id": booking.id,
        "phone": phone,
        "token": token,
    }
    return redirect("my_booking")


@require_POST
def cancel_my_booking(request):
    """Отмена записи клиентом (через сессию)."""
    session_data = request.session.get("client_booking")
    if not session_data:
        messages.error(request, "Сессия истекла. Введите номер телефона снова.")
        return redirect("my_booking")

    booking = get_object_or_404(Booking, pk=session_data["booking_id"])
    expected_token = _make_booking_token(booking.id, session_data["phone"])
    if session_data.get("token") != expected_token:
        messages.error(request, "Ошибка безопасности. Попробуйте снова.")
        return redirect("my_booking")

    # Сохраняем дату/время до обнуления слота
    booking.cache_slot_info()
    slot_date = booking.slot_date.strftime("%d.%m.%Y") if booking.slot_date else "—"
    slot_time = booking.slot_time_str or "—"

    # Освобождаем слот
    if booking.slot:
        booking.slot.is_available = True
        booking.slot.save(update_fields=["is_available"])

    # Обнуляем FK, ставим статус cancelled — данные в БД остаются навсегда
    booking.slot   = None
    booking.status = Booking.STATUS_CANCELLED
    booking.save(update_fields=["slot", "status", "slot_date", "slot_time_str"])
    request.session.pop("client_booking", None)

    # Клиентское действие — уведомляем владельца
    try:
        notify_client_action(booking, "cancelled")
    except Exception:
        pass

    request.session["mybooking_result"] = {
        "action": "cancelled",
        "title": "Запись отменена",
        "message": f"Занятие {slot_date} в {slot_time} успешно отменено.",
    }
    return redirect("my_booking")


def reschedule_my_booking(request):
    """
    GET: показывает календарь для выбора нового слота.
    POST: сохраняет перенос.
    """
    session_data = request.session.get("client_booking")
    if not session_data:
        messages.error(request, "Сессия истекла. Введите номер телефона снова.")
        return redirect("my_booking")

    booking = get_object_or_404(Booking, pk=session_data["booking_id"])
    expected_token = _make_booking_token(booking.id, session_data["phone"])
    if session_data.get("token") != expected_token:
        messages.error(request, "Ошибка безопасности. Попробуйте снова.")
        return redirect("my_booking")

    if request.method == "POST":
        slot_id = request.POST.get("slot_id")
        if not slot_id:
            messages.error(request, "Выберите новое время.")
            return render(request, "core/reschedule.html", {"booking": booking})

        try:
            new_slot = TimeSlot.objects.get(pk=int(slot_id), is_available=True)
        except (TimeSlot.DoesNotExist, ValueError):
            messages.error(request, "Выбранный слот недоступен.")
            return render(request, "core/reschedule.html", {"booking": booking})

        if new_slot.is_booked:
            messages.error(request, "Этот слот уже занят.")
            return render(request, "core/reschedule.html", {"booking": booking})

        old_date  = booking.slot.date.strftime("%d.%m.%Y")
        old_time  = booking.slot.get_time_range()
        new_date  = new_slot.date.strftime("%d.%m.%Y")
        new_time  = new_slot.get_time_range()

        booking.slot = new_slot
        booking.cache_slot_info()
        booking.save(update_fields=["slot", "slot_date", "slot_time_str"])
        request.session.pop("client_booking", None)

        # Клиентское действие — уведомляем владельца
        try:
            notify_client_action(booking, "rescheduled")
        except Exception:
            pass

        request.session["mybooking_result"] = {
            "action": "rescheduled",
            "title": "Занятие перенесено",
            "message": f"С {old_date} {old_time} → {new_date} {new_time}.",
        }
        return redirect("my_booking")

    return render(request, "core/reschedule.html", {"booking": booking})


def _get_session_booking(request):
    """Вспомогательная: возвращает запись из сессии или None."""
    session_data = request.session.get("client_booking")
    if not session_data:
        return None
    try:
        booking = Booking.objects.select_related("slot").get(pk=session_data["booking_id"])
        expected_token = _make_booking_token(booking.id, session_data["phone"])
        if session_data.get("token") != expected_token:
            return None
        return booking
    except Booking.DoesNotExist:
        request.session.pop("client_booking", None)
        return None


def _make_booking_token(booking_id, phone):
    """Генерирует токен для верификации клиента без пароля."""
    from django.conf import settings
    raw = f"{booking_id}:{phone}:{settings.SECRET_KEY[:16]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ─── Аутентификация ───────────────────────────────────────────────────────────

def owner_login(request):
    """Скрытая страница входа для владельца."""
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("dashboard")
    return render(request, "registration/login.html", {"form": form})


def owner_logout(request):
    """Выход из кабинета."""
    logout(request)
    return redirect("/")


# ─── Личный кабинет: главная ─────────────────────────────────────────────────

@login_required
def dashboard(request):
    """Главная страница кабинета: 14-дневный сеточный календарь."""
    today = date.today()
    days_ahead = 30  # Синхронизировано с горизонтом клиента
    schedule_dates = [today + timedelta(days=i) for i in range(days_ahead)]

    all_slots = (
        TimeSlot.objects.filter(date__in=schedule_dates)
        .prefetch_related("booking")
        .order_by("date", "start_time")
    )

    schedule = {}
    for d in schedule_dates:
        schedule[d] = {"free": 0, "booked": 0, "closed": 0, "slots": []}
    for slot in all_slots:
        day = schedule[slot.date]
        day["slots"].append(slot)
        if slot.is_booked:
            day["booked"] += 1
        elif not slot.is_available:
            day["closed"] += 1
        else:
            day["free"] += 1

    # Только активные будущие записи (отменённые и завершённые не показываем)
    upcoming_bookings = (
        Booking.objects.filter(slot__date__gte=today, status=Booking.STATUS_ACTIVE)
        .select_related("slot")
        .order_by("slot__date", "slot__start_time")[:10]
    )

    # Статистика за последние 30 дней
    thirty_days_ago = today - timedelta(days=30)
    stats_30d = {
        "active":    Booking.objects.filter(status=Booking.STATUS_ACTIVE, slot__date__gte=today).count(),
        "completed": Booking.objects.filter(
            status=Booking.STATUS_COMPLETED, slot_date__gte=thirty_days_ago
        ).count(),
        "cancelled": Booking.objects.filter(
            status=Booking.STATUS_CANCELLED, slot_date__gte=thirty_days_ago
        ).count(),
    }

    context = {
        "schedule":        schedule,
        "schedule_dates":  schedule_dates,
        "today":           today,
        "upcoming_bookings": upcoming_bookings,
        "total_bookings":  Booking.objects.count(),
        "stats_30d":       stats_30d,
    }
    return render(request, "core/dashboard.html", context)


# ─── Управление конкретным днём ───────────────────────────────────────────────

@login_required
def dashboard_day(request, date_str):
    """Страница управления конкретным днём расписания."""
    try:
        selected_date = date.fromisoformat(date_str)
    except ValueError:
        return redirect("dashboard")

    existing_slots = (
        TimeSlot.objects.filter(date=selected_date)
        .prefetch_related("booking")
        .order_by("start_time")
    )
    existing_map = {s.start_time: s for s in existing_slots}

    slot_data = []
    for start, end in ALL_DAY_SLOTS:
        slot = existing_map.get(start)
        slot_data.append({
            "start": start,
            "end": end,
            "slot": slot,
            "time_range": f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
            "value": f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}",
        })

    context = {
        "selected_date": selected_date,
        "today": date.today(),
        "slot_data": slot_data,
        "is_past": selected_date < date.today(),
        "bookings_today": [s for s in existing_slots if s.is_booked],
        "prev_date": (selected_date - timedelta(days=1)).isoformat(),
        "next_date": (selected_date + timedelta(days=1)).isoformat(),
        "manual_form": ManualBookingForm(),
    }
    return render(request, "core/dashboard_day.html", context)


# ─── AJAX: операции со слотами (без перезагрузки страницы) ───────────────────

@login_required
@require_POST
def add_single_slot(request, date_str):
    """
    AJAX: добавляет один слот. Всегда возвращает JSON.
    """
    try:
        selected_date = date.fromisoformat(date_str)
    except ValueError:
        return JsonResponse({"success": False, "error": "Некорректная дата"}, status=400)

    time_range = request.POST.get("time_range", "")
    try:
        start_str, end_str = time_range.split("-")
        start = time(*map(int, start_str.split(":")))
        end   = time(*map(int, end_str.split(":")))
    except (ValueError, AttributeError):
        return JsonResponse({"success": False, "error": "Некорректный формат времени"}, status=400)

    slot, created = TimeSlot.objects.get_or_create(
        date=selected_date,
        start_time=start,
        defaults={"end_time": end, "is_available": True},
    )

    return JsonResponse({
        "success": True,
        "created": created,
        "slot_id": slot.pk,
        "time_range": f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
        "is_available": slot.is_available,
        "message": "Слот добавлен" if created else "Слот уже существует",
    })


@login_required
@require_POST
def add_slots(request):
    """Быстрое добавление нескольких слотов (из дашборда, AJAX или форма)."""
    form = AddSlotsForm(request.POST)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if not form.is_valid():
        if is_ajax:
            return JsonResponse({"success": False, "error": "Ошибка в форме"}, status=400)
        messages.error(request, "Ошибка в форме.")
        return redirect("dashboard")

    selected_date = form.cleaned_data["date"]
    times = form.cleaned_data["times"]
    added = 0

    for time_range in times:
        start_str, end_str = time_range.split("-")
        start = time(*map(int, start_str.split(":")))
        end   = time(*map(int, end_str.split(":")))
        _, created = TimeSlot.objects.get_or_create(
            date=selected_date,
            start_time=start,
            defaults={"end_time": end, "is_available": True},
        )
        if created:
            added += 1

    if is_ajax:
        return JsonResponse({
            "success": True,
            "added": added,
            "date": selected_date.strftime("%d.%m.%Y"),
        })

    if added:
        messages.success(request, f"Добавлено {added} слот(ов) на {selected_date.strftime('%d.%m.%Y')}.")
    else:
        messages.info(request, "Все выбранные слоты уже существуют.")
    return redirect("dashboard")


@login_required
@require_POST
def delete_slot(request, slot_id):
    """AJAX: удаляет слот (только незанятый)."""
    slot = get_object_or_404(TimeSlot, pk=slot_id)
    if slot.is_booked:
        return JsonResponse({"success": False, "error": "Нельзя удалить занятый слот."}, status=400)
    slot.delete()
    return JsonResponse({"success": True})


@login_required
@require_POST
def toggle_slot(request, slot_id):
    """AJAX: переключает доступность слота."""
    slot = get_object_or_404(TimeSlot, pk=slot_id)
    if slot.is_booked:
        return JsonResponse({"success": False, "error": "Нельзя закрыть занятый слот."}, status=400)
    slot.is_available = not slot.is_available
    slot.save(update_fields=["is_available"])
    return JsonResponse({"success": True, "is_available": slot.is_available})


@login_required
@require_POST
def cancel_booking(request, booking_id):
    """
    AJAX: владелец отменяет запись.
    Запись НЕ удаляется — статус меняется на 'cancelled'.
    Слот освобождается для повторной записи.
    """
    try:
        booking = get_object_or_404(Booking, pk=booking_id)
        name = booking.name

        booking.cache_slot_info()
        slot_str = f"{booking.slot_date.strftime('%d.%m.%Y')} {booking.slot_time_str}" if booking.slot_date else "—"

        if booking.slot:
            booking.slot.is_available = True
            booking.slot.save(update_fields=["is_available"])

        booking.slot   = None
        booking.status = Booking.STATUS_CANCELLED
        booking.save(update_fields=["slot", "status", "slot_date", "slot_time_str"])

        return JsonResponse({
            "success": True,
            "message": f"Запись {name} на {slot_str} отменена.",
        })
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
@require_POST
def create_manual_booking(request, slot_id):
    """
    AJAX: владелец создаёт запись вручную (клиент позвонил / написал).
    Возвращает JSON с данными созданной записи.
    """
    slot = get_object_or_404(TimeSlot, pk=slot_id)

    if slot.is_booked:
        return JsonResponse({"success": False, "error": "Этот слот уже занят."}, status=409)
    if not slot.is_available:
        return JsonResponse({"success": False, "error": "Этот слот закрыт."}, status=409)

    form = ManualBookingForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        return JsonResponse({"success": False, "error": first_error}, status=400)

    cd = form.cleaned_data
    booking = Booking.objects.create(
        slot=slot,
        name=cd["name"],
        phone=cd["phone"],
        comment=cd.get("comment", ""),
        service="",
        slot_date=slot.date,
        slot_time_str=slot.get_time_range(),
    )

    # Создание владельцем — уведомления НЕ отправляем

    return JsonResponse({
        "success": True,
        "booking_id": booking.pk,
        "name": booking.name,
        "phone": booking.phone,
        "comment": booking.comment,
        "message": f"Запись для {booking.name} создана.",
    })


@login_required
@require_GET
def booking_detail(request, booking_id):
    """AJAX: возвращает данные записи для модала."""
    try:
        booking = get_object_or_404(Booking, pk=booking_id)
        d = booking.get_display_date
        return JsonResponse({
            "id":         booking.pk,
            "name":       booking.name,
            "phone":      booking.phone,
            "date":       d.strftime("%d.%m.%Y") if d else "—",
            "date_long":  d.strftime("%d %B %Y") if d else "—",
            "time":       booking.get_display_time,
            "comment":    booking.comment,
            "owner_note": booking.owner_note,
            "status":     booking.status,
            "is_past":    (d < date.today()) if d else True,
        })
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
@require_POST
def booking_update(request, booking_id):
    """AJAX: обновляет имя, телефон и заметку инструктора."""
    try:
        booking = get_object_or_404(Booking, pk=booking_id)
        name  = request.POST.get("name",  "").strip()
        phone = request.POST.get("phone", "").strip()
        owner_note = request.POST.get("owner_note", "").strip()
        if not name or not phone:
            return JsonResponse({"success": False, "error": "Имя и телефон обязательны"}, status=400)
        booking.name       = name
        booking.phone      = phone
        booking.owner_note = owner_note
        booking.save(update_fields=["name", "phone", "owner_note"])
        return JsonResponse({
            "success":    True,
            "name":       booking.name,
            "phone":      booking.phone,
            "owner_note": booking.owner_note,
        })
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
@require_POST
def booking_delete(request, booking_id):
    """AJAX: полное удаление записи из БД (освобождает слот)."""
    try:
        booking = get_object_or_404(Booking, pk=booking_id)
        if booking.slot:
            booking.slot.is_available = True
            booking.slot.save(update_fields=["is_available"])
        booking.delete()
        return JsonResponse({"success": True})
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
@require_POST
def complete_booking(request, booking_id):
    """AJAX: помечает запись как завершённую."""
    try:
        booking = get_object_or_404(Booking, pk=booking_id)
        booking.cache_slot_info()
        booking.status = Booking.STATUS_COMPLETED
        booking.save(update_fields=["status", "slot_date", "slot_time_str"])
        return JsonResponse({"success": True, "status": "completed"})
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
def owner_reschedule(request, booking_id):
    """
    Владелец переносит активную запись или восстанавливает отменённую,
    назначая новый слот через тот же calendar-интерфейс.
    """
    booking = get_object_or_404(
        Booking, pk=booking_id,
        status__in=[Booking.STATUS_ACTIVE, Booking.STATUS_CANCELLED],
    )

    if request.method == "POST":
        slot_id = request.POST.get("slot_id")
        try:
            new_slot = TimeSlot.objects.get(pk=int(slot_id), is_available=True)
        except (TimeSlot.DoesNotExist, ValueError):
            messages.error(request, "Выбранный слот недоступен.")
            return render(request, "core/owner_reschedule.html", {"booking": booking})

        if new_slot.is_booked:
            messages.error(request, "Этот слот уже занят.")
            return render(request, "core/owner_reschedule.html", {"booking": booking})

        # Освобождаем старый слот если был
        if booking.slot:
            booking.slot.is_available = True
            booking.slot.save(update_fields=["is_available"])

        booking.slot = new_slot
        booking.cache_slot_info()
        booking.status = Booking.STATUS_ACTIVE
        booking.save(update_fields=["slot", "status", "slot_date", "slot_time_str"])

        verb = "перенесена" if booking.status == Booking.STATUS_ACTIVE else "восстановлена"
        messages.success(request, f"Запись {booking.name} {verb} на {new_slot}.")
        return redirect("all_bookings")

    return render(request, "core/owner_reschedule.html", {"booking": booking})


@login_required
def all_bookings(request):
    """
    Раздел «Все записи» — полный список с поиском
    по имени, телефону, комментарию и дате.
    """
    q = request.GET.get("q", "").strip()
    q_filter = _build_search_filter(q) if q else Q()

    today_date = date.today()
    qs = (
        Booking.objects.filter(q_filter)
        .select_related("slot")
        .order_by("-slot_date", "-slot__start_time")
    )[:200]

    # Помечаем каждую запись флагом is_active_flag для шаблона
    bookings = list(qs)
    for b in bookings:
        d = b.get_display_date
        b.is_active_flag = (
            d is not None
            and d >= today_date
            and b.status == Booking.STATUS_ACTIVE
        )

    context = {
        "bookings": bookings,
        "q": q,
        "total": Booking.objects.count(),
        "today": today_date,
    }
    return render(request, "core/all_bookings.html", context)


@login_required
@require_POST
def add_owner_note(request, booking_id):
    """AJAX: сохраняет заметку инструктора к ученику."""
    booking = get_object_or_404(Booking, pk=booking_id)
    form = OwnerNoteForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"success": False, "error": "Ошибка формы"}, status=400)
    booking.owner_note = form.cleaned_data["note"]
    booking.save(update_fields=["owner_note"])
    return JsonResponse({"success": True, "note": booking.owner_note})


# ─── Вспомогательные функции поиска ──────────────────────────────────────────

def _build_search_filter(q: str) -> "Q":
    """
    Строим Q-фильтр для поиска по имени, телефону, комментарию и дате.
    Поддерживает русские форматы дат: ДД.ММ.ГГГГ, ДД.ММ
    icontains работает без учёта регистра (ILIKE в PostgreSQL).
    """
    import re

    text_filter = (
        Q(name__icontains=q)
        | Q(phone__icontains=q)
        | Q(comment__icontains=q)
        | Q(owner_note__icontains=q)
    )

    # Пытаемся распознать дату формата ДД.ММ.ГГГГ или ДД.ММ
    date_filter = Q()
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?$", q.strip())
    if m:
        try:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else None
            date_filter = Q(slot__date__day=day, slot__date__month=month)
            if year:
                date_filter &= Q(slot__date__year=year)
        except (ValueError, TypeError):
            pass

    return text_filter | date_filter


# ─── История занятий ──────────────────────────────────────────────────────────

@login_required
def booking_history(request):
    """
    История прошедших занятий.
    Поиск по имени ИЛИ телефону (регистронезависимо, один параметр q).
    """
    today = date.today()

    q = request.GET.get("q", "").strip()
    q_filter = _build_search_filter(q) if q else Q()

    # ── Активные: есть слот, дата >= сегодня, статус active
    active_qs = (
        Booking.objects.filter(slot__date__gte=today, status=Booking.STATUS_ACTIVE)
        .filter(q_filter)
        .select_related("slot")
        .order_by("slot__date", "slot__start_time")
    )

    # ── История за 30 дней: completed/cancelled + прошедшие активные
    thirty_days_ago = today - timedelta(days=30)
    date_window = Q(slot_date__gte=thirty_days_ago) | Q(slot__date__gte=thirty_days_ago)
    past_qs = (
        Booking.objects.filter(
            Q(status__in=[Booking.STATUS_COMPLETED, Booking.STATUS_CANCELLED])
            | Q(slot__date__lt=today, status=Booking.STATUS_ACTIVE)
        )
        .filter(date_window)
        .filter(q_filter)
        .select_related("slot")
        .distinct()
        .order_by("-slot_date", "-slot__start_time")
    )

    active_bookings = list(active_qs[:50])
    past_bookings   = list(past_qs[:150])

    context = {
        "active_bookings": active_bookings,
        "past_bookings":   past_bookings,
        "active_count":    len(active_bookings),
        "past_count":      len(past_bookings),
        "total":           len(active_bookings) + len(past_bookings),
        "q":    q,
        "today": today,
    }
    return render(request, "core/booking_history.html", context)


@login_required
@require_GET
def dashboard_month(request):
    """AJAX: слоты на месяц для виджета дашборда."""
    today = date.today()
    try:
        year  = int(request.GET.get("year",  today.year))
        month = int(request.GET.get("month", today.month))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Некорректные параметры"}, status=400)

    slots = TimeSlot.objects.filter(
        date__year=year, date__month=month,
    ).prefetch_related("booking").order_by("date", "start_time")

    result = {}
    for slot in slots:
        d = slot.date.strftime("%Y-%m-%d")
        if d not in result:
            result[d] = []
        result[d].append({
            "id": slot.pk,
            "time": slot.get_time_range(),
            "status": slot.status_class,
            "is_booked": slot.is_booked,
            "booking_name": slot.booking.name if slot.is_booked else None,
        })

    return JsonResponse(result)


# ─── Управление услугами (AJAX) ───────────────────────────────────────────────

@login_required
def services_dashboard(request):
    """Страница управления услугами в личном кабинете."""
    services = Service.objects.order_by("order", "id")
    return render(request, "core/services_dashboard.html", {"services": services})


@login_required
def service_create(request):
    """AJAX POST: создать услугу."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    data = json.loads(request.body)
    service = Service.objects.create(
        emoji=data.get("emoji", "").strip()[:10],
        title=data.get("title", "").strip()[:255],
        description=data.get("description", "").strip(),
        price=data.get("price", "").strip()[:100],
        is_active=bool(data.get("is_active", True)),
        order=int(data.get("order", 0)),
    )
    return JsonResponse({"success": True, "id": service.pk, "service": _service_to_dict(service)})


@login_required
def service_update(request, pk):
    """AJAX POST: обновить услугу."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    service = get_object_or_404(Service, pk=pk)
    data = json.loads(request.body)
    service.emoji       = data.get("emoji", service.emoji).strip()[:10]
    service.title       = data.get("title", service.title).strip()[:255]
    service.description = data.get("description", service.description).strip()
    service.price       = data.get("price", service.price).strip()[:100]
    service.is_active   = bool(data.get("is_active", service.is_active))
    service.order       = int(data.get("order", service.order))
    service.save()
    return JsonResponse({"success": True, "service": _service_to_dict(service)})


@login_required
def service_toggle(request, pk):
    """AJAX POST: переключить активность услуги."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    service = get_object_or_404(Service, pk=pk)
    service.is_active = not service.is_active
    service.save(update_fields=["is_active"])
    return JsonResponse({"success": True, "is_active": service.is_active})


@login_required
def service_delete(request, pk):
    """AJAX POST: удалить услугу."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    service = get_object_or_404(Service, pk=pk)
    service.delete()
    return JsonResponse({"success": True})


@login_required
def service_reorder(request):
    """AJAX POST: сохранить новый порядок услуг.
    Тело: {"order": [id1, id2, id3, ...]}
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    data  = json.loads(request.body)
    order = data.get("order", [])
    for idx, service_id in enumerate(order):
        Service.objects.filter(pk=service_id).update(order=idx)
    return JsonResponse({"success": True})


def _service_to_dict(service):
    return {
        "id":          service.pk,
        "emoji":       service.emoji,
        "title":       service.title,
        "description": service.description,
        "price":       service.price,
        "is_active":   service.is_active,
        "order":       service.order,
    }

"""
Общие константы приложения.
"""
from datetime import time

# Стандартные слоты рабочего дня: 06:00–21:00, шаг 2 часа
ALL_DAY_SLOTS = [
    (time(6,  0), time(8,  0)),
    (time(8,  0), time(10, 0)),
    (time(10, 0), time(12, 0)),
    (time(12, 0), time(14, 0)),
    (time(14, 0), time(16, 0)),
    (time(16, 0), time(18, 0)),
    (time(18, 0), time(20, 0)),
    (time(19, 0), time(21, 0)),
]

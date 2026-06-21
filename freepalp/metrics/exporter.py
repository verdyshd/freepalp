"""
freepalp/metrics/exporter.py
Класс для экспорта метрик в различные форматы с валидацией данных.
"""

import csv
import json
from typing import Dict, List, Union, Optional
from dataclasses import dataclass
from datetime import datetime

@dataclass
class Metric:
    """Класс для представления отдельной метрики."""
    name: str
    value: Union[int, float, str]
    timestamp: Optional[datetime] = None
    tags: Optional[Dict[str, str]] = None

class MetricsExporter:
    """Класс для экспорта метрик в различные форматы."""

    def __init__(self):
        self.metrics: List[Metric] = []
        self._validators = {
            'csv': self._validate_for_csv,
            'json': self._validate_for_json
        }

    def add_metric(self, name: str, value: Union[int, float, str],
                  timestamp: Optional[datetime] = None,
                  tags: Optional[Dict[str, str]] = None) -> None:
        """Добавляет новую метрику."""
        self.metrics.append(Metric(
            name=name,
            value=value,
            timestamp=timestamp or datetime.now(),
            tags=tags
        ))

    def _validate_for_csv(self) -> bool:
        """Валидация данных для CSV экспорта."""
        if not self.metrics:
            raise ValueError("Нет данных для экспорта")

        # Проверка на однородность типов значений
        value_types = {type(m.value) for m in self.metrics}
        if len(value_types) > 1:
            raise ValueError("Все значения метрик должны быть одного типа для CSV экспорта")
        return True

    def _validate_for_json(self) -> bool:
        """Валидация данных для JSON экспорта."""
        if not self.metrics:
            raise ValueError("Нет данных для экспорта")
        return True

    def export(self, format: str = 'csv', filepath: str = 'metrics_export') -> str:
        """
        Экспортирует метрики в указанном формате.

        Args:
            format: Формат экспорта ('csv' или 'json')
            filepath: Базовое имя файла (без расширения)

        Returns:
            Путь к созданному файлу
        """
        if format not in self._validators:
            raise ValueError(f"Неподдерживаемый формат: {format}")

        self._validators[format]()

        if format == 'csv':
            return self._export_to_csv(filepath)
        elif format == 'json':
            return self._export_to_json(filepath)

    def _export_to_csv(self, filepath: str) -> str:
        """Экспорт метрик в CSV формат."""
        full_path = f"{filepath}.csv"

        with open(full_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            # Заголовок
            writer.writerow(['name', 'value', 'timestamp', 'tags'])

            # Данные
            for metric in self.metrics:
                tags_str = json.dumps(metric.tags) if metric.tags else ''
                writer.writerow([
                    metric.name,
                    metric.value,
                    metric.timestamp.isoformat(),
                    tags_str
                ])

        return full_path

    def _export_to_json(self, filepath: str) -> str:
        """Экспорт метрик в JSON формат."""
        full_path = f"{filepath}.json"

        data = [{
            'name': m.name,
            'value': m.value,
            'timestamp': m.timestamp.isoformat(),
            'tags': m.tags
        } for m in self.metrics]

        with open(full_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return full_path

    def clear(self) -> None:
        """Очищает все метрики."""
        self.metrics.clear()
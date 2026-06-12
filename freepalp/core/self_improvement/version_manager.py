"""
VersionManager — безопасное версионирование конфига prompts.json.

Структура:
  freepalp/config/prompts.json          ← активная версия (код читает отсюда)
  freepalp/config/versions/
    active.json                     ← {"version": "1.2.0", "activated_at": "..."}
    v1.0.0/
      prompts.json                  ← снимок конфига
      metadata.json                 ← результаты тестов, изменения, причина
    v1.1.0/
      prompts.json
      metadata.json
    ...

Жизненный цикл версии:
  propose()   → создаёт versions/v{N}/ с новым prompts.json (статус: proposed)
  test()      → запускает test_mvp.py, сохраняет результат в metadata.json
  activate()  → копирует в prompts.json, обновляет active.json (статус: active)
  rollback()  → откатывается к предыдущей версии
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

CONFIG_DIR   = Path(__file__).parent.parent.parent / "config"
PROMPTS_FILE = CONFIG_DIR / "prompts.json"
VERSIONS_DIR = CONFIG_DIR / "versions"
ACTIVE_FILE  = VERSIONS_DIR / "active.json"
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent


class VersionManager:

    def __init__(self):
        VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def current_version(self) -> str:
        """Возвращает номер активной версии."""
        if ACTIVE_FILE.exists():
            return json.loads(ACTIVE_FILE.read_text("utf-8")).get("version", "unknown")
        # Читаем из prompts.json напрямую
        if PROMPTS_FILE.exists():
            return json.loads(PROMPTS_FILE.read_text("utf-8")).get("version", "1.0.0")
        return "1.0.0"

    def list_versions(self) -> list[dict]:
        """Список всех версий с метаданными."""
        versions = []
        for d in sorted(VERSIONS_DIR.iterdir()):
            if d.is_dir() and d.name.startswith("v"):
                meta_file = d / "metadata.json"
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text("utf-8"))
                        versions.append(meta)
                    except Exception:
                        pass
        return versions

    def propose(self, new_config: dict, changes_description: str) -> str:
        """
        Сохраняет предложенную новую версию в versions/v{N}/.
        Возвращает строку версии.
        """
        current = self.current_version()
        new_version = self._bump_version(current)

        version_dir = VERSIONS_DIR / f"v{new_version}"
        version_dir.mkdir(parents=True, exist_ok=True)

        # Записываем новый конфиг
        new_config["version"] = new_version
        new_config["created_at"] = datetime.now().isoformat()
        (version_dir / "prompts.json").write_text(
            json.dumps(new_config, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # Сохраняем метаданные
        metadata = {
            "version": new_version,
            "parent_version": current,
            "proposed_at": datetime.now().isoformat(),
            "changes": changes_description,
            "status": "proposed",
            "test_passed": None,
            "test_output": None,
            "activated_at": None,
        }
        (version_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        return new_version

    def test(self, version: str) -> tuple[bool, str]:
        """
        Запускает test_mvp.py с временно активированным новым конфигом.
        Возвращает (passed, output).
        БЕЗОПАСНО: оригинальный prompts.json восстанавливается в любом случае.
        """
        version_dir = VERSIONS_DIR / f"v{version}"
        if not version_dir.exists():
            return False, f"Версия {version} не найдена"

        new_prompts = version_dir / "prompts.json"
        backup_prompts = CONFIG_DIR / "_prompts_backup.json"

        # Бекапим текущий конфиг
        shutil.copy2(PROMPTS_FILE, backup_prompts)

        passed = False
        output = ""
        try:
            # Временно подменяем конфиг
            shutil.copy2(new_prompts, PROMPTS_FILE)

            # Запускаем тесты (явно utf-8 чтобы не сломаться на Windows cp1251)
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "test_mvp.py")],
                capture_output=True,
                timeout=120,
                cwd=str(PROJECT_ROOT),
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            )
            # Декодируем bytes вручную с fallback
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            output = stdout + stderr
            passed = result.returncode == 0 and "6/6" in output
        except subprocess.TimeoutExpired:
            output = "ERROR: Tests timed out (>120s)"
        except Exception as e:
            output = f"ERROR: {e}"
        finally:
            # ВСЕГДА восстанавливаем оригинал
            shutil.copy2(backup_prompts, PROMPTS_FILE)
            backup_prompts.unlink(missing_ok=True)

            # Обновляем метаданные версии
            self._update_metadata(version, {
                "test_passed": passed,
                "test_output": output[-2000:],  # последние 2000 символов
                "tested_at": datetime.now().isoformat(),
                "status": "tested",
            })

        return passed, output

    def activate(self, version: str) -> bool:
        """
        Активирует версию: копирует prompts.json в config/prompts.json.
        Возвращает True если успешно.
        Предварительно сохраняет текущую версию как backup в versions/.
        """
        version_dir = VERSIONS_DIR / f"v{version}"
        if not version_dir.exists():
            return False

        # Проверяем что тесты прошли
        meta_file = version_dir / "metadata.json"
        if meta_file.exists():
            meta = json.loads(meta_file.read_text("utf-8"))
            if not meta.get("test_passed"):
                return False  # Не активируем непроверенную версию

        # Бекапим текущий конфиг в его версионную папку (если ещё нет)
        current_ver = self.current_version()
        current_dir = VERSIONS_DIR / f"v{current_ver}"
        current_dir.mkdir(parents=True, exist_ok=True)
        if not (current_dir / "prompts.json").exists():
            shutil.copy2(PROMPTS_FILE, current_dir / "prompts.json")

        # Архивируем предыдущую активную версию
        for d in VERSIONS_DIR.iterdir():
            if d.is_dir() and d.name.startswith("v") and d.name != f"v{version}":
                mf = d / "metadata.json"
                if mf.exists():
                    try:
                        meta = json.loads(mf.read_text("utf-8"))
                        if meta.get("status") == "active":
                            meta["status"] = "archived"
                            mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass

        # Активируем новую версию
        shutil.copy2(version_dir / "prompts.json", PROMPTS_FILE)

        # Обновляем active.json
        active_info = {
            "version": version,
            "activated_at": datetime.now().isoformat(),
            "previous_version": current_ver,
        }
        ACTIVE_FILE.write_text(
            json.dumps(active_info, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # Обновляем метаданные версии
        self._update_metadata(version, {
            "activated_at": datetime.now().isoformat(),
            "status": "active",
        })

        # Горячая перезагрузка промптов в памяти
        try:
            from freepalp.core import prompt_loader
            prompt_loader.reload()
            print(f"  [VM] prompt_loader reloaded -> v{version}")
        except Exception as e:
            print(f"  [VM] prompt_loader reload error: {e}")

        return True

    def rollback(self) -> tuple[bool, str]:
        """
        Откатывается к предыдущей версии.
        Возвращает (success, message).
        """
        if not ACTIVE_FILE.exists():
            return False, "Нет информации об активной версии"

        active_info = json.loads(ACTIVE_FILE.read_text("utf-8"))
        prev_version = active_info.get("previous_version")
        if not prev_version:
            return False, "Нет предыдущей версии для отката"

        prev_dir = VERSIONS_DIR / f"v{prev_version}"
        if not prev_dir.exists() or not (prev_dir / "prompts.json").exists():
            return False, f"Файлы версии {prev_version} не найдены"

        current_ver = active_info["version"]
        shutil.copy2(prev_dir / "prompts.json", PROMPTS_FILE)

        active_info["version"] = prev_version
        active_info["previous_version"] = current_ver
        active_info["rolled_back_at"] = datetime.now().isoformat()
        ACTIVE_FILE.write_text(
            json.dumps(active_info, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        try:
            from freepalp.core import prompt_loader
            prompt_loader.reload()
            print(f"  [VM] prompt_loader reloaded after rollback -> v{prev_version}")
        except Exception as e:
            print(f"  [VM] prompt_loader reload error: {e}")

        return True, f"Откат с {current_ver} → {prev_version}"

    def load_version_config(self, version: str) -> Optional[dict]:
        """Загружает prompts.json из конкретной версии."""
        version_file = VERSIONS_DIR / f"v{version}" / "prompts.json"
        if version_file.exists():
            return json.loads(version_file.read_text("utf-8"))
        return None

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------

    def _bump_version(self, version: str) -> str:
        """1.2.3 → 1.2.4"""
        try:
            parts = version.split(".")
            parts[-1] = str(int(parts[-1]) + 1)
            return ".".join(parts)
        except Exception:
            return version + ".1"

    def _update_metadata(self, version: str, updates: dict):
        meta_file = VERSIONS_DIR / f"v{version}" / "metadata.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text("utf-8"))
                meta.update(updates)
                meta_file.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass

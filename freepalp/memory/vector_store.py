"""
VectorStore — лёгкий семантический поиск по памяти FreePalp.

Зачем: старый поиск (search_cold) искал по подстроке — пропускал словоформы
и синонимы. Этот модуль даёт устойчивый к словоформам поиск по смыслу.

Технология (без тяжёлых зависимостей, только numpy):
  - Hashing TF-IDF: каждое слово хешируется в бакет фиксированного размера
  - Char 3-grams: устойчивость к словоформам ("роутинг"/"роутер" близки)
  - Косинусное сходство нормализованных векторов
  - Опционально: Gemini text-embedding-004 если есть ключ (настоящая семантика)

Вдохновлено agentmemory (rohitg00): hybrid retrieval вместо чистого keyword.
"""

from __future__ import annotations
import json
import math
import re
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np

_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-Я0-9_]+", re.UNICODE)
_DEFAULT_DIM = 512


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _char_ngrams(token: str, n: int = 3) -> list[str]:
    """Char n-grams слова для устойчивости к словоформам."""
    s = f"#{token}#"
    if len(s) <= n:
        return [s]
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def _hash_bucket(s: str, dim: int) -> int:
    h = hashlib.md5(s.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % dim


class VectorStore:
    """Семантический индекс над записями памяти.

    Каждая запись: {id, text, layer, meta, vec}.
    layer ∈ {working, episodic, semantic, procedural}.
    """

    def __init__(self, dim: int = _DEFAULT_DIM, path: Optional[Path] = None,
                 use_gemini: bool = False):
        self.dim = dim
        self.path = Path(path) if path else None
        self.use_gemini = use_gemini
        self.entries: list[dict] = []   # {id, text, layer, meta, vec: list[float]}
        self._matrix: Optional[np.ndarray] = None  # кэш матрицы векторов
        if self.path and self.path.exists():
            self.load()

    # ──────────────────────────────────────────────────────────────
    # Эмбеддинг
    # ──────────────────────────────────────────────────────────────

    def _embed_local(self, text: str) -> np.ndarray:
        """Hashing TF-IDF вектор: слова + char 3-grams, L2-нормализация."""
        vec = np.zeros(self.dim, dtype=np.float32)
        toks = _tokens(text)
        if not toks:
            return vec
        # Слова (вес 1.0)
        for t in toks:
            vec[_hash_bucket("w:" + t, self.dim)] += 1.0
            # Char 3-grams (вес 0.5 — мягче, для словоформ)
            for ng in _char_ngrams(t, 3):
                vec[_hash_bucket("g:" + ng, self.dim)] += 0.5
        # Сглаживание частот (log1p) — частые слова не доминируют
        vec = np.log1p(vec)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def _embed(self, text: str) -> np.ndarray:
        """Эмбеддинг с опциональным апгрейдом на Gemini."""
        if self.use_gemini:
            v = self._embed_gemini(text)
            if v is not None:
                return v
        return self._embed_local(text)

    def _embed_gemini(self, text: str) -> Optional[np.ndarray]:
        """Настоящие эмбеддинги через Gemini text-embedding-004 (если есть ключ)."""
        try:
            import os, httpx
            key = os.environ.get("GEMINI_API_KEY", "")
            if not key:
                return None
            url = (f"https://generativelanguage.googleapis.com/v1beta/"
                   f"models/text-embedding-004:embedContent?key={key}")
            r = httpx.post(url, json={
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": text[:2000]}]},
            }, timeout=8.0)
            if r.status_code == 200:
                emb = r.json().get("embedding", {}).get("values")
                if emb:
                    v = np.array(emb, dtype=np.float32)
                    norm = np.linalg.norm(v)
                    return v / norm if norm > 0 else v
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────────────────────
    # CRUD
    # ──────────────────────────────────────────────────────────────

    def add(self, text: str, layer: str = "semantic", meta: Optional[dict] = None) -> str:
        """Добавляет запись. Дедуплицирует по точному совпадению текста."""
        text = (text or "").strip()
        if not text:
            return ""
        for e in self.entries:
            if e["text"] == text:
                return e["id"]   # уже есть
        vec = self._embed(text)
        entry_id = hashlib.md5(f"{text}{datetime.now().isoformat()}".encode()).hexdigest()[:12]
        self.entries.append({
            "id": entry_id,
            "text": text,
            "layer": layer,
            "meta": meta or {},
            "vec": vec.tolist(),
            "ts": datetime.now().isoformat(),
        })
        self._matrix = None   # инвалидируем кэш
        return entry_id

    # Порог: при стольких записях имеет смысл HNSW-индекс (если установлен)
    _HNSW_THRESHOLD = 2000

    def _try_hnsw_search(self, qv: np.ndarray, k: int) -> Optional[list[int]]:
        """Быстрый ANN-поиск через hnswlib если установлен и записей много.
        Возвращает индексы кандидатов или None (тогда — брют-форс)."""
        if len(self.entries) < self._HNSW_THRESHOLD:
            return None
        try:
            import hnswlib
        except ImportError:
            return None
        try:
            idx = getattr(self, "_hnsw_idx", None)
            if idx is None or getattr(self, "_hnsw_n", 0) != len(self.entries):
                # (Пере)строим индекс
                idx = hnswlib.Index(space="cosine", dim=self.dim)
                idx.init_index(max_elements=len(self.entries), ef_construction=200, M=16)
                idx.add_items(self._get_matrix(), np.arange(len(self.entries)))
                idx.set_ef(max(k * 4, 50))
                self._hnsw_idx = idx
                self._hnsw_n = len(self.entries)
            labels, _ = idx.knn_query(qv, k=min(k * 3, len(self.entries)))
            return labels[0].tolist()
        except Exception:
            return None

    def search(self, query: str, k: int = 5, layer: Optional[str] = None,
               min_score: float = 0.05) -> list[dict]:
        """Возвращает top-k записей по косинусному сходству.
        При больших объёмах (>2000 записей) и наличии hnswlib — ANN-поиск."""
        if not self.entries:
            return []
        qv = self._embed(query)
        if np.linalg.norm(qv) == 0:
            return []

        # Фильтр по слою
        idxs = [i for i, e in enumerate(self.entries)
                if layer is None or e["layer"] == layer]
        if not idxs:
            return []

        # HNSW кандидаты (если доступно и объём большой) — иначе все idxs
        if layer is None:
            hnsw_cands = self._try_hnsw_search(qv, k)
            if hnsw_cands is not None:
                idxs = hnsw_cands

        mat = self._get_matrix()[idxs]      # (n, dim) уже нормализованы
        scores = mat @ qv                    # косинус (всё нормализовано)

        ranked = sorted(zip(idxs, scores), key=lambda x: -x[1])
        results = []
        for i, sc in ranked[:k]:
            if sc < min_score:
                continue
            e = self.entries[i]
            results.append({
                "id": e["id"], "text": e["text"], "layer": e["layer"],
                "meta": e["meta"], "score": round(float(sc), 4),
            })
        return results

    def _get_matrix(self) -> np.ndarray:
        if self._matrix is None:
            self._matrix = np.array([e["vec"] for e in self.entries], dtype=np.float32)
        return self._matrix

    def remove(self, entry_id: str) -> bool:
        n = len(self.entries)
        self.entries = [e for e in self.entries if e["id"] != entry_id]
        if len(self.entries) != n:
            self._matrix = None
            return True
        return False

    def count(self, layer: Optional[str] = None) -> int:
        if layer is None:
            return len(self.entries)
        return sum(1 for e in self.entries if e["layer"] == layer)

    # ──────────────────────────────────────────────────────────────
    # Персистентность
    # ──────────────────────────────────────────────────────────────

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "dim": self.dim,
            "use_gemini": self.use_gemini,
            "entries": self.entries,
        }, ensure_ascii=False), encoding="utf-8")

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.dim = data.get("dim", self.dim)
            self.entries = data.get("entries", [])
            self._matrix = None
        except Exception:
            self.entries = []

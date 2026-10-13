"""Base de conocimiento vectorial (Chroma) sobre fichas de proveedor y
catálogo del fabricante, y recuperación para el Agente 2.

Chunking: estructural, una sección por producto (## Producto ...). Una ficha
técnica ya viene organizada así; cortar por tokens partiría la tabla de un
SKU en dos y el agente podría leer el peso de un producto y las dimensiones
de otro.

Embeddings (cadena con fallback):
  1. EMBEDDINGS=chroma (default si hay red): all-MiniLM-L6-v2 vía ONNX, local.
  2. Si no se puede descargar el modelo (sin red, proxy): embedding local por
     hashing de palabras y trigramas de caracteres. No es semántico, pero es
     determinístico y alcanza porque las consultas incluyen código y nombre
     del producto. Los tests usan esta ruta para ser reproducibles.

Grounding: un chunk recuperado solo se acepta como evidencia si menciona el
código del SKU. La similitud vectorial encuentra candidatos; la verificación
de identidad evita citar la ficha del producto equivocado.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from .config import DATA_DIR, STATE_DIR, Umbrales
from .datos import a_float
from .observabilidad import METRICAS, log

DIM = 512


class EmbeddingHash(EmbeddingFunction):
    """Bolsa de palabras + trigramas de caracteres -> vector de 512 dims (L2)."""

    def __init__(self) -> None:  # chroma pide __init__ explícito
        pass

    @staticmethod
    def name() -> str:
        return "hash-local-512"

    def get_config(self) -> dict:
        return {"dim": DIM}

    @staticmethod
    def build_from_config(config: dict) -> "EmbeddingHash":
        return EmbeddingHash()

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for texto in input:
            v = [0.0] * DIM
            t = texto.lower()
            tokens = re.findall(r"[a-záéíóúñ0-9\-]+", t)
            feats = tokens + [t[i:i + 3] for i in range(len(t) - 2)]
            for f in feats:
                h = int(hashlib.md5(f.encode()).hexdigest(), 16)
                v[h % DIM] += 1.0 if (h >> 9) & 1 else -1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def funcion_embedding() -> tuple[EmbeddingFunction, str]:
    if os.getenv("EMBEDDINGS", "chroma") == "chroma":
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            ef = DefaultEmbeddingFunction()
            ef(["prueba"])  # fuerza la descarga ahora, no en medio de la corrida
            return ef, "all-MiniLM-L6-v2"
        except Exception as e:  # noqa: BLE001
            METRICAS.inc("fallback_embeddings")
            log("WARNING", "modelo de embeddings no disponible, uso hashing local", error=type(e).__name__)
    return EmbeddingHash(), "hash-local-512"


@dataclass
class Chunk:
    id: str
    texto: str
    fuente: str  # ficha_proveedor | catalogo_fabricante
    archivo: str


def chunkear(md: str, fuente: str, archivo: str) -> list[Chunk]:
    """Una sección por producto. El encabezado del documento (proveedor) se
    repite en cada chunk para no perder contexto al recuperarlo solo."""
    lineas = md.splitlines()
    cabecera = next((l.lstrip("# ").strip() for l in lineas if l.startswith("# ")), archivo)
    secciones, actual = [], []
    for l in lineas:
        if l.startswith("## ") and actual:
            secciones.append(actual)
            actual = []
        if l.startswith("## ") or actual:
            actual.append(l)
    if actual:
        secciones.append(actual)
    if not secciones:  # documento de un solo producto (catálogo del fabricante)
        secciones = [[l for l in lineas if not l.startswith("<!--")]]
    chunks = []
    for i, s in enumerate(secciones):
        texto = f"[{cabecera}]\n" + "\n".join(s).strip()
        chunks.append(Chunk(f"{archivo}#{i}", texto, fuente, archivo))
    return chunks


class BaseConocimiento:
    def __init__(self, persistir: bool = True, nombre: str = "fichas_tecnicas"):
        self.ef, self.modelo_embedding = funcion_embedding()
        if persistir:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            self.cliente = chromadb.PersistentClient(path=str(STATE_DIR / "chroma"))
        else:
            self.cliente = chromadb.EphemeralClient()
        nombre = f"{nombre}_{self.modelo_embedding.replace('-', '_')}"
        self.col = self.cliente.get_or_create_collection(nombre, embedding_function=self.ef,
                                                         metadata={"hnsw:space": "cosine"})

    def indexar(self, parsed_dir: Path | None = None, fabricante_dir: Path | None = None) -> int:
        parsed_dir = parsed_dir or DATA_DIR / "parsed"
        fabricante_dir = fabricante_dir or DATA_DIR / "fuentes" / "catalogo_fabricante"
        chunks: list[Chunk] = []
        for p in sorted(parsed_dir.glob("*.md")):
            chunks += chunkear(p.read_text(encoding="utf-8"), "ficha_proveedor", p.name)
        for p in sorted(fabricante_dir.glob("*.md")):
            chunks += chunkear(p.read_text(encoding="utf-8"), "catalogo_fabricante", p.name)
        if chunks:  # upsert: re-indexar es idempotente
            self.col.upsert(ids=[c.id for c in chunks], documents=[c.texto for c in chunks],
                            metadatas=[{"fuente": c.fuente, "archivo": c.archivo} for c in chunks])
        METRICAS.inc("chunks_indexados", len(chunks))
        log("INFO", "base vectorial indexada", chunks=len(chunks), modelo_embedding=self.modelo_embedding)
        return len(chunks)

    def indexar_textos(self, docs: list[tuple[str, str, str]]) -> int:
        """Indexa documentos ya limpios: (texto, fuente, archivo). Lo usa la
        variante healthcare (prospectos y reclamos ya redactados)."""
        chunks: list[Chunk] = []
        for texto, fuente, archivo in docs:
            chunks += chunkear(texto, fuente, archivo)
        if chunks:
            self.col.upsert(ids=[c.id for c in chunks], documents=[c.texto for c in chunks],
                            metadatas=[{"fuente": c.fuente, "archivo": c.archivo} for c in chunks])
        METRICAS.inc("chunks_indexados", len(chunks))
        return len(chunks)

    def buscar(self, consulta: str, sku_id: str, u: Umbrales) -> list[dict]:
        """Búsqueda híbrida: filtro por palabra clave exacta (el código del
        SKU) + ranking por similitud vectorial dentro de ese subconjunto. Si
        el filtro no devuelve nada, se hace la búsqueda puramente semántica
        y la verificación de identidad descarta lo que no corresponda."""
        with METRICAS.cronometro("rag_busqueda"):
            n = max(1, min(u.top_k, self.col.count()))
            r = self.col.query(query_texts=[consulta], n_results=n, where_document={"$contains": sku_id})
            if not r["ids"][0]:
                r = self.col.query(query_texts=[consulta], n_results=n)
        hits = []
        for doc, meta, dist, cid in zip(r["documents"][0], r["metadatas"][0], r["distances"][0], r["ids"][0]):
            sim = 1 - dist
            identidad = sku_id in doc
            hits.append({"id": cid, "texto": doc, "fuente": meta["fuente"], "archivo": meta["archivo"],
                         "similitud": round(sim, 3), "menciona_sku": identidad})
        aceptados = [h for h in hits if h["menciona_sku"] and h["similitud"] >= u.similitud_min]
        METRICAS.inc("rag_chunks_descartados_identidad", sum(1 for h in hits if not h["menciona_sku"]))
        return aceptados


# ---------- extracción de valores desde el chunk ----------
_PESO = re.compile(r"peso bruto[^|]*\|\s*([\d.,]+)\s*(kg|g)", re.I)
_DIMS = re.compile(r"dimensiones[^|]*\|\s*([\d.,]+)\s*x\s*([\d.,]+)\s*x\s*([\d.,]+)\s*cm", re.I)
_NETO = re.compile(r"contenido neto\s*\|\s*([\d.,]+)\s*(kg|g)", re.I)

ESQUEMA_EXTRACCION = {
    "type": "object",
    "properties": {
        "peso_kg": {"type": ["number", "null"]},
        "largo_cm": {"type": ["number", "null"]},
        "alto_cm": {"type": ["number", "null"]},
        "ancho_cm": {"type": ["number", "null"]},
        "net_quantity_g": {"type": ["number", "null"]},
    },
    "required": ["peso_kg", "largo_cm", "alto_cm", "ancho_cm", "net_quantity_g"],
}


def extraer_valores(texto: str) -> dict:
    """Determinístico (regex sobre la tabla markdown). El chunk ya viene con
    estructura fija, así que no hace falta gastar una llamada al modelo; si
    el formato no se reconoce, devuelve None en ese campo y el SKU termina en
    'sin_fuente_confiable' en vez de inventar."""
    out = {k: None for k in ESQUEMA_EXTRACCION["properties"]}
    if m := _PESO.search(texto):
        v = a_float(m.group(1))
        out["peso_kg"] = v if m.group(2).lower() == "kg" else v / 1000
    if m := _DIMS.search(texto):
        # orden de la ficha: L x A x H = largo x ancho x alto
        out["largo_cm"], out["ancho_cm"], out["alto_cm"] = (a_float(x) for x in m.groups())
    if m := _NETO.search(texto):
        v = a_float(m.group(1))
        out["net_quantity_g"] = v * 1000 if m.group(2).lower() == "kg" else v
    return out

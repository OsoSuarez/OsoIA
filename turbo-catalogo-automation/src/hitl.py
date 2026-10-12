"""Human-in-the-Loop: cola de aprobación, registro de decisiones, políticas
de espera y escalamiento, notificaciones y escritura en el catálogo.

Políticas de espera (Pre-entrega 3, Módulo 4 del curso):
- Corrección condicional sin respuesta en 48 h -> se ESCALA al líder. Nunca
  se auto-aprueba: un valor sin firma no se escribe.
- Bloqueo propuesto de un SKU activo -> no vence nunca. Se re-notifica y
  escala, pero el SKU sigue vendiéndose hasta que una persona decida (bloquear
  por timeout sería exactamente la venta perdida que se quiere evitar).
- 'sin_fuente_confiable' que se repite 2 semanas seguidas -> escala al líder.

Escritura en el catálogo: modo dry-run por defecto (escribe en reports/),
modo real con PATCH idempotente a la API interna del catálogo.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import REPORTS_DIR, STATE_DIR, Umbrales
from .observabilidad import METRICAS, log
from .resiliencia import ErrorPermanente, ErrorTransitorio, con_fallback, reintentar

DECISIONES = ("aprobado", "rechazado", "pedido_mas_evidencia")


def ahora() -> datetime:
    return datetime.now(timezone.utc)


class ColaHITL:
    def __init__(self, path: Path | None = None, silenciosa: bool = False):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.silenciosa = silenciosa  # evals/tests: sin notificaciones
        self.path = path or STATE_DIR / "hitl.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS cola (
            sku_id TEXT, campo TEXT, estado TEXT, accion TEXT, propuesta TEXT,
            creado TEXT, vence TEXT, escalado INTEGER DEFAULT 0, resuelto INTEGER DEFAULT 0,
            semanas_sin_fuente INTEGER DEFAULT 0,
            PRIMARY KEY (sku_id, campo));
        CREATE TABLE IF NOT EXISTS registro_aprobaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sku_id TEXT, campo TEXT, ts TEXT,
            aprobador TEXT, rol TEXT, fuente_revisada TEXT, desvio_revisado REAL,
            justificacion TEXT, resultado TEXT, accion_original TEXT, valor_aplicado REAL);
        """)

    def encolar(self, res: dict, u: Umbrales) -> None:
        """Idempotente por (sku, campo): si el SKU ya estaba en la cola se
        actualiza la propuesta pero se conserva la fecha original (la espera no
        se reinicia cada semana)."""
        vence = None if res["estado"] == "bloqueado_pendiente_aprobacion" and not u.hitl_bloqueo_expira \
            else (ahora() + timedelta(hours=u.hitl_timeout_horas_correccion)).isoformat()
        prev = self.db.execute("SELECT * FROM cola WHERE sku_id=? AND campo=? AND resuelto=0",
                               (res["sku_id"], res["campo"])).fetchone()
        semanas = (prev["semanas_sin_fuente"] + 1) if prev and res["estado"] == "sin_fuente_confiable" else \
            (1 if res["estado"] == "sin_fuente_confiable" else 0)
        self.db.execute("""INSERT INTO cola (sku_id,campo,estado,accion,propuesta,creado,vence,semanas_sin_fuente)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(sku_id,campo) DO UPDATE SET estado=excluded.estado, accion=excluded.accion,
              propuesta=excluded.propuesta, semanas_sin_fuente=excluded.semanas_sin_fuente, resuelto=0""",
                        (res["sku_id"], res["campo"], res["estado"], res["accion_propuesta"],
                         json.dumps(res, ensure_ascii=False, default=str), ahora().isoformat(), vence, semanas))
        self.db.commit()
        METRICAS.inc("hitl_encolados")
        if self.silenciosa:
            pass
        elif semanas >= 2:
            notificar(f"[ESCALADO] {res['sku_id']} lleva {semanas} semanas sin fuente confiable", "lider_catalogo")
            self.db.execute("UPDATE cola SET escalado=1 WHERE sku_id=? AND campo=?", (res["sku_id"], res["campo"]))
            self.db.commit()
        else:
            notificar(f"{res['sku_id']} ({res['campo']}): {res['estado']} — {res['motivo']}", "analista_catalogo")

    def pendientes(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM cola WHERE resuelto=0 ORDER BY escalado DESC, creado").fetchall()
        return [dict(r) | {"propuesta": json.loads(r["propuesta"])} for r in rows]

    def revisar_vencimientos(self, momento: datetime | None = None) -> list[str]:
        momento = momento or ahora()
        escalados = []
        for r in self.db.execute("SELECT * FROM cola WHERE resuelto=0 AND escalado=0 AND vence IS NOT NULL"):
            if datetime.fromisoformat(r["vence"]) <= momento:
                escalados.append(r["sku_id"])
        for sku in escalados:
            self.db.execute("UPDATE cola SET escalado=1 WHERE sku_id=? AND resuelto=0", (sku,))
            notificar(f"[ESCALADO] {sku}: sin respuesta del analista en el plazo; no se aplica nada sin firma",
                      "lider_catalogo")
            METRICAS.inc("hitl_escalados_por_timeout")
            log("WARNING", "aprobación vencida, escalada al líder", sku_id=sku)
        self.db.commit()
        return escalados

    def decidir(self, sku_id: str, campo: str, decision: str, aprobador: str, rol: str,
                justificacion: str, valor_elegido: float | None = None) -> dict:
        if decision not in DECISIONES:
            raise ValueError(f"decisión inválida: {decision}")
        if not justificacion or len(justificacion.strip()) < 5:
            raise ValueError("la justificación es obligatoria")
        if rol not in ("analista_catalogo", "lider_catalogo"):
            raise PermissionError("solo analista o líder de Catálogo pueden decidir")
        row = self.db.execute("SELECT * FROM cola WHERE sku_id=? AND campo=? AND resuelto=0",
                              (sku_id, campo)).fetchone()
        if row is None:
            raise KeyError(f"{sku_id}/{campo} no está pendiente")
        prop = json.loads(row["propuesta"])
        if prop["estado"] == "bloqueado_pendiente_aprobacion" and decision == "aprobado" and rol != "lider_catalogo" \
                and prop["rotacion"] == "alta":
            raise PermissionError("bloquear un SKU de alta rotación requiere al líder")
        valor = valor_elegido if valor_elegido is not None else prop.get("valor_propuesto")
        if prop["estado"] == "fuentes_contradictorias" and decision == "aprobado" and valor_elegido is None:
            raise ValueError("con fuentes contradictorias hay que elegir explícitamente el valor")
        aplicado = None
        if decision == "aprobado":
            if prop["accion_propuesta"] == "bloquear_pendiente_aprobacion":
                ESCRITOR.bloquear(sku_id, motivo=justificacion)
            elif valor is not None:
                ESCRITOR.corregir(sku_id, campo, prop["valor_actual"], valor, fuente=f"aprobación {rol}",
                                  version=prop.get("umbrales_version"))
                aplicado = valor
        fuente_rev = ", ".join(e["cita"] for e in prop.get("evidencias", [])) or "sin fuente"
        self.db.execute("""INSERT INTO registro_aprobaciones (sku_id,campo,ts,aprobador,rol,fuente_revisada,
            desvio_revisado,justificacion,resultado,accion_original,valor_aplicado) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (sku_id, campo, ahora().isoformat(), aprobador, rol, fuente_rev,
                         prop["desvio_vs_rango_pct"], justificacion, decision, prop["accion_propuesta"], aplicado))
        if decision != "pedido_mas_evidencia":
            self.db.execute("UPDATE cola SET resuelto=1 WHERE sku_id=? AND campo=?", (sku_id, campo))
        self.db.commit()
        METRICAS.inc(f"hitl_decision_{decision}")
        log("INFO", "decisión humana registrada", sku_id=sku_id, campo=campo, decision=decision, rol=rol,
            accion_original=prop["accion_propuesta"], valor_aplicado=aplicado)
        return {"sku_id": sku_id, "campo": campo, "resultado": decision, "valor_aplicado": aplicado}

    def registro(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM registro_aprobaciones ORDER BY id")]


# ---------------- notificaciones (webhook saliente) ----------------
@reintentar(intentos=3, base_s=0.5, max_s=4.0, servicio="slack")
def _post_slack(texto: str) -> None:
    import httpx

    try:
        r = httpx.post(os.environ["SLACK_WEBHOOK_URL"], json={"text": texto}, timeout=5)
    except httpx.HTTPError as e:
        raise ErrorTransitorio(str(e)) from e
    if r.status_code == 429 or r.status_code >= 500:
        raise ErrorTransitorio(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        raise ErrorPermanente(f"HTTP {r.status_code}")


def notificar(texto: str, destinatario: str) -> str:
    """Slack si hay webhook configurado; si no (o si Slack falla), queda en el
    log y en reports/notificaciones.log para que nadie pierda el aviso."""
    def archivar():
        REPORTS_DIR.mkdir(exist_ok=True)
        with open(REPORTS_DIR / "notificaciones.log", "a", encoding="utf-8") as f:
            f.write(f"{ahora().isoformat()} [{destinatario}] {texto}\n")
        return "archivo"

    if os.getenv("SLACK_WEBHOOK_URL"):
        _, ruta = con_fallback(lambda: _post_slack(f"@{destinatario} {texto}"), archivar, "notificacion")
    else:
        archivar()
        ruta = "archivo"
    METRICAS.inc("notificaciones")
    return ruta


# ---------------- escritura en el catálogo ----------------
class EscritorCatalogo:
    """Única puerta de escritura. Acciones prohibidas (precio, tabla de
    rangos, borrar histórico) no existen como método: no hay forma de que
    el agente las ejecute."""

    def __init__(self) -> None:
        self.dry_run = os.getenv("DRY_RUN", "1") == "1"

    def _registrar(self, fila: dict) -> None:
        REPORTS_DIR.mkdir(exist_ok=True)
        p = REPORTS_DIR / "correcciones_aplicadas.csv"
        nuevo = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(fila))
            if nuevo:
                w.writeheader()
            w.writerow(fila)

    @reintentar(intentos=3, base_s=0.5, max_s=4.0, servicio="catalogo_api")
    def _patch(self, sku_id: str, cuerpo: dict) -> None:
        import httpx

        try:
            r = httpx.patch(f"{os.environ['CATALOGO_API_URL']}/skus/{sku_id}", json=cuerpo, timeout=10,
                            headers={"Authorization": f"Bearer {os.environ['CATALOGO_API_TOKEN']}",
                                     "Idempotency-Key": f"{sku_id}-{cuerpo.get('campo')}-{cuerpo.get('valor')}"})
        except httpx.HTTPError as e:
            raise ErrorTransitorio(str(e)) from e
        if r.status_code >= 500 or r.status_code == 429:
            raise ErrorTransitorio(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ErrorPermanente(f"HTTP {r.status_code}: {r.text[:200]}")

    def corregir(self, sku_id: str, campo: str, valor_anterior: float, valor: float, fuente: str,
                 version: str | None) -> None:
        fila = {"ts": ahora().isoformat(), "sku_id": sku_id, "campo": campo, "valor_anterior": valor_anterior,
                "valor_nuevo": valor, "fuente": fuente, "umbrales_version": version,
                "modo": "dry_run" if self.dry_run else "api"}
        if not self.dry_run:
            self._patch(sku_id, {"campo": campo, "valor": valor, "fuente": fuente})
        self._registrar(fila)
        METRICAS.inc("correcciones_escritas")
        log("INFO", "corrección escrita", **fila)

    def bloquear(self, sku_id: str, motivo: str) -> None:
        fila = {"ts": ahora().isoformat(), "sku_id": sku_id, "campo": "ACTIVO", "valor_anterior": True,
                "valor_nuevo": False, "fuente": f"bloqueo aprobado: {motivo}", "umbrales_version": None,
                "modo": "dry_run" if self.dry_run else "api"}
        if not self.dry_run:
            self._patch(sku_id, {"campo": "activo", "valor": False, "fuente": motivo})
        self._registrar(fila)
        METRICAS.inc("skus_bloqueados")
        log("WARNING", "SKU bloqueado con aprobación humana", sku_id=sku_id, motivo=motivo)


ESCRITOR = EscritorCatalogo()

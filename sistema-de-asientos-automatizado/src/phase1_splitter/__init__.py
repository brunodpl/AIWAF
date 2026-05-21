"""Fase 1 — splitter de PDFs multi-factura.

Detecta y separa PDFs que contienen varias facturas distintas en uno solo
(caso típico: escaneo en lote desde un MFP con ADF). Se ejecuta como paso
previo a Fase 2 OCR; cada PDF de salida sigue siendo "1 factura = 1 doc"
para el resto del pipeline.
"""

from .main import run_split, split_single_file, SplitOutcome

__all__ = ["run_split", "split_single_file", "SplitOutcome"]

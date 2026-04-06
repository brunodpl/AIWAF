import { InvoiceDocument } from "./types";

export const SAMPLE_INVOICES: InvoiceDocument[] = [
  {
    id: "AS-500",
    status: "pending",
    imageUrl: "https://www.facturas.net/wp-content/uploads/2016/06/ejemplo-factura-completa-1-724x1024.jpg",
    fields: [
      // Grupo 1: Identificación del Emisor
      {
        id: "nif_entidad",
        label: "NIF Entidad",
        value: "49915950Q",
        status: "auto",
        confidence: 100,
        group: 1,
      },
      {
        id: "nombre_entidad",
        label: "Nombre Entidad",
        value: "NOVADELTA... S.L.",
        status: "auto",
        confidence: 99,
        group: 1,
      },
      // Grupo 2: Datos de la Factura
      {
        id: "numero_factura",
        label: "Nº Factura",
        value: "FT 30156/0197",
        status: "block",
        confidence: 45,
        reason: "Formato no estándar detectado en el OCR",
        group: 2,
        editable: true,
      },
      {
        id: "fecha_expedicion",
        label: "Fecha Expedición",
        value: "2026-02-16",
        status: "auto",
        confidence: 98,
        group: 2,
      },
      {
        id: "fecha_operacion",
        label: "Fecha Operación",
        value: "2026-02-14",
        status: "auto",
        confidence: 95,
        group: 2,
      },
      // Grupo 3 handled separately as lines
      // Grupo 4: Clasificación Contable
      {
        id: "concepto",
        label: "Concepto",
        value: "Mercaderías",
        status: "warn",
        confidence: 70,
        reason: "Clasificación ambigua entre 600 y 601",
        group: 4,
        editable: true,
      },
      {
        id: "cuenta_contable",
        label: "Cuenta PGC",
        value: "60000000",
        status: "auto",
        confidence: 92,
        group: 4,
      },
      // Grupo 5: Receptor/Cliente
      {
        id: "nif_receptor",
        label: "NIF Receptor",
        value: "B12345678",
        status: "auto",
        confidence: 100,
        group: 5,
      },
      {
        id: "nombre_receptor",
        label: "Nombre Receptor",
        value: "SUMINISTROS GLOBALES S.L.",
        status: "auto",
        confidence: 100,
        group: 5,
      },
    ],
    fiscalLines: [
      {
        id: "line_1",
        base: 1000.0,
        vatRate: 21,
        vatAmount: 210.0,
        total: 1210.0,
      },
      {
        id: "line_2",
        base: 50.0,
        vatRate: 10,
        vatAmount: 5.0,
        total: 55.0,
      },
    ],
  },
  {
    id: "AS-501",
    status: "pending",
    imageUrl: "https://facturaoficial.com/wp-content/uploads/2021/04/Ejemplo-Factura-Completa.png",
    fields: [
      {
        id: "nif_entidad",
        label: "NIF Entidad",
        value: "A28000000",
        status: "auto",
        confidence: 100,
        group: 1,
      },
      {
        id: "nombre_entidad",
        label: "Nombre Entidad",
        value: "TELEFONICA DE ESPAÑA S.A.U.",
        status: "auto",
        confidence: 99,
        group: 1,
      },
    ],
    fiscalLines: [],
  }
];

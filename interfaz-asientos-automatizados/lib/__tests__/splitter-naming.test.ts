import { describe, it, expect } from "vitest";
import { prettifyBatchFile } from "../splitter-naming";
import type { BatchFileEntry } from "../types";

function entry(overrides: Partial<BatchFileEntry> = {}): BatchFileEntry {
  return {
    doc_id: "doc",
    filename: "doc.pdf",
    status: "pending",
    ...overrides,
  };
}

describe("prettifyBatchFile", () => {
  it("formatea facturas del splitter como 'X.pdf — factura N de M'", () => {
    expect(
      prettifyBatchFile(entry({
        filename: "1_2_3_merged__1of3.pdf",
        split_origin: "1_2_3_merged.pdf",
        split_index: 1,
        split_total: 3,
      })),
    ).toBe("1_2_3_merged.pdf — factura 1 de 3");
  });

  it("devuelve el filename crudo cuando no hay metadata de split", () => {
    expect(prettifyBatchFile(entry({ filename: "factura_001.pdf" })))
      .toBe("factura_001.pdf");
  });

  it("devuelve el filename si split_origin existe pero faltan índices", () => {
    expect(prettifyBatchFile(entry({
      filename: "doc__1of3.pdf",
      split_origin: "doc.pdf",
      split_index: null,
      split_total: 3,
    }))).toBe("doc__1of3.pdf");
  });
});

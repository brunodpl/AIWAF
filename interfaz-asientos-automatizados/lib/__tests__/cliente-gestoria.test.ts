import { describe, it, expect } from "vitest";
import { resolveClienteGestoria } from "../cliente-gestoria";

describe("resolveClienteGestoria", () => {
  it("ingresos → cliente = entidad (issuer of an issued invoice)", () => {
    expect(resolveClienteGestoria("ingresos", {
      nif_entidad: "B111", nombre_entidad: "ACME",
      nif_receptor: "B999", nombre_receptor: "Final customer",
    })).toEqual({ nif: "B111", nombre: "ACME" });
  });

  it("gastos → cliente = receptor (the one who receives a purchase invoice)", () => {
    expect(resolveClienteGestoria("gastos", {
      nif_entidad: "B999", nombre_entidad: "Supplier",
      nif_receptor: "B111", nombre_receptor: "ACME",
    })).toEqual({ nif: "B111", nombre: "ACME" });
  });

  it("bienes → cliente = receptor", () => {
    expect(resolveClienteGestoria("bienes", {
      nif_receptor: "B111", nombre_receptor: "ACME",
    })).toEqual({ nif: "B111", nombre: "ACME" });
  });

  it("reactive: changing nif_receptor in gastos changes the result", () => {
    const fd1 = { nif_receptor: "B111", nombre_receptor: "ACME" };
    const fd2 = { ...fd1, nif_receptor: "B222", nombre_receptor: "BETA" };
    expect(resolveClienteGestoria("gastos", fd1).nif).toBe("B111");
    expect(resolveClienteGestoria("gastos", fd2).nif).toBe("B222");
  });

  it("undefined libro → fallback to receptor (compras-like)", () => {
    expect(resolveClienteGestoria(undefined, {
      nif_receptor: "B111", nombre_receptor: "ACME",
    })).toEqual({ nif: "B111", nombre: "ACME" });
  });

  it("empty fields → returns empty strings", () => {
    expect(resolveClienteGestoria("gastos", {})).toEqual({ nif: "", nombre: "" });
  });

  it("trims whitespace from values", () => {
    expect(resolveClienteGestoria("gastos", {
      nif_receptor: "  B111  ", nombre_receptor: "  ACME  ",
    })).toEqual({ nif: "B111", nombre: "ACME" });
  });
});

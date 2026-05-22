import { describe, it, expect } from "vitest";
import { inferLibroFromCuenta } from "../libro-inference";
import { libroLabel } from "../libros";

describe("inferLibroFromCuenta", () => {
  describe("cuentas del maestro", () => {
    it("700 → ingresos (ventas de mercaderías)", () => {
      expect(inferLibroFromCuenta("700")).toBe("ingresos");
    });

    it("705 → ingresos (prestación de servicios)", () => {
      expect(inferLibroFromCuenta("705")).toBe("ingresos");
    });

    it("705.01 → ingresos (cuenta con punto en maestro)", () => {
      expect(inferLibroFromCuenta("705.01")).toBe("ingresos");
    });

    it("600 → gastos (compras de mercaderías)", () => {
      expect(inferLibroFromCuenta("600")).toBe("gastos");
    });

    it("640 → gastos (sueldos, grupo 6 aunque no sea compra)", () => {
      expect(inferLibroFromCuenta("640")).toBe("gastos");
    });

    it("212 → bienes (instalaciones técnicas)", () => {
      expect(inferLibroFromCuenta("212")).toBe("bienes");
    });

    it("218 → bienes (elementos de transporte)", () => {
      expect(inferLibroFromCuenta("218")).toBe("bienes");
    });
  });

  describe("cuentas custom (por prefijo)", () => {
    it("705.99 (no en maestro) → ingresos por prefijo 7", () => {
      expect(inferLibroFromCuenta("705.99")).toBe("ingresos");
    });

    it("699 (no en maestro) → gastos por prefijo 6", () => {
      expect(inferLibroFromCuenta("699")).toBe("gastos");
    });

    it("215 (no en maestro) → bienes por prefijo 2", () => {
      expect(inferLibroFromCuenta("215")).toBe("bienes");
    });
  });

  describe("cuentas ambiguas / inválidas", () => {
    it("572 (tesorería, prefijo 5) → null", () => {
      expect(inferLibroFromCuenta("572")).toBeNull();
    });

    it("4xx (proveedores/clientes, prefijo 4) → null", () => {
      expect(inferLibroFromCuenta("4xx")).toBeNull();
    });

    it("abc (no numérica) → null", () => {
      expect(inferLibroFromCuenta("abc")).toBeNull();
    });

    it("1 (prefijo no contemplado) → null", () => {
      expect(inferLibroFromCuenta("1")).toBeNull();
    });
  });

  describe("entradas vacías", () => {
    it("cadena vacía → null", () => {
      expect(inferLibroFromCuenta("")).toBeNull();
    });

    it("solo whitespace → null", () => {
      expect(inferLibroFromCuenta("   ")).toBeNull();
    });

    it("null → null", () => {
      expect(inferLibroFromCuenta(null)).toBeNull();
    });

    it("undefined → null", () => {
      expect(inferLibroFromCuenta(undefined)).toBeNull();
    });
  });

  describe("trim + prefijo", () => {
    it("'  700  ' → ingresos (trim antes de buscar)", () => {
      expect(inferLibroFromCuenta("  700  ")).toBe("ingresos");
    });
  });
});

describe("libroLabel", () => {
  it("ingresos → 'Emitidas (ventas/ingresos)'", () => {
    expect(libroLabel("ingresos")).toBe("Emitidas (ventas/ingresos)");
  });
  it("gastos → 'Recibidas (compras/gastos)'", () => {
    expect(libroLabel("gastos")).toBe("Recibidas (compras/gastos)");
  });
  it("bienes → 'Bienes de inversión'", () => {
    expect(libroLabel("bienes")).toBe("Bienes de inversión");
  });
  it("'ventas' (forma corta del backend) → 'Emitidas (ventas/ingresos)'", () => {
    expect(libroLabel("ventas")).toBe("Emitidas (ventas/ingresos)");
  });
  it("undefined → '—'", () => {
    expect(libroLabel(undefined)).toBe("—");
  });
});

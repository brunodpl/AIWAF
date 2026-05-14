/**
 * Snapshot estático del maestro contable HORECA.
 *
 * Fuente de verdad:
 *   sistema-de-asientos-automatizado/data/maestros/maestro_contable_fiscal.yaml (v3)
 *
 * Si el yaml cambia (nuevo código de cuenta, nuevo concepto, label revisado),
 * actualizar también este array. Mantener en orden ascendente por code.
 */

export interface CuentaMaestro {
  code: string;
  label: string;
  concepto: string;
  group: "2" | "6" | "7";
  descripcion: string;
}

export const CUENTAS: readonly CuentaMaestro[] = [
  // ── Grupo 2: bienes de inversión ──
  { code: "212", label: "Instalaciones técnicas",                   concepto: "instalacion_tecnica",     group: "2", descripcion: "Instalaciones eléctricas, gas, climatización o frigoríficas fijas" },
  { code: "213", label: "Maquinaria",                               concepto: "maquinaria",              group: "2", descripcion: "Hornos, lavavajillas industrial, cafetera industrial, freidoras" },
  { code: "216", label: "Mobiliario",                               concepto: "mobiliario",              group: "2", descripcion: "Muebles, sillas, mesas, mostrador, barra, estanterías" },
  { code: "217", label: "Equipos para procesos de información",     concepto: "equipo_informatico",      group: "2", descripcion: "Ordenadores, TPV, tablets, impresoras, escáner" },
  { code: "218", label: "Elementos de transporte",                  concepto: "vehiculo",                group: "2", descripcion: "Vehículos de empresa: furgoneta, coche, camión, motocicleta" },

  // ── Grupo 6: compras y gastos ──
  { code: "600", label: "Compras de mercaderías",                   concepto: "mercaderias",             group: "6", descripcion: "Productos terminados sin transformación (bebidas embotelladas, snacks, tabaco)" },
  { code: "601", label: "Compras de materias primas",               concepto: "materias_primas",         group: "6", descripcion: "Ingredientes crudos que se transforman en cocina" },
  { code: "602", label: "Otros aprovisionamientos",                 concepto: "aprovisionamientos",      group: "6", descripcion: "Consumibles del proceso (envases, servilletas, vajilla desechable, limpieza)" },
  { code: "607", label: "Trabajos realizados por otras empresas",   concepto: "trabajos_subcontratados", group: "6", descripcion: "Servicios productivos encargados a terceros (maquila, subcontratación)" },
  { code: "621", label: "Arrendamientos y cánones",                 concepto: "alquiler_local",          group: "6", descripcion: "Alquiler de local, maquinaria o equipos; leasing operativo" },
  { code: "622", label: "Reparaciones y conservación",              concepto: "reparacion_conservacion", group: "6", descripcion: "Mantenimiento de activos existentes" },
  { code: "623", label: "Servicios de profesionales independientes", concepto: "profesional_independiente", group: "6", descripcion: "Honorarios de abogados, asesores, notarios, auditores, arquitectos" },
  { code: "624", label: "Transportes",                              concepto: "transporte",              group: "6", descripcion: "Transporte, mensajería, flete y distribución por terceros" },
  { code: "625", label: "Primas de seguros",                        concepto: "seguro",                  group: "6", descripcion: "Seguros de la actividad (multirriesgo, RC, vehículos). Generalmente exentos de IVA" },
  { code: "626", label: "Servicios bancarios y similares",          concepto: "gasto_bancario",          group: "6", descripcion: "Comisiones bancarias, mantenimiento, transferencias. Exentos de IVA" },
  { code: "627", label: "Publicidad, propaganda y RR.PP.",          concepto: "publicidad",              group: "6", descripcion: "Marketing, publicidad, diseño gráfico, rótulos, patrocinio" },
  { code: "628", label: "Suministros",                              concepto: "suministros",             group: "6", descripcion: "Electricidad, agua, gas, telefonía e internet del local" },
  { code: "629", label: "Otros servicios",                          concepto: "servicios_generales",     group: "6", descripcion: "Papelería, limpieza, suscripciones de software, dietas" },
  { code: "631", label: "Otros tributos",                           concepto: "tributo",                 group: "6", descripcion: "IBI, IAE, tasas municipales. Sin IVA" },
  { code: "640", label: "Sueldos y salarios",                       concepto: "nomina",                  group: "6", descripcion: "Nóminas y retribuciones del personal. Sin IVA" },
  { code: "642", label: "Seguridad Social a cargo de la empresa",   concepto: "seguridad_social",        group: "6", descripcion: "Cuota patronal, cotizaciones TC1/TC2. Sin IVA" },

  // ── Grupo 7: ventas e ingresos ──
  { code: "700", label: "Ventas de mercaderías",                    concepto: "venta_mercaderias",       group: "7", descripcion: "Ingresos por venta de productos sin transformación" },
  { code: "705", label: "Prestación de servicios",                  concepto: "prestacion_servicios",    group: "7", descripcion: "Ingresos por servicios prestados a terceros" },
];

export const CUENTA_BY_CODE: Readonly<Record<string, CuentaMaestro>> =
  Object.fromEntries(CUENTAS.map((c) => [c.code, c]));

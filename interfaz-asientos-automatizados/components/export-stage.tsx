"use client";

import { useState, useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
  Download,
  Search,
  Loader2,
  ArrowLeft,
  CheckCircle,
  AlertCircle,
} from "lucide-react";
import { ApprovedInvoiceData, Client, FiscalLine } from "@/lib/types";
import { fetchClients } from "@/lib/api";
import { generateCSV, downloadCSV } from "@/lib/csv";

interface ExportStageProps {
  approvedInvoices: Map<string, ApprovedInvoiceData>;
  onUpdateInvoice: (
    id: string,
    updates: Partial<Pick<ApprovedInvoiceData, "clase_fiscal" | "cuenta_contable">>
  ) => void;
  onBack: () => void;
}

const CLASES_FISCALES = [
  "gasto_deducible_interior",
  "gasto_no_deducible",
  "gasto_no_deducible_iva",
  "bien_inversion",
  "ingreso_interior",
  "gasto_intracomunitario",
  "ingreso_intracomunitario",
  "gasto_importacion",
  "gasto_isp",
  "recargo_equivalencia",
];

export function ExportStage({ approvedInvoices, onUpdateInvoice, onBack }: ExportStageProps) {
  const [clients, setClients] = useState<Map<string, Client>>(new Map());
  const [clientsLoaded, setClientsLoaded] = useState(false);
  const [loadingClients, setLoadingClients] = useState(false);

  // Load clients on mount
  useEffect(() => {
    // Don't auto-load — wait for user to click "Contrastar NIFs"
  }, []);

  const handleContrastarNIFs = useCallback(async () => {
    try {
      setLoadingClients(true);
      const response = await fetchClients();
      const clientMap = new Map<string, Client>();
      for (const client of response.clients) {
        clientMap.set(client.nif.toUpperCase(), client);
      }
      setClients(clientMap);
      setClientsLoaded(true);

      const matches = response.clients.filter((c) =>
        Array.from(approvedInvoices.values()).some(
          (inv) => {
            const nifReceptor = inv.formData.nif_receptor?.toUpperCase() || "";
            const nifEmisor = inv.formData.nif_entidad?.toUpperCase() || "";
            return c.nif.toUpperCase() === nifReceptor || c.nif.toUpperCase() === nifEmisor;
          }
        )
      );

      toast.success(
        `Base de clientes cargada: ${response.clients.length} clientes, ${matches.length} coinciden con facturas aprobadas`
      );
    } catch (err) {
      console.error("Error loading clients:", err);
      toast.error("Error cargando base de clientes");
    } finally {
      setLoadingClients(false);
    }
  }, [approvedInvoices]);

  const handleDownloadCSV = useCallback(() => {
    try {
      if (approvedInvoices.size === 0) {
        toast.error("No hay facturas aprobadas para exportar");
        return;
      }

      // Check for missing account codes
      let missingAccounts = 0;
      for (const [_id, invoice] of approvedInvoices) {
        const nifRec = invoice.formData.nif_receptor?.toUpperCase() || "";
        const nifEmi = invoice.formData.nif_entidad?.toUpperCase() || "";
        const hasAccount =
          invoice.cuenta_contable ||
          clients.has(nifRec) ||
          clients.has(nifEmi);
        if (!hasAccount) {
          missingAccounts++;
        }
      }

      if (missingAccounts > 0) {
        const confirmed = window.confirm(
          `Hay ${missingAccounts} factura(s) sin cuenta contable asignada. ¿Continuar de todas formas?`
        );
        if (!confirmed) return;
      }

      const csvContent = generateCSV(approvedInvoices, clients);
      downloadCSV(csvContent);
      toast.success("CSV descargado correctamente");
    } catch (err) {
      console.error("Error generating CSV:", err);
      toast.error("Error generando el CSV");
    }
  }, [approvedInvoices, clients]);

  const handleClaseFiscalChange = useCallback(
    (id: string, value: string) => {
      onUpdateInvoice(id, { clase_fiscal: value });
    },
    [onUpdateInvoice]
  );

  const handleCuentaContableChange = useCallback(
    (id: string, value: string) => {
      onUpdateInvoice(id, { cuenta_contable: value });
    },
    [onUpdateInvoice]
  );

  return (
    <div className="flex flex-col h-full bg-white">
      {/* Header */}
      <header className="h-14 border-b bg-slate-50/50 flex items-center justify-between px-6 flex-shrink-0">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
            className="text-xs rounded-none uppercase tracking-[0.15em]"
          >
            <ArrowLeft className="h-3 w-3 mr-1" />
            Volver
          </Button>
          <h2 className="text-xs font-black uppercase tracking-[0.1em] text-slate-800">
            Generación de Asientos
          </h2>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-bold text-slate-400 font-mono">
            {approvedInvoices.size} factura(s) aprobada(s)
          </span>
        </div>
      </header>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        {/* Summary table */}
        <div className="mb-6">
          <h3 className="text-xs font-black uppercase tracking-[0.1em] text-slate-700 mb-3">
            Resumen de Facturas Aprobadas
          </h3>
          <div className="border border-slate-200 overflow-hidden">
            <table className="w-full text-xs font-mono border-collapse">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-100 text-[10px] text-slate-400 uppercase font-black">
                  <th className="p-3 text-left tracking-widest">ID</th>
                  <th className="p-3 text-left tracking-widest">Num. Factura</th>
                  <th className="p-3 text-left tracking-widest">NIF Emisor</th>
                  <th className="p-3 text-left tracking-widest">Nombre Emisor</th>
                  <th className="p-3 text-right tracking-widest">Total €</th>
                  <th className="p-3 text-left tracking-widest">Clase Fiscal</th>
                  <th className="p-3 text-left tracking-widest">Cuenta Contable</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {Array.from(approvedInvoices.entries()).map(([id, invoice]) => {
                  const nifReceptor = invoice.formData.nif_receptor || "";
                  const nifEmisor = invoice.formData.nif_entidad || "";
                  const clientMatch = clientsLoaded
                    ? (clients.get(nifReceptor.toUpperCase()) || clients.get(nifEmisor.toUpperCase()) || null)
                    : null;

                  return (
                    <tr key={id} className="hover:bg-slate-50/30 transition-colors">
                      <td className="p-3 font-mono text-[10px]">{id}</td>
                      <td className="p-3">{invoice.formData.numero_factura || "—"}</td>
                      <td className="p-3">{nifEmisor || "—"}</td>
                      <td className="p-3">{invoice.formData.nombre_entidad || "—"}</td>
                      <td className="p-3 text-right">{invoice.formData.total_euros || "0"}</td>
                      <td className="p-3">
                        <select
                          value={invoice.clase_fiscal || "gasto_deducible_interior"}
                          onChange={(e) => handleClaseFiscalChange(id, e.target.value)}
                          className="text-[10px] font-mono border border-slate-100 rounded-none bg-transparent p-1"
                        >
                          {CLASES_FISCALES.map((cf) => (
                            <option key={cf} value={cf}>
                              {cf}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="p-3">
                        {clientMatch ? (
                          <div className="flex items-center gap-1">
                            <CheckCircle className="h-3 w-3 text-emerald-500" />
                            <span className="text-emerald-600 font-bold">
                              {clientMatch.cuenta_contable}
                            </span>
                          </div>
                        ) : (
                          <Input
                            value={invoice.cuenta_contable || ""}
                            onChange={(e) => handleCuentaContableChange(id, e.target.value)}
                            placeholder={clientsLoaded ? "NIF no encontrado" : "Pendiente"}
                            className={cn(
                              "h-8 text-xs border-slate-100 rounded-none font-mono",
                              clientsLoaded &&
                                nifEmisor &&
                                !clientMatch &&
                                "border-amber-200 bg-amber-50/30"
                            )}
                          />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* NIF Lookup section */}
        <div className="mb-6 p-4 bg-slate-50 rounded border border-slate-100">
          <div className="flex items-center gap-3">
            <Search className="h-4 w-4 text-slate-400" />
            <span className="text-xs font-bold text-slate-600">Base de clientes</span>
            {loadingClients && <Loader2 className="h-3 w-3 animate-spin text-slate-400" />}
          </div>
          <p className="text-[10px] text-slate-400 mt-1">
            {clientsLoaded
              ? `${clients.size} clientes cargados. Las cuentas contables coincidentes se muestran en verde.`
              : "Pulsa para contrastar los NIFs de las facturas con la base de clientes de la gestoría."}
          </p>
          <Button
            onClick={handleContrastarNIFs}
            disabled={loadingClients}
            variant="outline"
            size="sm"
            className="mt-3 text-[10px] uppercase tracking-[0.15em] rounded-none"
          >
            {loadingClients ? (
              <>
                <Loader2 className="h-3 w-3 animate-spin mr-1" />
                Cargando...
              </>
            ) : (
              "Contrastar NIFs"
            )}
          </Button>
        </div>
      </div>

      {/* Footer */}
      <footer className="h-24 flex items-center justify-center gap-6 px-8 bg-white border-t flex-shrink-0">
        <Button
          size="lg"
          onClick={handleDownloadCSV}
          disabled={approvedInvoices.size === 0}
          className={cn(
            "w-64 h-11 bg-slate-900 border border-slate-900 hover:bg-black text-white font-bold uppercase text-[10px] tracking-[0.2em] rounded-none shadow-lg transition-all",
            approvedInvoices.size === 0 && "opacity-50 cursor-not-allowed"
          )}
        >
          <Download className="h-4 w-4 mr-2" />
          Descargar CSV
        </Button>
      </footer>
    </div>
  );
}

"use client";

import React from "react";
import { FiscalLine } from "@/lib/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface FiscalLinesTableProps {
  lines: FiscalLine[];
}

export function FiscalLinesTable({ lines }: FiscalLinesTableProps) {
  if (lines.length === 0) return <div className="p-4 text-xs text-muted-foreground italic">No hay líneas fiscales extraídas.</div>;

  return (
    <div className="rounded-md border bg-white overflow-hidden shadow-sm">
      <Table>
        <TableHeader className="bg-gray-50/80">
          <TableRow>
            <TableHead className="text-[10px] uppercase font-bold text-muted-foreground">Base</TableHead>
            <TableHead className="text-[10px] uppercase font-bold text-muted-foreground text-center">IVA %</TableHead>
            <TableHead className="text-[10px] uppercase font-bold text-muted-foreground text-right">Cuota</TableHead>
            <TableHead className="text-[10px] uppercase font-bold text-muted-foreground text-right">Total</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {lines.map((line) => (
            <TableRow key={line.id} className="hover:bg-gray-50/50 transition-colors">
              <TableCell className="font-mono text-xs">{line.base.toFixed(2)}€</TableCell>
              <TableCell className="text-center font-mono text-xs">{line.vatRate}%</TableCell>
              <TableCell className="text-right font-mono text-xs font-medium">{line.vatAmount.toFixed(2)}€</TableCell>
              <TableCell className="text-right font-mono text-xs font-bold text-blue-700">{line.total.toFixed(2)}€</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

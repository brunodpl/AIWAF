import { InvoiceReviewer } from "@/components/invoice-reviewer"
import { Toaster } from "@/components/ui/sonner"

export default function Home() {
  return (
    <div className="w-full h-screen bg-slate-50 overflow-hidden">
      <InvoiceReviewer />
      <Toaster position="top-right" closeButton richColors />
    </div>
  )
}
